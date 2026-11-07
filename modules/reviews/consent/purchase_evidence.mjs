/** Private, test-only purchaser evidence reader. Never route this Worker publicly. */
const encoder = new TextEncoder();
const id = /^[A-Za-z0-9_-]{1,128}$/;
const hash = /^[a-f0-9]{64}$/;
const sessionPattern = /^cs_test_[A-Za-z0-9]{10,180}$/;
const unavailable = () => Response.json({error:'unavailable'},{status:503,headers:{'Cache-Control':'no-store'}});
const hex = bytes => [...new Uint8Array(bytes)].map(x=>x.toString(16).padStart(2,'0')).join('');
function randomHex() { return hex(crypto.getRandomValues(new Uint8Array(32))); }
async function mac(secret, message, isHex=false) {
  const material = isHex ? Uint8Array.from(secret.match(/../g),x=>parseInt(x,16)) : encoder.encode(secret);
  const key=await crypto.subtle.importKey('raw',material,{name:'HMAC',hash:'SHA-256'},false,['sign']);
  return hex(await crypto.subtle.sign('HMAC',key,encoder.encode(message)));
}
function equal(a,b) {
  if(typeof a!=='string'||typeof b!=='string'||a.length!==b.length) return false;
  let x=0; for(let i=0;i<a.length;i++) x|=a.charCodeAt(i)^b.charCodeAt(i); return x===0;
}
async function jsonBounded(response,max=16384) {
  if(!response.ok || !response.body || Number(response.headers.get('Content-Length')||'0')>max) throw Error('unavailable');
  const reader=response.body.getReader(), decoder=new TextDecoder(); let n=0,out='',timer;
  const timeout=new Promise((_,reject)=>{timer=setTimeout(()=>reject(Error('timeout')),2000);});
  try { while(true) { const part=await Promise.race([reader.read(),timeout]); if(part.done) return JSON.parse(out+decoder.decode());
    n+=part.value.byteLength; if(n>max) throw Error('too large'); out+=decoder.decode(part.value,{stream:true}); } }
  finally { clearTimeout(timer); void reader.cancel().catch(()=>{}); }
}
async function callBounded(fetcher,url,init) {
  const controller=new AbortController(); let timer;
  const timeout=new Promise((_,reject)=>{timer=setTimeout(()=>{controller.abort();reject(Error('timeout'));},2000);});
  try { return await jsonBounded(await Promise.race([fetcher(url,{...init,redirect:'error',signal:controller.signal}),timeout])); }
  finally { clearTimeout(timer); controller.abort(); }
}
function configured(env) {
  return env.PURCHASE_EVIDENCE_MODE==='test' && env.STORE_KEY==='acorn-store' && env.ENVIRONMENT==='test' &&
    env.COMMERCE_DB?.prepare && env.CONTEXT_DB?.prepare && env.ATTESTOR?.fetch && env.CF_VERSION_METADATA?.id &&
    typeof env.STRIPE_TEST_KEY==='string' && /^(sk_test_|rk_test_)/.test(env.STRIPE_TEST_KEY) &&
    typeof env.PURCHASE_ATTESTATION_TOKEN==='string' && env.PURCHASE_ATTESTATION_TOKEN.length>=32 &&
    hash.test(env.PURCHASE_ATTESTATION_KEY||'') && typeof env.RECIPIENT_HASH_KEY==='string' && env.RECIPIENT_HASH_KEY.length>=32 &&
    env.RECIPIENT_HASH_KEY!==env.PURCHASE_ATTESTATION_TOKEN && typeof env.APPROVED_TEST_EMAIL==='string' &&
    typeof env.RECIPIENT_RESOLVE_TOKEN==='string' && env.RECIPIENT_RESOLVE_TOKEN.length>=32 &&
    env.RECIPIENT_RESOLVE_TOKEN!==env.PURCHASE_ATTESTATION_TOKEN && env.RECIPIENT_RESOLVE_TOKEN!==env.RECIPIENT_HASH_KEY &&
    id.test(env.INSTALLATION_ID||'') && /^acct_[A-Za-z0-9]+$/.test(env.STRIPE_ACCOUNT_ID||'') &&
    /^sha256:[a-f0-9]{64}$/.test(env.CATALOGUE_VERSION||'') && hash.test(env.MAPPING_SHA256||'');
}
async function attest(env) {
  const nonce=randomHex();
  const result=await callBounded(env.ATTESTOR.fetch.bind(env.ATTESTOR),'https://attestor.internal/attest-purchase',{
    method:'POST',headers:{'Content-Type':'application/json','Authorization':'Bearer '+env.PURCHASE_ATTESTATION_TOKEN},
    body:JSON.stringify({authority_version:env.CF_VERSION_METADATA.id,nonce})});
  const p=result.proof, now=Math.floor(Date.now()/1000);
  if(!Array.isArray(p)||p.length!==16 || p[0]!=='cartray-reviews-purchase-attestation-v1' || p[1]!==env.STORE_KEY || p[2]!=='test' ||
    p[3]!==env.INSTALLATION_ID || p[4]!==env.COMMERCE_DB_ID || p[5]!==env.CONTEXT_DB_ID || p[6]!==env.STRIPE_ACCOUNT_ID ||
    p[7]!==env.CATALOGUE_VERSION || p[8]!==env.MAPPING_SHA256 || p[9]!==env.SOURCE_WORKER_VERSION ||
    p[10]!==env.REVIEWS_WORKER_VERSION || p[11]!==env.PRODUCER_WORKER_VERSION || p[12]!==env.CF_VERSION_METADATA.id || p[13]!==nonce ||
    !Number.isSafeInteger(p[14]) || !Number.isSafeInteger(p[15]) || p[14]>now || p[15]<=now || p[15]-p[14]>30 ||
    !equal(result.signature,await mac(env.PURCHASE_ATTESTATION_KEY,JSON.stringify(p),true))) throw Error('attestation');
  return p;
}
function normalizeEmail(value) {
  if(typeof value!=='string' || value.length>254 || value!==value.trim() || !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(value)) throw Error('recipient');
  const at=value.lastIndexOf('@'); return value.slice(0,at)+'@'+value.slice(at+1).toLowerCase();
}
async function resolve(session,contextHash,env,fetcher) {
  if(!sessionPattern.test(session) || !hash.test(contextHash)) throw Error('scope');
  const proof=await attest(env);
  const now=Math.floor(Date.now()/1000);
  const context=await env.CONTEXT_DB.prepare('SELECT checkout_session_id FROM purchaser_contexts WHERE token_hash=? AND expires_at>?').bind(contextHash,now).first();
  if(!context || context.checkout_session_id!==session) throw Error('context');
  const row=await env.COMMERCE_DB.prepare("SELECT o.order_id,o.manifest_version,o.currency,o.subtotal_minor,o.items_digest,s.external_session_id,s.settlement_event_id,s.stripe_payment_status,s.stripe_amount_total_minor,b.id AS outbox_id,b.payload_json FROM orders o JOIN checkout_sessions s ON s.order_id=o.order_id JOIN outbox b ON b.order_id=o.order_id AND b.event_type='OrderConfirmed' WHERE s.external_session_id=? AND s.settlement_state='confirmed' LIMIT 1").bind(session).first();
  if(!row || row.manifest_version!==proof[7] || row.stripe_payment_status!=='paid' || row.stripe_amount_total_minor!==row.subtotal_minor) throw Error('order');
  // Existing CartRay reconciliation uses this canonical acquisition event ID
  // when there is no Stripe webhook event; it is not a Stripe event claim.
  const eventId=row.settlement_event_id || 'recon_'+row.order_id;
  if(!id.test(eventId)) throw Error('event');
  const payload=JSON.parse(row.payload_json);
  if(payload.order_id!==row.order_id || payload.session_id!==session || payload.items_digest!==row.items_digest || Object.keys(payload).length!==3) throw Error('outbox');
  const items=(await env.COMMERCE_DB.prepare('SELECT stripe_price_id,quantity,unit_amount_minor FROM order_items WHERE order_id=? ORDER BY stripe_price_id LIMIT 101').bind(row.order_id).all()).results;
  if(!items.length || items.length>100) throw Error('items');
  const headers={'Authorization':'Bearer '+env.STRIPE_TEST_KEY,'Stripe-Version':'2025-09-30.clover'};
  const account=await callBounded(fetcher,'https://api.stripe.com/v1/account',{headers});
  if(account.id!==proof[6]) throw Error('stripe account');
  const stripe=await callBounded(fetcher,'https://api.stripe.com/v1/checkout/sessions/'+encodeURIComponent(session),{headers});
  if(stripe.id!==session || stripe.livemode!==false || stripe.status!=='complete' || stripe.payment_status!=='paid' ||
    stripe.currency!==row.currency || stripe.amount_total!==row.stripe_amount_total_minor || stripe.metadata?.cr_order_id!==row.order_id || stripe.metadata?.cr_catalogue_version!==row.manifest_version || stripe.metadata?.cr_items_digest!==row.items_digest) throw Error('stripe session');
  const lines=await callBounded(fetcher,'https://api.stripe.com/v1/checkout/sessions/'+encodeURIComponent(session)+'/line_items?limit=100',{headers});
  if(lines.has_more!==false || !Array.isArray(lines.data) || lines.data.length!==items.length) throw Error('stripe items');
  const expected=items.map(x=>[x.stripe_price_id,x.quantity,x.unit_amount_minor]).sort();
  const actual=lines.data.map(x=>[x.price?.id,x.quantity,x.price?.unit_amount]).sort();
  if(JSON.stringify(actual)!==JSON.stringify(expected)) throw Error('stripe items');
  const email=normalizeEmail(stripe.customer_details?.email);
  if(email!==normalizeEmail(env.APPROVED_TEST_EMAIL)) throw Error('test recipient');
  const version='v1';
  const recipientHash=await mac(env.RECIPIENT_HASH_KEY,JSON.stringify(['acorn-recipient-v1',env.STORE_KEY,'test',row.order_id,version,email]));
  const reference='ref_'+await mac(env.RECIPIENT_HASH_KEY,JSON.stringify(['acorn-delivery-v1',contextHash,recipientHash]));
  const sourceProvenance=env.INSTALLATION_ID+'_'+proof[9];
  if(!id.test(sourceProvenance)) throw Error('source provenance');
  return {email, store:env.STORE_KEY,environment:'test',session_id:session,context_hash:contextHash,confirmed:true,source_attested:true,
    event_id:eventId,acquisition_id:row.order_id,source_provenance:sourceProvenance,recipient_context_hash:recipientHash,
    binding_version:version,recipient_reference:reference,outbox_id:row.outbox_id};
}
export async function purchaseEvidence(request,env,fetcher=fetch) {
  const path=new URL(request.url).pathname;
  if(!configured(env) || request.method!=='POST' || !['/confirmed','/delivery-recipient'].includes(path) ||
    (request.headers.get('content-type')||'')!=='application/json') return unavailable();
  try {
    const input=await jsonBounded(new Response(request.body,{headers:request.headers}),512);
    if(!input || Array.isArray(input)) return unavailable();
    if(path==='/confirmed') {
      if(Object.keys(input).sort().join(',')!=='context_hash,session_id') return unavailable();
      const {email,...safe}=await resolve(input.session_id,input.context_hash,env,fetcher);
      return Response.json(safe,{headers:{'Cache-Control':'no-store'}});
    }
    if(Object.keys(input).sort().join(',')!=='context_hash,recipient_reference' || !hash.test(input.context_hash || '') ||
       !equal(request.headers.get('Authorization')||'', 'Bearer '+env.RECIPIENT_RESOLVE_TOKEN)) return unavailable();
    const context=await env.CONTEXT_DB.prepare('SELECT checkout_session_id FROM purchaser_contexts WHERE token_hash=? AND expires_at>?')
      .bind(input.context_hash,Math.floor(Date.now()/1000)).first();
    if(!context) return unavailable();
    const result=await resolve(context.checkout_session_id,input.context_hash,env,fetcher);
    if(!equal(input.recipient_reference,result.recipient_reference)) return unavailable();
    return Response.json({email:result.email},{headers:{'Cache-Control':'no-store'}});
  } catch { return unavailable(); }
}
export default {fetch:(request,env)=>purchaseEvidence(request,env)};
