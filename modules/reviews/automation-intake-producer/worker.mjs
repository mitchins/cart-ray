/** Test-only source verifier and bounded outbox producer. No mail or collection. */
const encoder = new TextEncoder();
const id = /^[A-Za-z0-9_-]{1,128}$/;
const uuid = /^[a-f0-9-]{36}$/;
const hex = /^[0-9a-f]{64}$/;
const token = value => typeof value === 'string' && value.length >= 32;
const bytes = value => Uint8Array.from(value.match(/../g), x => parseInt(x,16));
async function jsonResponse(response) {
  if (!response.ok || !response.body) throw new Error('evidence unavailable');
  const reader=response.body.getReader(); let size=0, text=''; const decoder=new TextDecoder();
  try { for (;;) { const x=await reader.read(); if (x.done) break; size+=x.value.byteLength;
    if (size>65536) throw new Error('evidence too large'); text+=decoder.decode(x.value,{stream:true}); }
    return JSON.parse(text+decoder.decode());
  } finally { void reader.cancel().catch(()=>{}); }
}
async function bounded(fn) {
  const controller=new AbortController(); let timer;
  try { return await Promise.race([fn(controller.signal), new Promise((_,reject)=>{
    timer=setTimeout(()=>reject(new Error('evidence timeout')),3000);
  })]); } finally {clearTimeout(timer);controller.abort();}
}
async function cf(env, path, signal) {
  const response=await fetch(`https://api.cloudflare.com/client/v4/accounts/${env.CF_ACCOUNT_ID}/workers/scripts/${path}`,
    {headers:{authorization:`Bearer ${env.CF_READ_TOKEN}`},signal});
  const data=await jsonResponse(response); if (data.success !== true) throw new Error('control plane unavailable');
  return data.result;
}
function dbBinding(version, binding, expected) {
  const found=version?.resources?.bindings?.filter(x=>x.type==='d1' && x.name===binding);
  return found?.length===1 && found[0].database_id===expected;
}
async function activeVersion(env, script, signal) {
  const data=await cf(env,`${script}/deployments`,signal);
  const deployments=Array.isArray(data) ? data : data?.deployments;
  if (!Array.isArray(deployments) || !deployments.length) throw new Error('deployment unavailable');
  const latest=[...deployments].sort((a,b)=>String(b.created_on).localeCompare(String(a.created_on)))[0];
  if (latest?.versions?.length!==1 || latest.versions[0].percentage!==100) throw new Error('ambiguous deployment');
  const version=latest.versions[0].version_id;
  if (!uuid.test(version)) throw new Error('invalid version');
  const details=await cf(env,`${script}/versions/${version}`,signal);
  if (details.id!==version) throw new Error('version mismatch');
  return details;
}
function binding(version, name, type, value) {
  const found=version?.resources?.bindings?.filter(x=>x.name===name && x.type===type);
  return found?.length===1 && (value === undefined || found[0].text===value);
}
function serviceBinding(version, name, service) {
  const found=version?.resources?.bindings?.filter(x=>x.name===name && x.type==='service');
  return found?.length===1 && found[0].service===service;
}
export async function attest(env) {
  if (env.REVIEWS_ENVIRONMENT!=='test' || env.REVIEWS_SOURCE_PRODUCER!=='test' ||
      ![env.SOURCE_WORKER,env.PRODUCER_WORKER,env.REVIEWS_WORKER,env.SOURCE_STORE,env.SOURCE_ISSUER,env.SOURCE_INSTALLATION].every(x=>typeof x==='string' && id.test(x)) ||
      !/^[a-f0-9]{32}$/.test(env.CF_ACCOUNT_ID||'') || !uuid.test(env.SOURCE_DATABASE_ID||'') ||
      !/^acct_[A-Za-z0-9]+$/.test(env.STRIPE_ACCOUNT_ID||'') || !/^sha256:[a-f0-9]{64}$/.test(env.CATALOGUE_VERSION||'') ||
      !hex.test(env.MAPPING_SHA256||'') || !hex.test(env.SOURCE_EVENT_KEY||'') ||
      !token(env.CF_READ_TOKEN) || !token(env.SOURCE_IDENTITY_TOKEN) || !token(env.REVIEWS_ACTIVATION_TOKEN) || !token(env.TRIGGER_TOKEN) ||
      new Set([env.CF_READ_TOKEN,env.SOURCE_IDENTITY_TOKEN,env.REVIEWS_ACTIVATION_TOKEN,env.SOURCE_EVENT_KEY,env.TRIGGER_TOKEN]).size!==5 ||
      !env.SOURCE_API || !env.REVIEWS_API || !env.COMMERCE_DB || !env.EVENT_QUEUE || !uuid.test(env.CF_VERSION_METADATA?.id||'')) throw new Error('source unavailable');
  return bounded(async signal=>{
    const [source,producer,reviews,identity]=await Promise.all([
      activeVersion(env,env.SOURCE_WORKER,signal), activeVersion(env,env.PRODUCER_WORKER,signal),
      activeVersion(env,env.REVIEWS_WORKER,signal),
      env.SOURCE_API.fetch('https://source.internal/internal/reviews-source-identity',{
        headers:{authorization:`Bearer ${env.SOURCE_IDENTITY_TOKEN}`}, signal}).then(jsonResponse),
    ]);
    if (producer.id!==env.CF_VERSION_METADATA.id || identity.version_id!==source.id ||
        identity.stripe_account!==env.STRIPE_ACCOUNT_ID || identity.catalogue_version!==env.CATALOGUE_VERSION ||
        !dbBinding(source,'DB',env.SOURCE_DATABASE_ID) || !dbBinding(producer,'COMMERCE_DB',env.SOURCE_DATABASE_ID) ||
        !dbBinding(reviews,'CARTRAY_DB',env.SOURCE_DATABASE_ID) ||
        !serviceBinding(producer,'SOURCE_API',env.SOURCE_WORKER) ||
        !serviceBinding(producer,'REVIEWS_API',env.REVIEWS_WORKER) ||
        !serviceBinding(source,'REVIEWS_SOURCE_TRIGGER',env.PRODUCER_WORKER) ||
        !binding(source,'CARTRAY_ENVIRONMENT','plain_text','test') ||
        !binding(source,'CARTRAY_REVIEWS_SOURCE','plain_text','test') ||
        !binding(source,'CARTRAY_REVIEWS_PRODUCER','plain_text','test') ||
        !binding(source,'CF_VERSION_METADATA','version_metadata') ||
        !binding(reviews,'REVIEWS_ENVIRONMENT','plain_text','test') ||
        !binding(reviews,'REVIEWS_MODE','plain_text','verified_acquisition_only') ||
        !binding(reviews,'REVIEWS_AUTOMATION_INTAKE','plain_text','test') ||
        !binding(reviews,'REVIEWS_STORE_KEY','plain_text',env.SOURCE_STORE)) throw new Error('source identity mismatch');
    const mapping=reviews.resources.bindings.find(x=>x.name==='REVIEWS_SUBJECT_MAPS' && x.type==='plain_text')?.text;
    if (typeof mapping!=='string') throw new Error('mapping unavailable');
    const digest=Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',encoder.encode(mapping))),x=>x.toString(16).padStart(2,'0')).join('');
    if (digest!==env.MAPPING_SHA256 || !JSON.parse(mapping)?.[env.CATALOGUE_VERSION]) throw new Error('mapping mismatch');
    return {source_version:source.id,producer_version:producer.id,reviews_version:reviews.id};
  });
}
async function activate(env) {
  const result=await bounded(signal=>env.REVIEWS_API.fetch('https://reviews.internal/reviews-internal/v1/automation-activate',{
    method:'POST',headers:{authorization:`Bearer ${env.REVIEWS_ACTIVATION_TOKEN}`,'content-type':'application/json'},
    body:JSON.stringify({policy_version:env.POLICY_VERSION,delay_seconds:env.DELAY_SECONDS,max_lateness:env.MAX_LATENESS}),signal,
  }).then(jsonResponse));
  if (result?.status!=='active' || !Number.isSafeInteger(result.outbox_watermark) || result.outbox_watermark<0) throw new Error('activation unavailable');
  return result.outbox_watermark;
}
export async function relay(env) {
  await attest(env);
  if (!id.test(env.POLICY_VERSION||'') || !Number.isSafeInteger(env.DELAY_SECONDS) || env.DELAY_SECONDS<0 ||
      !Number.isSafeInteger(env.MAX_LATENESS) || env.MAX_LATENESS<0) throw new Error('policy unavailable');
  const watermark=await activate(env), db=env.COMMERCE_DB.withSession('first-primary');
  await db.prepare('INSERT OR IGNORE INTO reviews_subscriber_checkpoint VALUES (?,?,?,?,?,?,?,?)')
    .bind(env.SOURCE_ISSUER,env.SOURCE_INSTALLATION,env.SOURCE_DATABASE_ID,env.STRIPE_ACCOUNT_ID,env.CATALOGUE_VERSION,env.MAPPING_SHA256,watermark,watermark).run();
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
export default {
  async fetch(request,env) {
    const supplied=request.headers.get('authorization')||'';
    if (request.method!=='POST' || !token(env.TRIGGER_TOKEN) || supplied!==`Bearer ${env.TRIGGER_TOKEN}`)
      return new Response(null,{status:404});
    try { await relay(env); return new Response(null,{status:204}); }
    catch { return new Response(null,{status:503}); }
  },
  async scheduled(_event,env,ctx) {ctx.waitUntil(relay(env));},
};
