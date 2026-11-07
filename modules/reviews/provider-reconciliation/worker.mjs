/** Private, bounded SMTP2GO evidence lookup. Never sends or infers failure from absence. */
const ID=/^[A-Za-z0-9_-]{1,128}$/;
const HEX=/^[a-f0-9]{64}$/;
const EVENTS=new Set(['processed','delivered','rejected','soft-bounced','hard-bounced']);
const HEADER='x-reviews-delivery-identity';
const unavailable=()=>new Response('unavailable',{status:503,headers:{'Cache-Control':'no-store'}});
function equal(a,b){if(typeof a!=='string'||typeof b!=='string')return false;let d=a.length^b.length;for(let i=0;i<b.length;i++)d|=b.charCodeAt(i)^(a.charCodeAt(i)||0);return d===0}
async function readBounded(stream,limit,timeout=10000){if(!stream)throw Error('missing');const reader=stream.getReader();let n=0,s='',timer;const dec=new TextDecoder();try{const deadline=new Promise((_,reject)=>{timer=setTimeout(()=>{void reader.cancel().catch(()=>{});reject(Error('timeout'))},timeout)});return await Promise.race([(async()=>{for(;;){const x=await reader.read();if(x.done)break;n+=x.value.byteLength;if(n>limit)throw Error('too large');s+=dec.decode(x.value,{stream:true})}return JSON.parse(s+dec.decode())})(),deadline])}finally{clearTimeout(timer);void reader.cancel().catch(()=>{})}}
export async function reconcile(input,env,fetcher=fetch,now=Math.floor(Date.now()/1000)){
 if(env.REVIEWS_ENVIRONMENT!=='test'||env.SMTP2GO_RECONCILIATION!=='test'||!env.REVIEWS_DB?.prepare||
    typeof env.SMTP2GO_ACTIVITY_KEY!=='string'||!env.SMTP2GO_ACTIVITY_KEY.startsWith('api-')||
    !input||Object.keys(input).length!==3||!Object.hasOwn(input,'acquisition_id')||
    !Object.hasOwn(input,'delivery_identity')||!Object.hasOwn(input,'generation_id')||
    !ID.test(input.acquisition_id||'')||!HEX.test(input.delivery_identity||'')||!HEX.test(input.generation_id||'')||
    !ID.test(env.REVIEWS_STORE||'')||!Number.isSafeInteger(now)||now<1)throw Error('unavailable');
 const row=await env.REVIEWS_DB.prepare('SELECT provider_email_id,accepted_at FROM review_provider_deliveries WHERE store_key=? AND environment=? AND acquisition_id=? AND generation_id=? AND delivery_identity=? AND provider=?')
  .bind(env.REVIEWS_STORE,'test',input.acquisition_id,input.generation_id,input.delivery_identity,'smtp2go').first();
 if(!row)return {status:'unknown',reason:'no_provider_identity'};
 if(!ID.test(row.provider_email_id||'')||!Number.isSafeInteger(row.accepted_at)||row.accepted_at<1||row.accepted_at>now)throw Error('unavailable');
 const controller=new AbortController();let timer;
 try{
  const deadline=new Promise((_,reject)=>{timer=setTimeout(()=>{controller.abort();reject(Error('timeout'))},10000)});
  return await Promise.race([(async()=>{
   const response=await fetcher('https://api.smtp2go.com/v3/activity/search',{method:'POST',redirect:'manual',signal:controller.signal,
    headers:{'Content-Type':'application/json','X-Smtp2go-Api-Key':env.SMTP2GO_ACTIVITY_KEY},
    body:JSON.stringify({search_email_id:row.provider_email_id,start_date:new Date(Math.max(0,row.accepted_at-86400)*1000).toISOString(),event_types:[...EVENTS],limit:100})});
   if(!response.ok)throw Error('provider unavailable');const data=(await readBounded(response.body,65536))?.data;
   if(!data||!Array.isArray(data.events)||data.events.length>100||data.continue_token||
      !Number.isSafeInteger(data.total_events)||data.total_events!==data.events.length)throw Error('incomplete provider evidence');
   const events=[];
   for(const e of data.events){
    if(!e||e.email_id!==row.provider_email_id||!EVENTS.has(e.event)||typeof e.date!=='string'||
       !/^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,9})?Z$/.test(e.date)||!Number.isFinite(Date.parse(e.date))||
       Date.parse(e.date)<(row.accepted_at-86400)*1000||Date.parse(e.date)>(now+300)*1000)throw Error('invalid provider evidence');
    events.push({type:e.event,date:e.date});
   }
   if(!events.length)return {status:'unknown',reason:'no_activity'};
   const statements=events.map(e=>env.REVIEWS_DB.prepare('INSERT OR IGNORE INTO review_provider_activity (store_key,environment,acquisition_id,generation_id,provider_email_id,event_type,event_at,observed_at) SELECT store_key,environment,acquisition_id,generation_id,provider_email_id,?,?,? FROM review_provider_deliveries WHERE store_key=? AND environment=? AND acquisition_id=? AND generation_id=? AND delivery_identity=? AND provider_email_id=?')
    .bind(e.type,e.date,now,env.REVIEWS_STORE,'test',input.acquisition_id,input.generation_id,input.delivery_identity,row.provider_email_id));
   await env.REVIEWS_DB.batch(statements);
   return {status:'observed',events:events.map(e=>e.type)};
  })(),deadline]);
 }finally{clearTimeout(timer);controller.abort()}
}
/** Discover a lost provider ID via bounded recipient history and an exact delivery header.
 * A match is evidence for an operator, not an acceptance timestamp or permission to resend.
 */
export async function discover(input,env,fetcher=fetch,now=Math.floor(Date.now()/1000)){
 if(env.REVIEWS_ENVIRONMENT!=='test'||env.SMTP2GO_RECONCILIATION!=='test'||!env.REVIEWS_DB?.prepare||
    !env.PURCHASE_EVIDENCE?.fetch||typeof env.SMTP2GO_ACTIVITY_KEY!=='string'||!env.SMTP2GO_ACTIVITY_KEY.startsWith('api-')||
    typeof env.AUTOMATION_RECIPIENT_TOKEN!=='string'||env.AUTOMATION_RECIPIENT_TOKEN.length<32||
    env.AUTOMATION_RECIPIENT_TOKEN===env.RECONCILIATION_TOKEN||!ID.test(env.REVIEWS_STORE||'')||
    !input||Object.keys(input).length!==3||!Object.hasOwn(input,'acquisition_id')||
    !Object.hasOwn(input,'delivery_identity')||!Object.hasOwn(input,'generation_id')||
    !ID.test(input.acquisition_id||'')||!HEX.test(input.delivery_identity||'')||!HEX.test(input.generation_id||'')||
    !Number.isSafeInteger(now)||now<1)throw Error('unavailable');
 const row=await env.REVIEWS_DB.prepare("SELECT g.state,g.prepared_at,i.outbox_id FROM invitation_generations g JOIN review_automation_intents i USING(store_key,environment,acquisition_id) WHERE g.store_key=? AND g.environment=? AND g.acquisition_id=? AND g.generation_id=? AND g.delivery_identity=? AND (g.request_key=('initial_' || i.outbox_id) OR g.request_key LIKE 'recovery-%') LIMIT 1")
  .bind(env.REVIEWS_STORE,'test',input.acquisition_id,input.generation_id,input.delivery_identity).first();
 if(!row||!['dispatching','uncertain','accepted','revoked'].includes(row.state)||!Number.isSafeInteger(row.prepared_at)||
    !Number.isSafeInteger(row.outbox_id)||row.prepared_at>now||row.outbox_id<1)throw Error('unavailable');
 // Old history may have expired. Never treat missing history as proof of failure.
 if(now-row.prepared_at>4*86400)return {status:'unknown',reason:'history_window'};
 const controller=new AbortController();let timer;
 try{const deadline=new Promise((_,reject)=>{timer=setTimeout(()=>{controller.abort();reject(Error('timeout'))},15000)});
  return await Promise.race([(async()=>{
   const recipientResponse=await env.PURCHASE_EVIDENCE.fetch('https://purchase-evidence.internal/automation-recipient',{
    method:'POST',signal:controller.signal,headers:{'Content-Type':'application/json',Authorization:'Bearer '+env.AUTOMATION_RECIPIENT_TOKEN},body:JSON.stringify({outbox_id:row.outbox_id})});
   if(!recipientResponse.ok)throw Error('recipient unavailable');const recipient=await readBounded(recipientResponse.body,4096);
   if(typeof recipient.email!=='string'||recipient.email!==env.APPROVED_TEST_EMAIL)throw Error('recipient unavailable');
   const start=new Date(Math.max(0,row.prepared_at-300)*1000).toISOString();
   const end=new Date(Math.min(now+1,row.prepared_at+86400)*1000).toISOString();
   const collected=[];let cursor,totalSeen=0,expectedTotal;const cursors=new Set();
   for(let page=0;page<5;page++){
    const payload={start_date:start,end_date:end,search_recipient:recipient.email,search_recipient_op:'equals',
     event_types:[...EVENTS],custom_headers:['X-Reviews-Delivery-Identity'],limit:100,...(cursor?{continue_token:cursor}:{})};
    const response=await fetcher('https://api.smtp2go.com/v3/activity/search',{method:'POST',redirect:'manual',signal:controller.signal,
     headers:{'Content-Type':'application/json','X-Smtp2go-Api-Key':env.SMTP2GO_ACTIVITY_KEY},body:JSON.stringify(payload)});
    if(!response.ok)throw Error('provider unavailable');const data=(await readBounded(response.body,65536))?.data;
    if(!data||!Array.isArray(data.events)||data.events.length>100||!Number.isSafeInteger(data.total_events)||data.total_events>500)throw Error('incomplete evidence');
    if(expectedTotal===undefined)expectedTotal=data.total_events;
    if(data.total_events!==expectedTotal)throw Error('incomplete evidence');totalSeen+=data.events.length;
    for(const e of data.events){
     const values=e?.custom_headers;let identity;
     if(values===undefined||values===null)throw Error('incomplete headers');
     if(values && !Array.isArray(values) && typeof values==='object'){
      const matches=Object.entries(values).filter(([k])=>k.toLowerCase()===HEADER);if(matches.length>1)throw Error('ambiguous headers');if(matches.length===1)identity=matches[0][1];
     }else if(Array.isArray(values)){
      const matches=values.filter(v=>typeof v?.header==='string'&&v.header.toLowerCase()===HEADER);if(matches.length>1)throw Error('ambiguous headers');if(matches.length===1)identity=matches[0].value;
     }else throw Error('invalid headers');
     if(identity!==input.delivery_identity)continue;
     // SMTP2GO processed events omit recipient; their documented `to` field
     // still identifies the intended mailbox. Never accept a conflicting value.
     if((e.recipient!==undefined && e.recipient!==recipient.email)||e.to!==recipient.email)throw Error('recipient mismatch');
     if(!ID.test(e.email_id||'')||!EVENTS.has(e.event)||typeof e.date!=='string'||
        !/^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,9})?Z$/.test(e.date)||!Number.isFinite(Date.parse(e.date))||
        Date.parse(e.date)<(row.prepared_at-300)*1000||Date.parse(e.date)>(now+300)*1000)throw Error('invalid evidence');
     collected.push({email_id:e.email_id,type:e.event,date:e.date});
    }
    if(!data.continue_token){cursor=null;break}
    if(typeof data.continue_token!=='string'||data.continue_token.length>2048||cursors.has(data.continue_token))throw Error('incomplete evidence');
    cursors.add(data.continue_token);cursor=data.continue_token;
   }
   if(cursor||totalSeen!==expectedTotal)throw Error('incomplete evidence');
   const ids=new Set(collected.map(x=>x.email_id));if(ids.size!==1)return {status:'unknown',reason:ids.size?'ambiguous':'no_match'};
   await env.REVIEWS_DB.batch(collected.map(e=>env.REVIEWS_DB.prepare('INSERT OR IGNORE INTO review_provider_identity_discoveries SELECT store_key,environment,acquisition_id,generation_id,delivery_identity,?,?,?,? FROM invitation_generations WHERE store_key=? AND environment=? AND acquisition_id=? AND generation_id=? AND delivery_identity=? AND state IN (\'dispatching\',\'uncertain\',\'accepted\',\'revoked\')')
    .bind(e.email_id,e.type,e.date,now,env.REVIEWS_STORE,'test',input.acquisition_id,input.generation_id,input.delivery_identity)));
   return {status:'matched_needs_operator',events:collected.map(x=>x.type)};
  })(),deadline]);
 }finally{clearTimeout(timer);controller.abort()}
}
export default {async fetch(request,env){
 if(env.REVIEWS_ENVIRONMENT!=='test'||env.SMTP2GO_RECONCILIATION!=='test'||typeof env.RECONCILIATION_TOKEN!=='string'||env.RECONCILIATION_TOKEN.length<32||
    env.RECONCILIATION_TOKEN===env.SMTP2GO_ACTIVITY_KEY||!equal(request.headers.get('Authorization')||'','Bearer '+env.RECONCILIATION_TOKEN))return unavailable();
 const path=new URL(request.url).pathname;
 if(request.method!=='POST'||!['/reconcile','/discover'].includes(path)||request.headers.get('content-type')!=='application/json')return new Response('not found',{status:404});
 try{const body=await readBounded(request.body,512,2000);
  const result=await (path==='/discover'?discover(body,env):reconcile(body,env));return Response.json(result,{headers:{'Cache-Control':'no-store'}})
 }catch{return unavailable()}
}};
