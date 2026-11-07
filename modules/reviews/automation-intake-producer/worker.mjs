/** Test-only source verifier and bounded outbox producer. No mail or collection. */
const encoder = new TextEncoder();
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
async function cf(env, path, signal, fetchApi) {
  const response=await fetchApi(`https://api.cloudflare.com/client/v4/accounts/${env.CF_ACCOUNT_ID}/workers/scripts/${path}`,
    {headers:{authorization:`Bearer ${env.CF_READ_TOKEN}`},signal});
  const data=await jsonResponse(response,signal); if (data.success !== true) throw new Error('control plane unavailable');
  return data.result;
}
async function queueInfo(env, queueId, signal, fetchApi) {
  const response=await fetchApi(`https://api.cloudflare.com/client/v4/accounts/${env.CF_ACCOUNT_ID}/queues/${queueId}`,
    {headers:{authorization:`Bearer ${env.CF_READ_TOKEN}`},signal});
  const data=await jsonResponse(response,signal); if (data.success!==true) throw new Error('queue unavailable');
  return data.result;
}
function dbBinding(version, binding, expected) {
  const found=version?.resources?.bindings?.filter(x=>x.type==='d1' && x.name===binding);
  return found?.length===1 && found[0].database_id===expected;
}
async function activeVersion(env, script, signal, fetchApi) {
  const data=await cf(env,`${script}/deployments`,signal,fetchApi);
  const deployments=Array.isArray(data) ? data : data?.deployments;
  if (!Array.isArray(deployments) || !deployments.length) throw new Error('deployment unavailable');
  const latest=[...deployments].sort((a,b)=>String(b.created_on).localeCompare(String(a.created_on)))[0];
  if (latest?.versions?.length!==1 || latest.versions[0].percentage!==100) throw new Error('ambiguous deployment');
  const version=latest.versions[0].version_id;
  if (!uuid.test(version)) throw new Error('invalid version');
  const details=await cf(env,`${script}/versions/${version}`,signal,fetchApi);
  if (details.id!==version) throw new Error('version mismatch');
  return details;
}
function binding(version, name, type, value) {
  const found=version?.resources?.bindings?.filter(x=>x.name===name && x.type===type);
  return found?.length===1 && (value === undefined || found[0].text===value);
}
function serviceBinding(version, name, service) {
  const found=version?.resources?.bindings?.filter(x=>x.name===name && x.type==='service');
  // A named environment or entrypoint can route to a different target than
  // the default deployment whose active version was checked below.
  return found?.length===1 && found[0].service===service &&
    (found[0].environment === undefined || found[0].environment === '') &&
    (found[0].entrypoint === undefined || found[0].entrypoint === '');
}
export async function attest(env, fetchApi=fetch) {
  if (env.REVIEWS_ENVIRONMENT!=='test' || env.REVIEWS_SOURCE_PRODUCER!=='test' ||
      ![env.SOURCE_WORKER,env.PRODUCER_WORKER,env.REVIEWS_WORKER,env.CONSUMER_WORKER,env.EVENT_QUEUE_NAME,env.DEAD_LETTER_QUEUE_NAME,env.SOURCE_STORE,env.SOURCE_ISSUER,env.SOURCE_INSTALLATION].every(x=>typeof x==='string' && id.test(x)) ||
      new Set([env.SOURCE_WORKER,env.PRODUCER_WORKER,env.REVIEWS_WORKER,env.CONSUMER_WORKER]).size!==4 ||
      !/^[a-f0-9]{32}$/.test(env.CF_ACCOUNT_ID||'') || !uuid.test(env.SOURCE_DATABASE_ID||'') ||
      !/^[a-f0-9]{32}$/.test(env.EVENT_QUEUE_ID||'') ||
      !/^[a-f0-9]{32}$/.test(env.DEAD_LETTER_QUEUE_ID||'') || env.DEAD_LETTER_QUEUE_ID===env.EVENT_QUEUE_ID ||
      env.DEAD_LETTER_QUEUE_NAME===env.EVENT_QUEUE_NAME ||
      !/^acct_[A-Za-z0-9]+$/.test(env.STRIPE_ACCOUNT_ID||'') || !/^sha256:[a-f0-9]{64}$/.test(env.CATALOGUE_VERSION||'') ||
      !hex.test(env.MAPPING_SHA256||'') || !hex.test(env.SOURCE_EVENT_KEY||'') ||
      !token(env.CF_READ_TOKEN) || !token(env.SOURCE_IDENTITY_TOKEN) || !token(env.REVIEWS_ACTIVATION_TOKEN) || !token(env.TRIGGER_TOKEN) ||
      new Set([env.CF_READ_TOKEN,env.SOURCE_IDENTITY_TOKEN,env.REVIEWS_ACTIVATION_TOKEN,env.SOURCE_EVENT_KEY,env.TRIGGER_TOKEN]).size!==5 ||
      !env.SOURCE_API || !env.REVIEWS_API || !env.COMMERCE_DB || !env.EVENT_QUEUE || !uuid.test(env.CF_VERSION_METADATA?.id||'')) throw new Error('source unavailable');
  return bounded(async signal=>{
    const [source,producer,reviews,consumer,queue,dlq,identity]=await Promise.all([
      activeVersion(env,env.SOURCE_WORKER,signal,fetchApi), activeVersion(env,env.PRODUCER_WORKER,signal,fetchApi),
      activeVersion(env,env.REVIEWS_WORKER,signal,fetchApi),
      activeVersion(env,env.CONSUMER_WORKER,signal,fetchApi), queueInfo(env,env.EVENT_QUEUE_ID,signal,fetchApi),
      queueInfo(env,env.DEAD_LETTER_QUEUE_ID,signal,fetchApi),
      env.SOURCE_API.fetch('https://source.internal/internal/reviews-source-identity',{
        headers:{authorization:`Bearer ${env.SOURCE_IDENTITY_TOKEN}`}, signal}).then(response=>jsonResponse(response,signal)),
    ]);
    if (producer.id!==env.CF_VERSION_METADATA.id || identity.version_id!==source.id ||
        identity.stripe_account!==env.STRIPE_ACCOUNT_ID || identity.catalogue_version!==env.CATALOGUE_VERSION ||
        !dbBinding(source,'DB',env.SOURCE_DATABASE_ID) || !dbBinding(producer,'COMMERCE_DB',env.SOURCE_DATABASE_ID) ||
        !dbBinding(reviews,'CARTRAY_DB',env.SOURCE_DATABASE_ID) ||
        !serviceBinding(producer,'SOURCE_API',env.SOURCE_WORKER) ||
        !serviceBinding(producer,'REVIEWS_API',env.REVIEWS_WORKER) ||
        !serviceBinding(source,'REVIEWS_SOURCE_TRIGGER',env.PRODUCER_WORKER) ||
        !serviceBinding(consumer,'REVIEWS_API',env.REVIEWS_WORKER) ||
        !producer.resources.bindings.some(x=>x.type==='queue' && x.name==='EVENT_QUEUE' && x.queue_name===env.EVENT_QUEUE_NAME) ||
        queue?.queue_id!==env.EVENT_QUEUE_ID || queue?.queue_name!==env.EVENT_QUEUE_NAME ||
        queue?.consumers_total_count!==1 || queue?.consumers?.length!==1 ||
        queue.consumers[0].type!=='worker' || queue.consumers[0].script_name!==env.CONSUMER_WORKER ||
        queue.consumers[0].dead_letter_queue!==env.DEAD_LETTER_QUEUE_NAME ||
        !Number.isSafeInteger(queue.consumers[0].settings?.max_retries) ||
        queue.consumers[0].settings.max_retries<1 || queue.consumers[0].settings.max_retries>10 ||
        queue.settings?.delivery_paused!==false || dlq?.queue_id!==env.DEAD_LETTER_QUEUE_ID ||
        dlq?.queue_name!==env.DEAD_LETTER_QUEUE_NAME || dlq.consumers_total_count!==0 ||
        !binding(source,'CARTRAY_ENVIRONMENT','plain_text','test') ||
        !binding(source,'CARTRAY_REVIEWS_SOURCE','plain_text','test') ||
        !binding(source,'CARTRAY_REVIEWS_PRODUCER','plain_text','test') ||
        !binding(source,'CF_VERSION_METADATA','version_metadata') ||
        !binding(reviews,'REVIEWS_ENVIRONMENT','plain_text','test') ||
        !binding(reviews,'REVIEWS_MODE','plain_text','verified_acquisition_only') ||
        !binding(reviews,'REVIEWS_AUTOMATION_INTAKE','plain_text','test') ||
        !binding(reviews,'REVIEWS_STORE_KEY','plain_text',env.SOURCE_STORE) ||
        !binding(consumer,'REVIEWS_ENVIRONMENT','plain_text','test') ||
        !binding(consumer,'REVIEWS_ORDER_SUBSCRIBER','plain_text','test') ||
        !binding(consumer,'SOURCE_STORE','plain_text',env.SOURCE_STORE) ||
        !binding(consumer,'SOURCE_ISSUER','plain_text',env.SOURCE_ISSUER) ||
        !binding(consumer,'SOURCE_INSTALLATION','plain_text',env.SOURCE_INSTALLATION)) throw new Error('source identity mismatch');
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
  }).then(response=>jsonResponse(response,signal)));
  if (result?.status!=='active' || !Number.isSafeInteger(result.outbox_watermark) || result.outbox_watermark<0) throw new Error('activation unavailable');
  return result.outbox_watermark;
}
export async function relay(env, fetchApi=fetch) {
  await attest(env,fetchApi);
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
// The caller's version is a hint only. Both the active version and its actual
// bindings are independently fetched from the authenticated control plane.
export async function attestPurchase(env, input, fetchApi=fetch, now=Math.floor(Date.now()/1000)) {
  if (!id.test(env.AUTHORITY_WORKER||'') || !uuid.test(env.AUTHORITY_CONTEXT_DB_ID||'') ||
      [env.SOURCE_WORKER,env.PRODUCER_WORKER,env.REVIEWS_WORKER,env.CONSUMER_WORKER].includes(env.AUTHORITY_WORKER) ||
      env.AUTHORITY_CONTEXT_DB_ID===env.SOURCE_DATABASE_ID || !hex.test(env.PURCHASE_ATTESTATION_KEY||'') ||
      [env.SOURCE_EVENT_KEY,env.CF_READ_TOKEN,env.SOURCE_IDENTITY_TOKEN,env.REVIEWS_ACTIVATION_TOKEN,
        env.TRIGGER_TOKEN].includes(env.PURCHASE_ATTESTATION_KEY) || !token(env.PURCHASE_ATTESTATION_TOKEN) ||
      [env.CF_READ_TOKEN,env.SOURCE_IDENTITY_TOKEN,env.REVIEWS_ACTIVATION_TOKEN,env.TRIGGER_TOKEN,
        env.SOURCE_EVENT_KEY,env.PURCHASE_ATTESTATION_KEY].includes(env.PURCHASE_ATTESTATION_TOKEN) ||
      !input || Object.keys(input).sort().join()!=='authority_version,nonce' ||
      !uuid.test(input.authority_version||'') || !hex.test(input.nonce||'') || !Number.isSafeInteger(now))
    throw new Error('purchase attestation unavailable');
  const source=await attest(env,fetchApi);
  const authority=await bounded(signal=>activeVersion(env,env.AUTHORITY_WORKER,signal,fetchApi));
  if (authority.id!==input.authority_version || !dbBinding(authority,'COMMERCE_DB',env.SOURCE_DATABASE_ID) ||
      !dbBinding(authority,'CONTEXT_DB',env.AUTHORITY_CONTEXT_DB_ID) ||
      !serviceBinding(authority,'SOURCE_IDENTITY',env.SOURCE_WORKER) ||
      !serviceBinding(authority,'CHECKOUT',env.SOURCE_WORKER) ||
      !serviceBinding(authority,'ATTESTOR',env.PRODUCER_WORKER) ||
      !serviceBinding(authority,'REVIEWS_API',env.REVIEWS_WORKER) ||
      !binding(authority,'CF_VERSION_METADATA','version_metadata')) throw new Error('authority binding mismatch');
  const proof=['cartray-reviews-purchase-attestation-v1',env.SOURCE_STORE,'test',env.SOURCE_INSTALLATION,
    env.SOURCE_DATABASE_ID,env.AUTHORITY_CONTEXT_DB_ID,env.STRIPE_ACCOUNT_ID,env.CATALOGUE_VERSION,
    env.MAPPING_SHA256,source.source_version,source.reviews_version,source.producer_version,
    authority.id,input.nonce,now,now+30];
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
