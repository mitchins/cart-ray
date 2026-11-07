/** Test-only same-origin checkout context and private consent authority.
 * PURCHASE_EVIDENCE must independently attest the CartRay source and return
 * confirmed, scoped purchase contact evidence. It is deliberately not supplied.
 */
const enc = new TextEncoder();
const cookieName = '__Host-acorn-purchase';
const unavailable = () => Response.json({error:'unavailable'}, {status:503, headers:{'Cache-Control':'no-store'}});
const safeId = /^[A-Za-z0-9_-]{1,128}$/;
const sessionId = /^cs_test_[A-Za-z0-9]{10,180}$/;
const digest = async (value) => [...new Uint8Array(await crypto.subtle.digest('SHA-256', enc.encode(value)))].map(x=>x.toString(16).padStart(2,'0')).join('');
const token = () => [...crypto.getRandomValues(new Uint8Array(32))].map(x=>x.toString(16).padStart(2,'0')).join('');
async function codeDigest(secret, context, code) {
  const key=await crypto.subtle.importKey('raw',enc.encode(secret),{name:'HMAC',hash:'SHA-256'},false,['sign']);
  const signature=await crypto.subtle.sign('HMAC',key,enc.encode(`acorn-possession-v1\0${context}\0${code}`));
  return [...new Uint8Array(signature)].map(x=>x.toString(16).padStart(2,'0')).join('');
}
function generateCode() {
  const draw=new Uint32Array(1); let value;
  do { crypto.getRandomValues(draw); value=draw[0]; } while (value>=4294000000);
  return String(value%1000000).padStart(6,'0');
}
async function boundedText(response, maximum) {
  if (!response.body || Number(response.headers.get('content-length') || '0') > maximum) throw new Error('unavailable');
  const reader = response.body.getReader();
  let timer;
  const timeout = new Promise((_,reject)=>{timer=setTimeout(()=>reject(new Error('timeout')),2000);});
  try {
    let size=0, out=''; const decoder=new TextDecoder();
    while (true) {
      const part=await Promise.race([reader.read(),timeout]);
      if (part.done) return out+decoder.decode();
      size+=part.value.byteLength; if(size>maximum) throw new Error('unavailable');
      out+=decoder.decode(part.value,{stream:true});
    }
  } finally { clearTimeout(timer); void reader.cancel().catch(()=>{}); }
}
function configured(env) {
  return env.ACORN_PURCHASE_CONTEXT === 'test' && env.REVIEWS_STORE_KEY === 'acorn-store' && env.REVIEWS_ENVIRONMENT === 'test' &&
    env.CONTEXT_DB?.prepare && env.CHECKOUT?.fetch && env.PURCHASE_EVIDENCE?.fetch &&
    typeof env.STOREFRONT_ORIGIN === 'string' && /^https:\/\/[a-z0-9.-]+(?::[0-9]+)?$/.test(env.STOREFRONT_ORIGIN);
}
function requestToken(request) {
  const cookie = request.headers.get('Cookie') || '';
  if (cookie.length > 4096) return null;
  const values = cookie.split(';').map(x=>x.trim()).filter(x=>x.startsWith(cookieName+'='));
  if (values.length !== 1) return null;
  const value = values[0].slice(cookieName.length+1);
  return /^[a-f0-9]{64}$/.test(value) ? value : null;
}
async function evidence(env, session, contextHash) {
  const signal=AbortSignal.timeout(2000);
  const timeout=new Promise((_,reject)=>signal.addEventListener('abort',()=>reject(new Error('timeout')),{once:true}));
  const r = await Promise.race([env.PURCHASE_EVIDENCE.fetch('https://purchase-evidence.internal/confirmed', {
    method:'POST', headers:{'content-type':'application/json'}, body:JSON.stringify({session_id:session,context_hash:contextHash}), signal,
  }),timeout]);
  if (!r.ok || Number(r.headers.get('content-length') || '0') > 4096) throw new Error('unavailable');
  const text = await boundedText(r,4096);
  const x = JSON.parse(text);
  if (x.store !== env.REVIEWS_STORE_KEY || x.environment !== 'test' || x.session_id !== session || x.context_hash !== contextHash ||
      x.confirmed !== true ||
      x.source_attested !== true || !safeId.test(x.event_id || '') || !safeId.test(x.acquisition_id || '') ||
      !safeId.test(x.source_provenance || '') || !/^[a-f0-9]{64}$/.test(x.recipient_context_hash || '') ||
      !Number.isSafeInteger(x.binding_version) || x.binding_version < 1) throw new Error('unavailable');
  return x;
}
/** Route only from the first-party storefront gateway. The trusted checkout service
 * supplies the session; the browser cannot choose it through this endpoint. */
export async function checkoutWithContext(request, env) {
  if (!env.CHECKOUT?.fetch) return unavailable();
  const result = await env.CHECKOUT.fetch(request);
  // CartRay owns checkout authorization/CORS. Optional context configuration
  // must never change or prevent the checkout response.
  if (!result.ok || !configured(env) || request.method !== 'POST' || request.headers.get('Origin') !== env.STOREFRONT_ORIGIN) return result;
  try {
    const clone = result.clone();
    if (Number(clone.headers.get('content-length') || '0') > 16384) return result;
    const text = await boundedText(clone,16384);
    const data = JSON.parse(text);
    if (!sessionId.test(data.session_id || '')) return result;
    const raw = token(), hash = await digest(raw), now = Math.floor(Date.now()/1000);
    const insertion = env.CONTEXT_DB.prepare('INSERT INTO purchaser_contexts(token_hash,checkout_session_id,created_at,expires_at) VALUES(?,?,?,?)')
      .bind(hash,data.session_id,now,now+7200).run();
    // Checkout must finish even if its optional context database stalls. A late
    // insertion is harmless: without the cookie, its random token is unknown.
    let timeoutId;
    const timeout = new Promise((_,reject)=>{timeoutId=setTimeout(()=>reject(new Error('timeout')),750);});
    try { await Promise.race([insertion,timeout]); } finally { clearTimeout(timeoutId); }
    const headers = new Headers(result.headers);
    headers.append('Set-Cookie', `${cookieName}=${raw}; Path=/; Max-Age=7200; Secure; HttpOnly; SameSite=Lax`);
    headers.set('Cache-Control','no-store');
    return new Response(result.body,{status:result.status,statusText:result.statusText,headers});
  } catch { return result; }
}
/** Expose ONLY by a private service binding from the existing consent bridge. */
export async function authority(request, env) {
  if (!configured(env) || request.method !== 'POST') return unavailable();
  const path = new URL(request.url).pathname;
  if (!['/review-consent/v1/context','/review-consent/v1/challenge','/review-consent/v1/verify','/review-consent/v1/grant'].includes(path)) return unavailable();
  try {
    if ((request.headers.get('content-type') || '') !== 'application/json') return unavailable();
    const body = await boundedText(request,128);
    let input; try { input=JSON.parse(body); } catch { return unavailable(); }
    const action=path.split('/').pop();
    if (!input || Array.isArray(input) || (action==='grant' ? Object.keys(input).length!==1 || input.accept!==true :
      action==='verify' ? Object.keys(input).length!==1 || !/^[0-9]{6}$/.test(input.code || '') : Object.keys(input).length!==0)) return unavailable();
    const raw = requestToken(request);
    if (!raw) return unavailable();
    const now = Math.floor(Date.now()/1000);
    const contextHash = await digest(raw);
    const context = await env.CONTEXT_DB.prepare('SELECT checkout_session_id FROM purchaser_contexts WHERE token_hash=? AND expires_at>?')
      .bind(contextHash,now).first();
    if (!context || !sessionId.test(context.checkout_session_id)) return unavailable();
    const purchase = await evidence(env,context.checkout_session_id,contextHash);
    if (!env.PURCHASE_EMAIL_TRANSPORT?.fetch || typeof env.PURCHASE_CODE_KEY !== 'string' || env.PURCHASE_CODE_KEY.length < 32) return unavailable();
    const challenge=await env.CONTEXT_DB.prepare('SELECT event_id,recipient_context_hash,binding_version,delivery_state,verified_at,expires_at FROM purchaser_email_challenges WHERE token_hash=?').bind(contextHash).first();
    const matches=challenge && challenge.event_id===purchase.event_id && challenge.recipient_context_hash===purchase.recipient_context_hash && challenge.binding_version===purchase.binding_version;
    if (action==='context') return Response.json({available:true,verified:!!(matches && challenge.verified_at && challenge.expires_at>now)},{headers:{'Cache-Control':'no-store'}});
    if (action==='challenge') {
      if (!safeId.test(purchase.recipient_reference || '')) return unavailable();
      if (challenge) return matches && challenge.delivery_state==='sent' && challenge.expires_at>now ? Response.json({sent:true}) : unavailable();
      const code=generateCode(), codeHash=await codeDigest(env.PURCHASE_CODE_KEY,contextHash,code);
      const claimed=await env.CONTEXT_DB.prepare("INSERT INTO purchaser_email_challenges(token_hash,event_id,recipient_context_hash,binding_version,code_digest,expires_at,delivery_state) VALUES(?,?,?,?,?,?,'claimed') ON CONFLICT(token_hash) DO NOTHING RETURNING token_hash")
        .bind(contextHash,purchase.event_id,purchase.recipient_context_hash,purchase.binding_version,codeHash,now+600).first();
      if (!claimed) return unavailable();
      try {
        const signal=AbortSignal.timeout(2000);
        const timeout=new Promise((_,reject)=>signal.addEventListener('abort',()=>reject(new Error('timeout')),{once:true}));
        const delivered=await Promise.race([env.PURCHASE_EMAIL_TRANSPORT.fetch('https://email-possession.internal/send',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({recipient_reference:purchase.recipient_reference,code,context_hash:contextHash}),signal}),timeout]);
        if (!delivered.ok) throw new Error('uncertain');
        if(delivered.body) void delivered.body.cancel().catch(()=>{});
        await env.CONTEXT_DB.prepare("UPDATE purchaser_email_challenges SET delivery_state='sent' WHERE token_hash=? AND delivery_state='claimed'").bind(contextHash).run();
        return Response.json({sent:true});
      } catch {
        await env.CONTEXT_DB.prepare("UPDATE purchaser_email_challenges SET delivery_state='uncertain' WHERE token_hash=? AND delivery_state='claimed'").bind(contextHash).run();
        return unavailable();
      }
    }
    if (!matches || challenge.delivery_state!=='sent' || challenge.expires_at<=now) return unavailable();
    if (action==='verify') {
      if (challenge.verified_at) return Response.json({verified:true});
      const codeHash=await codeDigest(env.PURCHASE_CODE_KEY,contextHash,input.code);
      const verification=await env.CONTEXT_DB.prepare('UPDATE purchaser_email_challenges SET attempts=attempts+1,verified_at=CASE WHEN code_digest=? THEN ? ELSE NULL END WHERE token_hash=? AND verified_at IS NULL AND attempts<5 AND expires_at>? AND delivery_state=\'sent\' RETURNING verified_at')
        .bind(codeHash,now,contextHash,now).first();
      return verification?.verified_at ? Response.json({verified:true}) : unavailable();
    }
    if (!challenge.verified_at) return unavailable();
    const reference = 'grant_'+token();
    await env.CONTEXT_DB.prepare('INSERT INTO purchaser_consent_grants(event_id,acquisition_id,source_provenance,recipient_context_hash,binding_version,evidence_reference,granted_at) VALUES(?,?,?,?,?,?,?) ON CONFLICT(event_id) DO NOTHING')
      .bind(purchase.event_id,purchase.acquisition_id,purchase.source_provenance,purchase.recipient_context_hash,purchase.binding_version,reference,now).run();
    const grant = await env.CONTEXT_DB.prepare('SELECT acquisition_id,source_provenance,recipient_context_hash,binding_version,evidence_reference,granted_at FROM purchaser_consent_grants WHERE event_id=?').bind(purchase.event_id).first();
    if (!grant || grant.acquisition_id !== purchase.acquisition_id || grant.source_provenance !== purchase.source_provenance ||
        grant.recipient_context_hash !== purchase.recipient_context_hash || grant.binding_version !== purchase.binding_version) return unavailable();
    return Response.json({store:env.REVIEWS_STORE_KEY,environment:'test',purpose:'review_invitation',decision:'granted',event_id:purchase.event_id,
      recipient_context_hash:grant.recipient_context_hash,evidence_reference:grant.evidence_reference,granted_at:grant.granted_at},{headers:{'Cache-Control':'no-store'}});
  } catch { return unavailable(); }
}
export default {fetch:authority};
