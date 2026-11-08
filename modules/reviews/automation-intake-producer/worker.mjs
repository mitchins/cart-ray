/** Test-only source verifier and bounded outbox producer. No mail or collection. */
const encoder = new TextEncoder();
// Normalized sqlite_master fingerprint of the optional module migration.
// An older experimental schema must not be treated as an installed cursor.
const CHECKPOINT_SCHEMA_SHA256 = '4af9ce45f521b7e00a7b5d37063da9aed64032c71dcfd08b53045a7fb6f33c94';
const id = /^[A-Za-z0-9_-]{1,128}$/;
const uuid = /^[a-f0-9-]{36}$/;
const hex = /^[0-9a-f]{64}$/;
const token = value => typeof value === 'string' && value.length >= 32;
const bytes = value => Uint8Array.from(value.match(/../g), x => parseInt(x,16));
async function jsonResponse(response, signal) {
  if (!response.ok || !response.body) throw new Error('evidence unavailable');
  const reader=response.body.getReader(); let size=0, text=''; const decoder=new TextDecoder();
  const cancel=()=>{void reader.cancel().catch(()=>{});};
  if (signal) {
    if (signal.aborted) cancel();
    else signal.addEventListener('abort',cancel,{once:true});
  }
  try { for (;;) { const x=await reader.read(); if (x.done) break; size+=x.value.byteLength;
    if (size>65536) throw new Error('evidence too large'); text+=decoder.decode(x.value,{stream:true}); }
    return JSON.parse(text+decoder.decode());
  } finally { if (signal) signal.removeEventListener('abort',cancel); cancel(); }
}
async function bounded(fn) {
  const controller=new AbortController(); let timer;
  try { return await Promise.race([fn(controller.signal), new Promise((_,reject)=>{
    timer=setTimeout(()=>reject(new Error('evidence timeout')),3000);
  })]); } finally {clearTimeout(timer);controller.abort();}
}
// Runtime trust is anchored in the operator-verified commerce D1 record.
// No management credentials or management API calls belong in this module.
export async function attest(env) {
  if (env.REVIEWS_ENVIRONMENT!=='test' || env.REVIEWS_SOURCE_PRODUCER!=='test' ||
      ![env.SOURCE_WORKER,env.PRODUCER_WORKER,env.REVIEWS_WORKER,env.CONSUMER_WORKER,env.EVENT_QUEUE_NAME,env.DEAD_LETTER_QUEUE_NAME,env.SOURCE_STORE,env.SOURCE_ISSUER,env.SOURCE_INSTALLATION].every(x=>typeof x==='string' && id.test(x)) ||
      new Set([env.SOURCE_WORKER,env.PRODUCER_WORKER,env.REVIEWS_WORKER,env.CONSUMER_WORKER]).size!==4 ||
      !/^[a-f0-9]{32}$/.test(env.CF_ACCOUNT_ID||'') || !uuid.test(env.SOURCE_DATABASE_ID||'') ||
      !/^[a-f0-9]{32}$/.test(env.EVENT_QUEUE_ID||'') ||
      !/^[a-f0-9]{32}$/.test(env.DEAD_LETTER_QUEUE_ID||'') || env.DEAD_LETTER_QUEUE_ID===env.EVENT_QUEUE_ID ||
      env.DEAD_LETTER_QUEUE_NAME===env.EVENT_QUEUE_NAME ||
      !/^acct_[A-Za-z0-9]+$/.test(env.STRIPE_ACCOUNT_ID||'') || !/^sha256:[a-f0-9]{64}$/.test(env.CATALOGUE_VERSION||'') ||
      !hex.test(env.MAPPING_SHA256||'') || !hex.test(env.SOURCE_EVENT_KEY||'') ||
      !token(env.SOURCE_IDENTITY_TOKEN) || !token(env.REVIEWS_ACTIVATION_TOKEN) || !token(env.TRIGGER_TOKEN) ||
      new Set([env.SOURCE_IDENTITY_TOKEN,env.REVIEWS_ACTIVATION_TOKEN,env.SOURCE_EVENT_KEY,env.TRIGGER_TOKEN]).size!==4 ||
      !env.SOURCE_API || !env.REVIEWS_API || !env.CONSUMER_API || !env.COMMERCE_DB || !env.EVENT_QUEUE || !uuid.test(env.CF_VERSION_METADATA?.id||'')) throw new Error('source unavailable');
  const record=await env.COMMERCE_DB.withSession('first-primary').prepare(
    'SELECT * FROM reviews_verified_deployment WHERE subscriber_id=? AND installation_id=? ORDER BY revision DESC LIMIT 1')
    .bind(env.SOURCE_ISSUER,env.SOURCE_INSTALLATION).first();
  const matches={account_id:env.CF_ACCOUNT_ID,store_key:env.SOURCE_STORE,environment:env.REVIEWS_ENVIRONMENT,
    source_database_id:env.SOURCE_DATABASE_ID,stripe_account_id:env.STRIPE_ACCOUNT_ID,
    catalogue_version:env.CATALOGUE_VERSION,mapping_sha256:env.MAPPING_SHA256,
    producer_version:env.CF_VERSION_METADATA.id,context_database_id:env.AUTHORITY_CONTEXT_DB_ID??null,
    event_queue_id:env.EVENT_QUEUE_ID,dead_letter_queue_id:env.DEAD_LETTER_QUEUE_ID,
    source_worker:env.SOURCE_WORKER,producer_worker:env.PRODUCER_WORKER,reviews_worker:env.REVIEWS_WORKER,
    consumer_worker:env.CONSUMER_WORKER,authority_worker:env.AUTHORITY_WORKER??null,
    event_queue_name:env.EVENT_QUEUE_NAME,dead_letter_queue_name:env.DEAD_LETTER_QUEUE_NAME};
  if (!record || !['verified','active'].includes(record.activation_status) || !Number.isSafeInteger(record.revision) || record.revision<1 ||
      !Number.isSafeInteger(record.verified_at) || record.verified_at<=0 ||
      !['source_version','producer_version','reviews_version','consumer_version'].every(k=>uuid.test(record[k]||'')) ||
      (env.AUTHORITY_WORKER!==undefined ? !uuid.test(record.authority_version||'') : record.authority_version!==null) ||
      Object.entries(matches).some(([k,v])=>(typeof v!=='string' && v!==null) || record[k]!==v)) throw new Error('verification record mismatch');
  const [identity,reviewsIdentity,consumerIdentity]=await bounded(signal=>Promise.all([
    env.SOURCE_API.fetch('https://source.internal/internal/reviews-runtime-identity',{headers:{authorization:`Bearer ${env.SOURCE_IDENTITY_TOKEN}`},signal}).then(r=>jsonResponse(r,signal)),
    env.REVIEWS_API.fetch('https://reviews.internal/reviews-internal/v1/runtime-identity',{headers:{authorization:`Bearer ${env.REVIEWS_ACTIVATION_TOKEN}`},signal}).then(r=>jsonResponse(r,signal)),
    env.CONSUMER_API.fetch('https://consumer.internal/runtime-identity',{headers:{authorization:`Bearer ${env.SOURCE_EVENT_KEY}`},signal}).then(r=>jsonResponse(r,signal)),
  ]));
  if (identity.version_id!==record.source_version || identity.catalogue_version!==record.catalogue_version ||
      reviewsIdentity.version_id!==record.reviews_version || consumerIdentity.version_id!==record.consumer_version)
    throw new Error('runtime identity mismatch');
  return record;
}
async function activate(env) {
  const result=await bounded(signal=>env.REVIEWS_API.fetch('https://reviews.internal/reviews-internal/v1/automation-activate',{
    method:'POST',headers:{authorization:`Bearer ${env.REVIEWS_ACTIVATION_TOKEN}`,'content-type':'application/json'},
    body:JSON.stringify({policy_version:env.POLICY_VERSION,delay_seconds:env.DELAY_SECONDS,max_lateness:env.MAX_LATENESS}),signal,
  }).then(response=>jsonResponse(response,signal)));
  if (result?.status!=='active' || !Number.isSafeInteger(result.outbox_watermark) || result.outbox_watermark<0) throw new Error('activation unavailable');
  return result.outbox_watermark;
}
export async function relay(env, fetchApi=fetch) {
  await attest(env,fetchApi);
  if (!id.test(env.POLICY_VERSION||'') || !Number.isSafeInteger(env.DELAY_SECONDS) || env.DELAY_SECONDS<0 ||
      !Number.isSafeInteger(env.MAX_LATENESS) || env.MAX_LATENESS<0) throw new Error('policy unavailable');
  const db=env.COMMERCE_DB.withSession('first-primary');
  const schema=await db.prepare("SELECT name,sql FROM sqlite_master WHERE (type='table' AND name='reviews_subscriber_checkpoint') OR (type='trigger' AND tbl_name='reviews_subscriber_checkpoint') ORDER BY name").all();
  if (!Array.isArray(schema.results) || schema.results.length!==4 || schema.results.some(x=>typeof x.name!=='string' || typeof x.sql!=='string'))
    throw new Error('checkpoint schema unavailable');
  const normalized=schema.results.map(x=>[x.name,x.sql.trim().replace(/\s+/g,' ')]);
  const schemaHash=Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',encoder.encode(JSON.stringify(normalized)))),x=>x.toString(16).padStart(2,'0')).join('');
  if (schemaHash!==CHECKPOINT_SCHEMA_SHA256) throw new Error('checkpoint schema mismatch');
  const watermark=await activate(env);
  await db.prepare('INSERT INTO reviews_subscriber_checkpoint SELECT ?,?,?,?,?,?,?,? WHERE NOT EXISTS (SELECT 1 FROM reviews_subscriber_checkpoint WHERE subscriber_id=?)')
    .bind(env.SOURCE_ISSUER,env.SOURCE_INSTALLATION,env.SOURCE_DATABASE_ID,env.STRIPE_ACCOUNT_ID,env.CATALOGUE_VERSION,env.MAPPING_SHA256,watermark,watermark,env.SOURCE_ISSUER).run();
  let row=await db.prepare('SELECT installation_id,source_database_id,stripe_account_id,catalogue_version,mapping_sha256,activation_watermark,last_outbox_id FROM reviews_subscriber_checkpoint WHERE subscriber_id=?')
    .bind(env.SOURCE_ISSUER).first();
  if (!row || row.installation_id!==env.SOURCE_INSTALLATION || row.source_database_id!==env.SOURCE_DATABASE_ID ||
      row.stripe_account_id!==env.STRIPE_ACCOUNT_ID || row.catalogue_version!==env.CATALOGUE_VERSION ||
      row.mapping_sha256!==env.MAPPING_SHA256 || row.activation_watermark!==watermark) throw new Error('checkpoint mismatch');
  const found=await db.prepare("SELECT id FROM outbox WHERE id>? AND event_type='OrderConfirmed' ORDER BY id LIMIT 20")
    .bind(row.last_outbox_id).all();
  for (const item of found.results) {
    if (!Number.isSafeInteger(item.id) || item.id<=row.last_outbox_id) throw new Error('invalid outbox');
    const payload=JSON.stringify(['cartray-reviews-order-confirmed-v1',env.SOURCE_ISSUER,env.SOURCE_INSTALLATION,env.SOURCE_STORE,'test',item.id]);
    const k=await crypto.subtle.importKey('raw',bytes(env.SOURCE_EVENT_KEY),{name:'HMAC',hash:'SHA-256'},false,['sign']);
    const signature=Array.from(new Uint8Array(await crypto.subtle.sign('HMAC',k,encoder.encode(payload))),x=>x.toString(16).padStart(2,'0')).join('');
    await bounded(signal=>env.EVENT_QUEUE.send({version:1,issuer:env.SOURCE_ISSUER,installation:env.SOURCE_INSTALLATION,
      store:env.SOURCE_STORE,environment:'test',outbox_id:item.id,signature},{signal}));
    const advanced=await db.prepare('UPDATE reviews_subscriber_checkpoint SET last_outbox_id=? WHERE subscriber_id=? AND last_outbox_id=? RETURNING last_outbox_id')
      .bind(item.id,env.SOURCE_ISSUER,row.last_outbox_id).all();
    if (advanced.results.length!==1) break; // Concurrent relay: never skip a source row.
    row.last_outbox_id=item.id;
  }
  return {last_outbox_id:row.last_outbox_id};
}
// The caller reports its runtime version; it must match the operator-verified
// authority version in commerce D1 before a nonce-bound proof is issued.
export async function attestPurchase(env, input, fetchApi=fetch, now=Math.floor(Date.now()/1000)) {
  if (!id.test(env.AUTHORITY_WORKER||'') || !uuid.test(env.AUTHORITY_CONTEXT_DB_ID||'') ||
      [env.SOURCE_WORKER,env.PRODUCER_WORKER,env.REVIEWS_WORKER,env.CONSUMER_WORKER].includes(env.AUTHORITY_WORKER) ||
      env.AUTHORITY_CONTEXT_DB_ID===env.SOURCE_DATABASE_ID || !hex.test(env.PURCHASE_ATTESTATION_KEY||'') ||
      [env.SOURCE_EVENT_KEY,env.SOURCE_IDENTITY_TOKEN,env.REVIEWS_ACTIVATION_TOKEN,
        env.TRIGGER_TOKEN].includes(env.PURCHASE_ATTESTATION_KEY) || !token(env.PURCHASE_ATTESTATION_TOKEN) ||
      [env.SOURCE_IDENTITY_TOKEN,env.REVIEWS_ACTIVATION_TOKEN,env.TRIGGER_TOKEN,
        env.SOURCE_EVENT_KEY,env.PURCHASE_ATTESTATION_KEY].includes(env.PURCHASE_ATTESTATION_TOKEN) ||
      !input || Object.keys(input).length!==2 || !Object.hasOwn(input,'authority_version') || !Object.hasOwn(input,'nonce') ||
      !uuid.test(input.authority_version||'') || !hex.test(input.nonce||'') || !Number.isSafeInteger(now))
    throw new Error('purchase attestation unavailable');
  const source=await attest(env,fetchApi);
  if (source.authority_version!==input.authority_version) throw new Error('authority version mismatch');
  const proof=['cartray-reviews-purchase-attestation-v1',env.SOURCE_STORE,'test',env.SOURCE_INSTALLATION,
    env.SOURCE_DATABASE_ID,env.AUTHORITY_CONTEXT_DB_ID,env.STRIPE_ACCOUNT_ID,env.CATALOGUE_VERSION,
    env.MAPPING_SHA256,source.source_version,source.reviews_version,source.producer_version,
    source.authority_version,input.nonce,now,now+30];
  const key=await crypto.subtle.importKey('raw',bytes(env.PURCHASE_ATTESTATION_KEY),{name:'HMAC',hash:'SHA-256'},false,['sign']);
  const signature=Array.from(new Uint8Array(await crypto.subtle.sign('HMAC',key,encoder.encode(JSON.stringify(proof)))),x=>x.toString(16).padStart(2,'0')).join('');
  return {proof,signature};
}
export default {
  async fetch(request,env) {
    if (new URL(request.url).pathname==='/attest-purchase') {
      const supplied=request.headers.get('authorization')||'';
      if (request.method!=='POST' || !token(env.PURCHASE_ATTESTATION_TOKEN) || supplied!==`Bearer ${env.PURCHASE_ATTESTATION_TOKEN}` ||
          !request.body) return new Response(null,{status:404});
      try {
        const input=await bounded(signal=>jsonResponse(new Response(request.body),signal));
        return Response.json(await attestPurchase(env,input),{headers:{'cache-control':'no-store'}});
      } catch { return new Response(null,{status:503}); }
    }
    const supplied=request.headers.get('authorization')||'';
    if (request.method!=='POST' || !token(env.TRIGGER_TOKEN) || supplied!==`Bearer ${env.TRIGGER_TOKEN}`)
      return new Response(null,{status:404});
    try { await relay(env); return new Response(null,{status:204}); }
    catch { return new Response(null,{status:503}); }
  },
  async scheduled(_event,env,ctx) {ctx.waitUntil(relay(env));},
};
