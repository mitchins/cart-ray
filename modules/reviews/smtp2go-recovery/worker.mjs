/** Single-recipient SMTP2GO bridge for an already-claimed test recovery delivery. */
const ID=/^[a-f0-9]{64}$/;
const EMAIL=/^[^\s@<>(),;:\\"\[\]]+@[^\s@<>(),;:\\"\[\]]+\.[A-Za-z]{2,}$/;
const unavailable=()=>new Response('unavailable',{status:503,headers:{'Cache-Control':'no-store'}});
function equal(a,b){if(typeof a!=='string'||typeof b!=='string')return false;let d=a.length^b.length;for(let i=0;i<b.length;i++)d|=b.charCodeAt(i)^(a.charCodeAt(i)||0);return d===0}
async function readBounded(stream,limit,ms=10000){if(!stream)throw Error('missing');const reader=stream.getReader();let timer,n=0,text='';const decoder=new TextDecoder();try{const deadline=new Promise((_,reject)=>{timer=setTimeout(()=>{void reader.cancel().catch(()=>{});reject(Error('timeout'))},ms)});return await Promise.race([(async()=>{for(;;){const x=await reader.read();if(x.done)break;n+=x.value.byteLength;if(n>limit)throw Error('too large');text+=decoder.decode(x.value,{stream:true})}return JSON.parse(text+decoder.decode())})(),deadline])}finally{clearTimeout(timer);void reader.cancel().catch(()=>{})}}
export async function send(message,env,fetcher=fetch){
 if(env.REVIEWS_ENVIRONMENT!=='test'||env.SMTP2GO_RECOVERY_MODE!=='test'||
    !EMAIL.test(env.APPROVED_TEST_EMAIL||'')||!EMAIL.test(env.RECOVERY_FROM||'')||
    typeof env.SMTP2GO_API_KEY!=='string'||!env.SMTP2GO_API_KEY.startsWith('api-')||
    !message||Object.keys(message).length!==5||!Object.hasOwn(message,'from')||!Object.hasOwn(message,'to')||
    !Object.hasOwn(message,'subject')||!Object.hasOwn(message,'text')||!Object.hasOwn(message,'headers')||
    message.from!==env.RECOVERY_FROM||message.to!==env.APPROVED_TEST_EMAIL||
    typeof message.subject!=='string'||message.subject.length<1||message.subject.length>200||/[\x00-\x1f\x7f]/.test(message.subject)||
    typeof message.text!=='string'||message.text.length<1||message.text.length>16000||
    !message.headers||Object.keys(message.headers).length!==1||!ID.test(message.headers['X-Reviews-Delivery-Identity']||''))throw Error('unavailable');
 const controller=new AbortController();let timer;
 try{const deadline=new Promise((_,reject)=>{timer=setTimeout(()=>{controller.abort();reject(Error('timeout'))},10000)});
  return await Promise.race([(async()=>{const response=await fetcher('https://api.smtp2go.com/v3/email/send',{method:'POST',redirect:'manual',signal:controller.signal,
   headers:{'Content-Type':'application/json','X-Smtp2go-Api-Key':env.SMTP2GO_API_KEY},body:JSON.stringify({sender:message.from,to:[message.to],subject:message.subject,text_body:message.text,
    custom_headers:[{header:'X-Reviews-Delivery-Identity',value:message.headers['X-Reviews-Delivery-Identity']}],fastaccept:false})});
   if(!response.ok)throw Error('provider unavailable');const data=(await readBounded(response.body,16384))?.data;
   if(data?.succeeded!==1||data.failed!==0||!Array.isArray(data.failures)||data.failures.length!==0||typeof data.email_id!=='string'||!/^[A-Za-z0-9_-]{1,128}$/.test(data.email_id))throw Error('provider outcome unknown');
   return {email_id:data.email_id};})(),deadline]);
 }finally{clearTimeout(timer);controller.abort()}
}
export default {async fetch(request,env){
 if(env.REVIEWS_ENVIRONMENT!=='test'||env.SMTP2GO_RECOVERY_MODE!=='test'||typeof env.SMTP2GO_MAIL_TOKEN!=='string'||env.SMTP2GO_MAIL_TOKEN.length<32||
    env.SMTP2GO_MAIL_TOKEN===env.SMTP2GO_API_KEY||!equal(request.headers.get('Authorization')||'','Bearer '+env.SMTP2GO_MAIL_TOKEN))return unavailable();
 if(request.method!=='POST'||new URL(request.url).pathname!=='/send'||request.headers.get('Content-Type')!=='application/json')return new Response('not found',{status:404});
 try{return Response.json(await send(await readBounded(request.body,20000,2000),env),{headers:{'Cache-Control':'no-store'}})}catch{return unavailable()}
}};
