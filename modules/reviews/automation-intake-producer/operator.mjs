import {jsonResponse,bounded} from './evidence.mjs';
/** Test-only source verifier and bounded outbox producer. No mail or collection. */
const encoder = new TextEncoder();
const id = /^[A-Za-z0-9_-]{1,128}$/;
const uuid = /^[a-f0-9-]{36}$/;
const hex = /^[0-9a-f]{64}$/;
const token = value => typeof value === 'string' && value.length >= 32;
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
  // Cloudflare reports the default deployment as `production` in version
  // metadata. Other named environments and entrypoints may route elsewhere.
  return found?.length===1 && found[0].service===service &&
    (found[0].environment === undefined || found[0].environment === '' || found[0].environment === 'production') &&
    (found[0].entrypoint === undefined || found[0].entrypoint === '');
}
// Cloudflare has returned both `script_name` (documented) and `script`
// (observed). If both appear they must agree; unknown or missing values fail.
function consumerScriptMatches(consumer, expected) {
  const names=[consumer?.script_name,consumer?.script].filter(value=>value!==undefined);
  return names.length>0 && names.every(value=>value===expected);
}
export async function inspectDeployment(env, fetchApi=fetch) {
  if (env.REVIEWS_ENVIRONMENT!=='test' || env.REVIEWS_SOURCE_PRODUCER!=='test' ||
      ![env.SOURCE_WORKER,env.PRODUCER_WORKER,env.REVIEWS_WORKER,env.CONSUMER_WORKER,env.EVENT_QUEUE_NAME,env.DEAD_LETTER_QUEUE_NAME,env.SOURCE_STORE,env.SOURCE_ISSUER,env.SOURCE_INSTALLATION].every(x=>typeof x==='string' && id.test(x)) ||
      new Set([env.SOURCE_WORKER,env.PRODUCER_WORKER,env.REVIEWS_WORKER,env.CONSUMER_WORKER]).size!==4 ||
      !/^[a-f0-9]{32}$/.test(env.CF_ACCOUNT_ID||'') || !uuid.test(env.SOURCE_DATABASE_ID||'') ||
      !/^[a-f0-9]{32}$/.test(env.EVENT_QUEUE_ID||'') ||
      !/^[a-f0-9]{32}$/.test(env.DEAD_LETTER_QUEUE_ID||'') || env.DEAD_LETTER_QUEUE_ID===env.EVENT_QUEUE_ID ||
      env.DEAD_LETTER_QUEUE_NAME===env.EVENT_QUEUE_NAME ||
      !/^acct_[A-Za-z0-9]+$/.test(env.STRIPE_ACCOUNT_ID||'') || !/^sha256:[a-f0-9]{64}$/.test(env.CATALOGUE_VERSION||'') ||
      !hex.test(env.MAPPING_SHA256||'') ||
      !token(env.CF_READ_TOKEN) || !token(env.SOURCE_IDENTITY_TOKEN) ||
      env.CF_READ_TOKEN===env.SOURCE_IDENTITY_TOKEN ||
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
        !serviceBinding(producer,'CONSUMER_API',env.CONSUMER_WORKER) ||
        !binding(reviews,'CF_VERSION_METADATA','version_metadata') ||
        !binding(consumer,'CF_VERSION_METADATA','version_metadata') ||
        !serviceBinding(producer,'REVIEWS_API',env.REVIEWS_WORKER) ||
        !serviceBinding(source,'REVIEWS_SOURCE_TRIGGER',env.PRODUCER_WORKER) ||
        !serviceBinding(consumer,'REVIEWS_API',env.REVIEWS_WORKER) ||
        !producer.resources.bindings.some(x=>x.type==='queue' && x.name==='EVENT_QUEUE' && x.queue_name===env.EVENT_QUEUE_NAME) ||
        queue?.queue_id!==env.EVENT_QUEUE_ID || queue?.queue_name!==env.EVENT_QUEUE_NAME ||
        queue?.consumers_total_count!==1 || queue?.consumers?.length!==1 ||
        queue.consumers[0].type!=='worker' || !consumerScriptMatches(queue.consumers[0],env.CONSUMER_WORKER) ||
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
    return {source_version:source.id,producer_version:producer.id,reviews_version:reviews.id,consumer_version:consumer.id};
  });
}

export async function inspectInstallation(env, fetchApi=fetch) {
  if (env.AUTHORITY_WORKER===undefined && env.AUTHORITY_CONTEXT_DB_ID===undefined) return {...await inspectDeployment(env,fetchApi),authority_version:null};
  if (!id.test(env.AUTHORITY_WORKER||'') || !uuid.test(env.AUTHORITY_CONTEXT_DB_ID||'') ||
      env.AUTHORITY_CONTEXT_DB_ID===env.SOURCE_DATABASE_ID ||
      [env.SOURCE_WORKER,env.PRODUCER_WORKER,env.REVIEWS_WORKER,env.CONSUMER_WORKER].includes(env.AUTHORITY_WORKER)) throw new Error('authority configuration mismatch');
  const source=await inspectDeployment(env,fetchApi);
  const authority=await bounded(signal=>activeVersion(env,env.AUTHORITY_WORKER,signal,fetchApi));
  if (!dbBinding(authority,'COMMERCE_DB',env.SOURCE_DATABASE_ID) ||
      !dbBinding(authority,'CONTEXT_DB',env.AUTHORITY_CONTEXT_DB_ID) ||
      !serviceBinding(authority,'SOURCE_IDENTITY',env.SOURCE_WORKER) ||
      !serviceBinding(authority,'CHECKOUT',env.SOURCE_WORKER) ||
      !serviceBinding(authority,'ATTESTOR',env.PRODUCER_WORKER) ||
      !serviceBinding(authority,'REVIEWS_API',env.REVIEWS_WORKER) ||
      !binding(authority,'CF_VERSION_METADATA','version_metadata')) throw new Error('authority binding mismatch');
  return {...source,authority_version:authority.id};
}

export function installationFields(env, versions, now=Math.floor(Date.now()/1000)) {
  return {subscriber_id:env.SOURCE_ISSUER,installation_id:env.SOURCE_INSTALLATION,
    account_id:env.CF_ACCOUNT_ID,store_key:env.SOURCE_STORE,environment:env.REVIEWS_ENVIRONMENT,
    source_database_id:env.SOURCE_DATABASE_ID,stripe_account_id:env.STRIPE_ACCOUNT_ID,
    catalogue_version:env.CATALOGUE_VERSION,mapping_sha256:env.MAPPING_SHA256,...versions,
    context_database_id:env.AUTHORITY_CONTEXT_DB_ID??null,event_queue_id:env.EVENT_QUEUE_ID,
    dead_letter_queue_id:env.DEAD_LETTER_QUEUE_ID,source_worker:env.SOURCE_WORKER,
    producer_worker:env.PRODUCER_WORKER,reviews_worker:env.REVIEWS_WORKER,
    consumer_worker:env.CONSUMER_WORKER,authority_worker:env.AUTHORITY_WORKER??null,
    event_queue_name:env.EVENT_QUEUE_NAME,dead_letter_queue_name:env.DEAD_LETTER_QUEUE_NAME,
    activation_status:'active',verified_at:now};
}
// expectedRevision is required: simultaneous operators cannot overwrite each other.
export async function verifyInstallation(env, expectedRevision, fetchApi=fetch) {
  if (!Number.isSafeInteger(expectedRevision) || expectedRevision<0) throw new Error('expected revision required');
  const record=installationFields(env,await inspectInstallation(env,fetchApi));
  const db=env.COMMERCE_DB.withSession('first-primary');
  const checkpoint=await db.prepare('SELECT activation_watermark FROM reviews_subscriber_checkpoint WHERE subscriber_id=? AND installation_id=?')
    .bind(env.SOURCE_ISSUER,env.SOURCE_INSTALLATION).first();
  // Existing activation owns the no-backfill watermark; verification never resets it.
  record.activation_status=checkpoint ? 'active' : 'verified';
  const keys=Object.keys(record);
  const result=await db.prepare(`INSERT INTO reviews_verified_deployment (${keys.join(',')},revision) SELECT ${keys.map(()=>'?').join(',')},? WHERE COALESCE((SELECT MAX(revision) FROM reviews_verified_deployment WHERE subscriber_id=? AND installation_id=?),0)=? RETURNING revision`)
    .bind(...Object.values(record),expectedRevision+1,env.SOURCE_ISSUER,env.SOURCE_INSTALLATION,expectedRevision).all();
  if (result.results?.length!==1) throw new Error('verification revision conflict; rerun doctor and retry with current revision');
  return {...record,revision:expectedRevision+1};
}
export async function doctor(env, fetchApi=fetch) {
  const actual=installationFields(env,await inspectInstallation(env,fetchApi));
  const record=await env.COMMERCE_DB.withSession('first-primary').prepare('SELECT * FROM reviews_verified_deployment WHERE subscriber_id=? AND installation_id=? ORDER BY revision DESC LIMIT 1')
    .bind(env.SOURCE_ISSUER,env.SOURCE_INSTALLATION).first();
  return {revision:record?.revision??0,status:record && Object.entries(actual).every(([k,v])=>k==='verified_at' || k==='activation_status' || record[k]===v) ? record.activation_status : 'mismatch',
    mismatches:Object.keys(actual).filter(k=>k!=='verified_at' && k!=='activation_status' && record?.[k]!==actual[k])};
}

// Resolve source routing from authenticated account metadata; an operator-supplied
// URL must never be allowed to impersonate the Worker whose version was checked.
export async function sourceIdentityURL(env, fetchApi=fetch) {
  if (!/^[a-f0-9]{32}$/.test(env.CF_ACCOUNT_ID||'') || !id.test(env.SOURCE_WORKER||'') || !token(env.CF_READ_TOKEN))
    throw new Error('source routing configuration unavailable');
  return bounded(async signal=>{
    const root=`https://api.cloudflare.com/client/v4/accounts/${env.CF_ACCOUNT_ID}/workers`;
    const read=async path=>{
      const result=await jsonResponse(await fetchApi(root+path,{headers:{authorization:`Bearer ${env.CF_READ_TOKEN}`},signal}),signal);
      if(result.success!==true)throw new Error('source routing unavailable');
      return result.result;
    };
    const [account,worker]=await Promise.all([read('/subdomain'),read(`/scripts/${env.SOURCE_WORKER}/subdomain`)]);
    if(!/^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/.test(account?.subdomain||'') || worker?.enabled!==true)
      throw new Error('verified source workers.dev route unavailable');
    const url=`https://${env.SOURCE_WORKER}.${account.subdomain}.workers.dev/internal/reviews-source-identity`;
    if(env.SOURCE_IDENTITY_URL!==undefined && env.SOURCE_IDENTITY_URL!==url)throw new Error('source identity URL does not match authenticated Worker route');
    return url;
  });
}

// Wrangler's CLI accepts SQL text rather than bindings. Keep the adapter's
// value encoding explicit, including nullable relay-only authority fields.
export function operatorSQL(sql,args=[]) {
  let index=0;
  const result=sql.replaceAll('?',()=>{
    if(index>=args.length)throw new Error('missing SQL argument');
    const value=args[index++];
    if(value===null)return 'NULL';
    if(typeof value==='number' && Number.isSafeInteger(value))return String(value);
    if(typeof value==='string')return "'"+value.replaceAll("'","''")+"'";
    throw new Error('unsupported SQL argument');
  });
  if(index!==args.length)throw new Error('extra SQL argument');
  return result;
}
