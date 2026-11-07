/** Test-only one-message sender and capability-based unsubscribe gateway. */
const hex=/^[a-f0-9]{64}$/;
const capability=/^[1-9][0-9]{0,15}\.[a-f0-9]{64}$/;
const mailbox=/^[^\s@<>(),;:\\"\[\]]+@[^\s@<>(),;:\\"\[\]]+\.[A-Za-z]{2,}$/;
function configured(e){
  return e.REVIEWS_ENVIRONMENT==='test' && e.INITIAL_SEND_MODE==='test' && e.REVIEWS_API?.fetch && e.PURCHASE_EVIDENCE?.fetch &&
    [e.REVIEWS_AUTOMATION_TOKEN,e.AUTOMATION_RECIPIENT_TOKEN,e.REVIEWS_DELIVERY_TOKEN].every(x=>typeof x==='string'&&x.length>=32) &&
    new Set([e.REVIEWS_AUTOMATION_TOKEN,e.AUTOMATION_RECIPIENT_TOKEN,e.REVIEWS_DELIVERY_TOKEN]).size===3 &&
    mailbox.test(e.APPROVED_TEST_EMAIL||'') && mailbox.test(e.TEST_SENDER||'') &&
    typeof e.SMTP2GO_API_KEY==='string' && e.SMTP2GO_API_KEY.startsWith('api-') &&
    typeof e.MERCHANT_NAME==='string' && e.MERCHANT_NAME.trim().length>0 && e.MERCHANT_NAME.length<=100 && !/[\x00-\x1f\x7f]/.test(e.MERCHANT_NAME) &&
    typeof e.MERCHANT_CONTACT==='string' && e.MERCHANT_CONTACT.trim().length>0 && e.MERCHANT_CONTACT.length<=200 && !/[\x00-\x1f\x7f]/.test(e.MERCHANT_CONTACT);
}
function safeUrl(raw) {let u;try{u=new URL(raw)}catch{throw Error('configuration unavailable')}if(u.protocol!=='https:'||u.username||u.password||u.hash||u.search)throw Error('configuration unavailable');return u}
async function bounded(fetcher,url,options,ms=10000){
  const controller=new AbortController(); let timer,reader;
  const deadline=new Promise((_,reject)=>{timer=setTimeout(()=>{controller.abort();reject(Error('timeout'))},ms)});
  try{return await Promise.race([(async()=>{const response=await fetcher(url,{...options,signal:controller.signal,redirect:'manual'});
    if(!response.ok||!response.body)throw Error('service unavailable');reader=response.body.getReader();let size=0,text='';const decoder=new TextDecoder();
    for(;;){const part=await reader.read();if(part.done)break;size+=part.value.byteLength;if(size>16384)throw Error('response too large');text+=decoder.decode(part.value,{stream:true})}
    return JSON.parse(text+decoder.decode());})(),deadline])}finally{clearTimeout(timer);controller.abort();if(reader)void reader.cancel().catch(()=>{})}
}
async function call(service,path,body,token){
  return bounded(service.fetch.bind(service),'https://reviews.internal'+path,{method:'POST',headers:{'content-type':'application/json',authorization:'Bearer '+token},body:JSON.stringify(body)});
}
export async function dispatchInitial(message,env,fetcher=fetch,now=()=>Math.floor(Date.now()/1000)) {
 if(!configured(env))throw Error('test sender unavailable');
 const landing=safeUrl(env.REVIEWS_LANDING_URL), unsubscribeBase=safeUrl(env.UNSUBSCRIBE_BASE_URL);
 if(!message || Object.keys(message).join(',')!=='outbox_id' || !Number.isSafeInteger(message.outbox_id) || message.outbox_id<1)return 'discard';
 const recipient=await call(env.PURCHASE_EVIDENCE,'/automation-recipient',message,env.AUTOMATION_RECIPIENT_TOKEN);
 if(recipient.email!==env.APPROVED_TEST_EMAIL || !hex.test(recipient.recipient_context_hash||''))throw Error('recipient unavailable');
 const draft=await call(env.REVIEWS_API,'/reviews-internal/v1/automation-initial-prepare',message,env.REVIEWS_AUTOMATION_TOKEN);
 if(draft.recipient_context_hash!==recipient.recipient_context_hash || !capability.test(draft.unsubscribe_token||'') ||
    !hex.test(draft.generation_id||'') || !hex.test(draft.delivery_identity||'')) throw Error('delivery reconciliation required');
 // An authenticated durable acceptance is terminal. A dispatching or uncertain
 // generation still needs reconciliation and must never trigger another send.
 if(draft.state==='accepted')return 'done';
 if(draft.state!=='prepared' || !hex.test(draft.token||''))throw Error('delivery reconciliation required');
 const claim={outbox_id:message.outbox_id,acquisition_id:draft.acquisition_id,generation_id:draft.generation_id,delivery_identity:draft.delivery_identity};
 const claimed=await call(env.REVIEWS_API,'/reviews-internal/v1/automation-initial-claim',claim,env.REVIEWS_AUTOMATION_TOKEN);
 if(claimed.state!=='dispatching')throw Error('delivery reconciliation required');
 landing.hash='invitation='+draft.token;
 unsubscribeBase.pathname=unsubscribeBase.pathname.replace(/\/$/,'')+'/'+draft.unsubscribe_token;
 const identity={acquisition_id:draft.acquisition_id,generation_id:draft.generation_id,delivery_identity:draft.delivery_identity};
 try{
   const result=await bounded(fetcher,'https://api.smtp2go.com/v3/email/send',{method:'POST',headers:{'content-type':'application/json','X-Smtp2go-Api-Key':env.SMTP2GO_API_KEY},body:JSON.stringify({
     sender:env.TEST_SENDER,to:[env.APPROVED_TEST_EMAIL],subject:`Review your purchase from ${env.MERCHANT_NAME}`,
     text_body:`Thank you for your purchase from ${env.MERCHANT_NAME}. If you would like to share your experience, visit:\n\n${landing}\n\nAnyone with this link can use it.\n\nContact: ${env.MERCHANT_CONTACT}\nStop review-request emails: ${unsubscribeBase}`,
     custom_headers:[{header:'List-Unsubscribe',value:`<${unsubscribeBase}>`},{header:'List-Unsubscribe-Post',value:'List-Unsubscribe=One-Click'},{header:'X-Reviews-Delivery-Identity',value:draft.delivery_identity}],fastaccept:false})});
   if(result?.data?.succeeded!==1||result.data.failed!==0||!Array.isArray(result.data.failures)||result.data.failures.length!==0||typeof result.data.email_id!=='string'||!/^[A-Za-z0-9_-]{1,128}$/.test(result.data.email_id))throw Error('unknown provider result');
 }catch(error){try{await call(env.REVIEWS_API,'/reviews-internal/v1/invitations/delivery-outcome',{...identity,outcome:'uncertain'},env.REVIEWS_DELIVERY_TOKEN)}catch{}throw Error('delivery reconciliation required',{cause:error});}
 const accepted_at=now();
 try{await call(env.REVIEWS_API,'/reviews-internal/v1/invitations/delivery-outcome',{...identity,outcome:'accepted',accepted_at},env.REVIEWS_DELIVERY_TOKEN)}catch{throw Error('delivery reconciliation required')}
 return 'sent';
}
export async function unsubscribeGateway(request,env){
 const url=new URL(request.url), match=/^\/unsubscribe\/([1-9][0-9]{0,15}\.[a-f0-9]{64})$/.exec(url.pathname);
 if(env.REVIEWS_ENVIRONMENT!=='test'||!env.REVIEWS_API?.fetch||!match||url.search)return new Response('Unavailable',{status:404});
 if(request.method==='GET'||request.method==='HEAD') return new Response(request.method==='HEAD'?null:'<!doctype html><title>Unsubscribe</title><h1>Stop review-request emails</h1><form method="post"><button>Unsubscribe</button></form>',{headers:{'content-type':'text/html; charset=utf-8','cache-control':'no-store','referrer-policy':'no-referrer','content-security-policy':"default-src 'none'; form-action 'self'; base-uri 'none'"}});
 if(request.method!=='POST')return new Response('Method not allowed',{status:405});
 const length=Number(request.headers.get('content-length')||'0'); if(length>128)return new Response('Invalid request',{status:400});
 try{const response=await env.REVIEWS_API.fetch('https://reviews.internal/reviews/v1/invitation-unsubscribe',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({token:match[1]})}); if(!response.ok)throw Error('unavailable'); return new Response('You will not receive review-request emails for this purchase.',{headers:{'cache-control':'no-store'}})}catch{return new Response('Please try again later.',{status:503,headers:{'cache-control':'no-store'}})}
}
export default {fetch:unsubscribeGateway,async queue(batch,env){for(const msg of batch.messages){try{await dispatchInitial(msg.body,env);msg.ack()}catch{msg.retry({delaySeconds:60})}}}};
