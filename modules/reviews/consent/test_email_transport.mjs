/** Test-only verification-code transport; no invitation sending. */
const encoder=new TextEncoder();
const hash=/^[a-f0-9]{64}$/;
const reference=/^ref_[a-f0-9]{64}$/;
const unavailable=()=>Response.json({error:'unavailable'},{status:503,headers:{'Cache-Control':'no-store'}});
function equal(a,b){if(typeof a!=='string'||typeof b!=='string'||a.length!==b.length)return false;let x=0;for(let i=0;i<a.length;i++)x|=a.charCodeAt(i)^b.charCodeAt(i);return x===0;}
async function jsonBounded(response,limit=4096){
 if(!response.ok || !response.body || Number(response.headers.get('content-length')||'0')>limit) throw Error('unavailable');
 const reader=response.body.getReader(),decoder=new TextDecoder();let n=0,out='',timer;
 const timeout=new Promise((_,reject)=>{timer=setTimeout(()=>reject(Error('timeout')),2000)});
 try{while(true){const part=await Promise.race([reader.read(),timeout]);if(part.done)return JSON.parse(out+decoder.decode());
 n+=part.value.byteLength;if(n>limit)throw Error('large');out+=decoder.decode(part.value,{stream:true});}}
 finally{clearTimeout(timer);void reader.cancel().catch(()=>{});}
}
async function call(fetcher,url,init,timeoutMs=2000){const controller=new AbortController();let timer;
 const timeout=new Promise((_,reject)=>{timer=setTimeout(()=>{controller.abort();reject(Error('timeout'));},timeoutMs)});
 try{return await jsonBounded(await Promise.race([fetcher(url,{...init,redirect:'error',signal:controller.signal}),timeout]));}
 finally{clearTimeout(timer);controller.abort();}}
function configured(env){return env.TEST_EMAIL_MODE==='test' && env.CONTEXT_DB?.prepare && env.PURCHASE_EVIDENCE?.fetch &&
 typeof env.PURCHASE_TRANSPORT_TOKEN==='string' && env.PURCHASE_TRANSPORT_TOKEN.length>=32 &&
 typeof env.RECIPIENT_RESOLVE_TOKEN==='string' && env.RECIPIENT_RESOLVE_TOKEN.length>=32 &&
 env.PURCHASE_TRANSPORT_TOKEN!==env.RECIPIENT_RESOLVE_TOKEN && typeof env.SMTP2GO_API_KEY==='string' && env.SMTP2GO_API_KEY.startsWith('api-') &&
 typeof env.APPROVED_TEST_EMAIL==='string' && /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(env.APPROVED_TEST_EMAIL) &&
 typeof env.TEST_SENDER==='string' && /^[A-Za-z0-9._+-]+@acorn-test\.effectivepolicy\.com\.au$/.test(env.TEST_SENDER);}
export async function sendTestCode(request,env,fetcher=fetch){
 if(!configured(env)||request.method!=='POST'||new URL(request.url).pathname!=='/send' ||
 !equal(request.headers.get('Authorization')||'','Bearer '+env.PURCHASE_TRANSPORT_TOKEN)||
 (request.headers.get('Content-Type')||'')!=='application/json')return unavailable();
 let input;
 try{input=await jsonBounded(new Response(request.body,{headers:request.headers}),512);}catch{return unavailable();}
 if(!input||Array.isArray(input)||Object.keys(input).sort().join(',')!=='code,context_hash,recipient_reference'||
 !hash.test(input.context_hash||'')||!reference.test(input.recipient_reference||'')||!/^[0-9]{6}$/.test(input.code||''))return unavailable();
 try{
  const recipient=await call(env.PURCHASE_EVIDENCE.fetch.bind(env.PURCHASE_EVIDENCE),'https://purchase-evidence.internal/delivery-recipient',{
   method:'POST',headers:{'Content-Type':'application/json','Authorization':'Bearer '+env.RECIPIENT_RESOLVE_TOKEN},
   body:JSON.stringify({context_hash:input.context_hash,recipient_reference:input.recipient_reference})},15000);
  if(!equal(recipient.email,env.APPROVED_TEST_EMAIL))return unavailable();
  const now=Math.floor(Date.now()/1000);
  const claimed=await env.CONTEXT_DB.prepare("INSERT INTO purchaser_test_email_claims(token_hash,delivery_state,created_at) VALUES(?,'claimed',?) ON CONFLICT(token_hash) DO NOTHING RETURNING token_hash").bind(input.context_hash,now).first();
  if(!claimed)return unavailable();
  try{
   const sent=await call(fetcher,'https://api.smtp2go.com/v3/email/send',{method:'POST',headers:{'Content-Type':'application/json','X-Smtp2go-Api-Key':env.SMTP2GO_API_KEY},
    body:JSON.stringify({sender:env.TEST_SENDER,to:[env.APPROVED_TEST_EMAIL],subject:'Squirrel test verification code',text_body:`Your Squirrel test verification code is ${input.code}. It expires in 10 minutes.`,fastaccept:false})});
   if(sent?.data?.succeeded!==1 || sent?.data?.failed!==0 || !Array.isArray(sent?.data?.failures) || sent.data.failures.length!==0 ||
      typeof sent.data.email_id!=='string' || !/^[A-Za-z0-9_-]{1,128}$/.test(sent.data.email_id))throw Error('uncertain');
   await env.CONTEXT_DB.prepare("UPDATE purchaser_test_email_claims SET delivery_state='accepted',provider_receipt=? WHERE token_hash=? AND delivery_state='claimed'").bind(sent.data.email_id,input.context_hash).run();
   return Response.json({accepted:true},{headers:{'Cache-Control':'no-store'}});
  }catch{
   await env.CONTEXT_DB.prepare("UPDATE purchaser_test_email_claims SET delivery_state='uncertain' WHERE token_hash=? AND delivery_state='claimed'").bind(input.context_hash).run();
   return unavailable();
  }
 }catch{return unavailable();}
}
export default {fetch:(request,env)=>sendTestCode(request,env)};
