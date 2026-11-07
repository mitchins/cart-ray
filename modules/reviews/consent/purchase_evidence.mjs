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
  try { return await jsonBounded(await Promise.race([fetcher(url,{...init,redirect:'manual',signal:controller.signal}),timeout])); }
  finally { clearTimeout(timer); controller.abort(); }
}
function configured(env) {
  return env.PURCHASE_EVIDENCE_MODE==='test' && env.STORE_KEY==='acorn-store' && env.ENVIRONMENT==='test' &&
    env.COMMERCE_DB?.prepare && env.CONTEXT_DB?.prepare && env.ATTESTOR?.fetch && env.REVIEWS_API?.fetch && env.CF_VERSION_METADATA?.id &&
    typeof env.STRIPE_TEST_KEY==='string' && /^(sk_test_|rk_test_)/.test(env.STRIPE_TEST_KEY) &&
    typeof env.PURCHASE_ATTESTATION_TOKEN==='string' && env.PURCHASE_ATTESTATION_TOKEN.length>=32 &&
    hash.test(env.PURCHASE_ATTESTATION_KEY||'') && typeof env.RECIPIENT_HASH_KEY==='string' && env.RECIPIENT_HASH_KEY.length>=32 &&
    env.RECIPIENT_HASH_KEY!==env.PURCHASE_ATTESTATION_TOKEN && typeof env.APPROVED_TEST_EMAIL==='string' &&
    typeof env.RECIPIENT_RESOLVE_TOKEN==='string' && env.RECIPIENT_RESOLVE_TOKEN.length>=32 &&
    env.RECIPIENT_RESOLVE_TOKEN!==env.PURCHASE_ATTESTATION_TOKEN && env.RECIPIENT_RESOLVE_TOKEN!==env.RECIPIENT_HASH_KEY &&
    typeof env.REVIEWS_AUTOMATION_INGRESS_TOKEN==='string' && env.REVIEWS_AUTOMATION_INGRESS_TOKEN.length>=32 &&
    ![env.PURCHASE_ATTESTATION_TOKEN,env.RECIPIENT_RESOLVE_TOKEN,env.RECIPIENT_HASH_KEY].includes(env.REVIEWS_AUTOMATION_INGRESS_TOKEN) &&
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
async function resolve(session,contextHash,env,fetcher,{automationOutbox=null}={}) {
  const automatic=automationOutbox!==null;
  if(automatic ? !Number.isSafeInteger(automationOutbox)||automationOutbox<1 : !sessionPattern.test(session) || !hash.test(contextHash)) throw Error('scope');
  if(automatic) {
    // Existing suppression pseudonyms cannot be translated from a new key.
    // Refuse evidence until an explicitly provisioned, immutable registry row
    // matches both the key version and a domain-separated key check.
    if(!id.test(env.RECIPIENT_HASH_KEY_VERSION || '') || env.RECIPIENT_HASH_KEY_VERSION.length>60) throw Error('suppression key unavailable');
    const pinned=await env.CONTEXT_DB.prepare('SELECT r.key_version,r.key_check FROM review_suppression_key_registry r JOIN review_suppression_key_current c USING(store_key,environment,installation_id,key_version) WHERE r.store_key=? AND r.environment=? AND r.installation_id=?')
      .bind(env.STORE_KEY,'test',env.INSTALLATION_ID).first();
    const check=await mac(env.RECIPIENT_HASH_KEY,JSON.stringify(['reviews-suppression-key-check-v1',env.STORE_KEY,'test',env.INSTALLATION_ID,env.RECIPIENT_HASH_KEY_VERSION]));
    if(!pinned || pinned.key_version!==env.RECIPIENT_HASH_KEY_VERSION || !equal(pinned.key_check,check)) throw Error('suppression key unavailable');
  }
  const proof=await attest(env);
  const now=Math.floor(Date.now()/1000);
  if(!automatic) {
    const context=await env.CONTEXT_DB.prepare('SELECT checkout_session_id FROM purchaser_contexts WHERE token_hash=? AND expires_at>?').bind(contextHash,now).first();
    if(!context || context.checkout_session_id!==session) throw Error('context');
  }
  const row=await env.COMMERCE_DB.prepare("SELECT o.order_id,o.manifest_version,o.currency,o.subtotal_minor,o.items_digest,s.external_session_id,s.settlement_event_id,s.settled_at,s.stripe_payment_status,s.stripe_amount_total_minor,b.id AS outbox_id,b.payload_json FROM orders o JOIN checkout_sessions s ON s.order_id=o.order_id JOIN outbox b ON b.order_id=o.order_id AND b.event_type='OrderConfirmed' WHERE "+(automatic ? 'b.id=?' : 's.external_session_id=?')+" AND s.settlement_state='confirmed' LIMIT 1").bind(automatic ? automationOutbox : session).first();
  if(automatic) {
    if(!row || row.outbox_id!==automationOutbox || !sessionPattern.test(row.external_session_id || '') ||
      !Number.isSafeInteger(row.settled_at) || row.settled_at<1 || row.settled_at>now) throw Error('order');
    session=row.external_session_id;
  }
  if(!row || row.manifest_version!==proof[7] || row.stripe_payment_status!=='paid' || row.stripe_amount_total_minor!==row.subtotal_minor) throw Error('order');
  let grant;
  if(automatic) {
    grant=await env.COMMERCE_DB.prepare('SELECT c.selected,c.merchant_default_selected,c.wording_version AS choice_version,c.wording AS choice_wording,c.recorded_at,g.wording_version,g.wording,g.granted_at,g.merchant_default_selected AS grant_default,g.selection_basis FROM review_invitation_checkout_choices c LEFT JOIN review_invitation_checkout_grants g ON g.order_id=c.order_id WHERE c.order_id=?').bind(row.order_id).first();
    if(!grant || (grant.selected===0 && grant.granted_at===null)) throw Error('no checkout grant');
    if(grant.selected!==1 || ![0,1].includes(grant.merchant_default_selected) || grant.grant_default!==grant.merchant_default_selected ||
      grant.selection_basis!==(grant.merchant_default_selected===1 ? 'preselected_acceptance' : 'express_opt_in') ||
      grant.wording_version!==grant.choice_version || grant.wording!==grant.choice_wording || grant.granted_at!==grant.recorded_at ||
      grant.wording_version!=='review-invitation-v1' || grant.wording!=='Email me one request to review my purchase.' ||
      !Number.isSafeInteger(grant.granted_at) || grant.granted_at<1 || grant.granted_at>row.settled_at) throw Error('grant');
  }
  const eligibility=await callBounded(env.REVIEWS_API.fetch.bind(env.REVIEWS_API),'https://reviews.internal/reviews-internal/v1/automation-evidence-status',{
    method:'POST',headers:{'Content-Type':'application/json','Authorization':'Bearer '+env.REVIEWS_AUTOMATION_INGRESS_TOKEN},body:JSON.stringify({outbox_id:row.outbox_id})});
  if(eligibility.state!=='held_eligibility_recipient_consent') throw Error('not eligible');
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
  // v2 has a stable merchant-scoped pseudonym for purpose suppression.
  // Retain v1's order-scoped interpretation for the old, test-only authority.
  let version=automatic ? 'v2_'+env.RECIPIENT_HASH_KEY_VERSION : 'v1';
  let recipientHash=await mac(env.RECIPIENT_HASH_KEY,JSON.stringify(automatic
    ? ['acorn-recipient-v2',env.STORE_KEY,'test',email]
    : ['acorn-recipient-v1',env.STORE_KEY,'test',row.order_id,version,email]));
  let priorConsent=null;
  if(automatic) {
    const replay=await callBounded(env.REVIEWS_API.fetch.bind(env.REVIEWS_API),'https://reviews.internal/reviews-internal/v1/automation-recipient-replay',{
      method:'POST',headers:{'Content-Type':'application/json','Authorization':'Bearer '+env.REVIEWS_AUTOMATION_INGRESS_TOKEN},
      body:JSON.stringify({outbox_id:row.outbox_id,candidate:recipientHash})});
    if(replay.status==='suppressed') throw Error('suppressed');
    if(replay.status==='existing') {
      if(!hash.test(replay.recipient_context_hash||'') || !id.test(replay.binding_version||'') ||
        replay.verified_at!==row.settled_at || (replay.consent_reference!==null &&
        (!id.test(replay.consent_reference||'') || replay.granted_at!==grant.granted_at))) throw Error('replay');
      recipientHash=replay.recipient_context_hash; version=replay.binding_version;
      priorConsent=replay.consent_reference;
    } else if(replay.status!=='new') throw Error('replay');
  }
  const reference='ref_'+await mac(env.RECIPIENT_HASH_KEY,JSON.stringify(['acorn-delivery-v1',automatic ? row.order_id : contextHash,recipientHash]));
  const sourceProvenance=env.INSTALLATION_ID+'_'+proof[9];
  if(!id.test(sourceProvenance)) throw Error('source provenance');
  return {email, store:env.STORE_KEY,environment:'test',session_id:session,context_hash:contextHash,confirmed:true,source_attested:true,
    event_id:eventId,acquisition_id:row.order_id,source_provenance:sourceProvenance,recipient_context_hash:recipientHash,
    binding_version:version,recipient_reference:reference,outbox_id:row.outbox_id,
    ...(automatic ? {grant,settled_at:row.settled_at,prior_consent_reference:priorConsent} : {})};
}
// Build exactly the payloads accepted by the existing Reviews Worker; this
// authenticates purchase-contact provenance, never mailbox possession.
export async function buildAutomationEvidence(result,env) {
  const ref=result.prior_consent_reference || 'checkout_'+await mac(env.RECIPIENT_HASH_KEY,JSON.stringify(['checkout-grant-v1',result.acquisition_id,result.grant.wording_version,result.grant.wording,result.grant.granted_at,result.grant.merchant_default_selected,result.grant.selection_basis]));
  const recipient={binding_version:result.binding_version,evidence_reference:'purchase_contact_'+result.outbox_id,
    outbox_id:result.outbox_id,recipient_context_hash:result.recipient_context_hash,verified_at:result.settled_at};
  const consent={event_id:result.event_id,evidence_reference:ref,granted_at:result.grant.granted_at,recipient_context_hash:result.recipient_context_hash};
  return [
    ['automation-recipient-evidence','X-Reviews-Recipient-Signature',env.REVIEWS_RECIPIENT_EVIDENCE_SECRET,recipient,'reviews-automation-recipient-v1'],
    ['consents','X-Reviews-Consent-Signature',env.REVIEWS_CONSENT_SECRET,consent,'reviews-consent-v1'],
  ];
}

export async function purchaseEvidence(request,env,fetcher=fetch) {
  const path=new URL(request.url).pathname;
  if(!configured(env) || request.method!=='POST' || !['/confirmed','/delivery-recipient','/automation-record','/automation-recipient'].includes(path) ||
    (request.headers.get('content-type')||'')!=='application/json') return unavailable();
  try {
    const input=await jsonBounded(new Response(request.body,{headers:request.headers}),512);
    if(!input || Array.isArray(input)) return unavailable();
    if(path==='/automation-recipient') {
      const token=env.AUTOMATION_RECIPIENT_TOKEN;
      if(Object.keys(input).join(',')!=='outbox_id' || !Number.isSafeInteger(input.outbox_id) || input.outbox_id<1 ||
        typeof token!=='string' || token.length<32 || [env.RECIPIENT_HASH_KEY,env.AUTOMATION_EVIDENCE_TOKEN,env.RECIPIENT_RESOLVE_TOKEN,env.PURCHASE_ATTESTATION_TOKEN,env.REVIEWS_AUTOMATION_INGRESS_TOKEN,env.REVIEWS_RECIPIENT_EVIDENCE_SECRET,env.REVIEWS_CONSENT_SECRET].includes(token) ||
        !equal(request.headers.get('Authorization')||'', 'Bearer '+token)) return unavailable();
      let result;
      try { result=await resolve(null,null,env,fetcher,{automationOutbox:input.outbox_id}); }
      catch(error) { if(error?.message==='suppressed') return Response.json({status:'suppressed'},{headers:{'Cache-Control':'no-store'}}); throw error; }
      return Response.json({email:result.email,recipient_context_hash:result.recipient_context_hash},{headers:{'Cache-Control':'no-store'}});
    }
    if(path==='/automation-record') {
      // The caller supplies only an outbox cursor, never order, recipient or grant data.
      // This private endpoint may be retried after an intake or downstream outage.
      const token=env.AUTOMATION_EVIDENCE_TOKEN, recipientSecret=env.REVIEWS_RECIPIENT_EVIDENCE_SECRET, consentSecret=env.REVIEWS_CONSENT_SECRET;
      if(Object.keys(input).join(',')!=='outbox_id' || !Number.isSafeInteger(input.outbox_id) || input.outbox_id<1 ||
        [token,recipientSecret,consentSecret].some(x=>typeof x!=='string'||x.length<32) ||
        new Set([token,recipientSecret,consentSecret,env.RECIPIENT_HASH_KEY,env.REVIEWS_AUTOMATION_INGRESS_TOKEN,env.RECIPIENT_RESOLVE_TOKEN,env.PURCHASE_ATTESTATION_TOKEN]).size!==7 ||
        !equal(request.headers.get('Authorization')||'', 'Bearer '+token)) return unavailable();
      let result;
      try { result=await resolve(null,null,env,fetcher,{automationOutbox:input.outbox_id}); }
      catch(error) {
        if(['no checkout grant','suppressed'].includes(error?.message)) return Response.json({status:'not_granted'},{headers:{'Cache-Control':'no-store'}});
        throw error;
      }
      // Contact evidence reflects Stripe's confirmed purchase contact, not proof
      // of mailbox ownership. The legacy field is named verified_at.
      for(const [route,header,secret,data,label] of await buildAutomationEvidence(result,env)) {
        const signature=await mac(secret,JSON.stringify([label,env.STORE_KEY,'test',data]));
        await callBounded(env.REVIEWS_API.fetch.bind(env.REVIEWS_API),'https://reviews.internal/reviews-internal/v1/'+route,{
          method:'POST',headers:{'Content-Type':'application/json',[header]:signature},body:JSON.stringify(data)});
      }
      return Response.json({status:'recorded'},{headers:{'Cache-Control':'no-store'}});
    }
    if(path==='/confirmed') {
      if(Object.keys(input).length!==2 || !Object.hasOwn(input,'context_hash') || !Object.hasOwn(input,'session_id')) return unavailable();
      const {email,...safe}=await resolve(input.session_id,input.context_hash,env,fetcher);
      return Response.json(safe,{headers:{'Cache-Control':'no-store'}});
    }
    if(Object.keys(input).length!==2 || !Object.hasOwn(input,'context_hash') || !Object.hasOwn(input,'recipient_reference') || !hash.test(input.context_hash || '') ||
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
