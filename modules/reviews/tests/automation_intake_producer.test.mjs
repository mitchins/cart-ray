import test from 'node:test';
import {schema} from './fixtures/producer-workerd/schema-fixture.mjs';
import assert from 'node:assert/strict';
import {webcrypto} from 'node:crypto';
import producer, {attest, attestPurchase, relay} from '../automation-intake-producer/worker.mjs';
import {inspectDeployment,inspectInstallation,installationFields,sourceIdentityURL,operatorSQL} from '../automation-intake-producer/operator.mjs';
import {deliver} from '../automation-intake-queue/worker.mjs';
if (!globalThis.crypto) globalThis.crypto=webcrypto;
const v1='11111111-1111-4111-8111-111111111111',v2='22222222-2222-4222-8222-222222222222',v3='33333333-3333-4333-8333-333333333333',dbid='44444444-4444-4444-8444-444444444444';
const catalogue='sha256:'+'b'.repeat(64), mapping=JSON.stringify({[catalogue]:{kit:'subject'}});
const digest=Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',new TextEncoder().encode(mapping))),x=>x.toString(16).padStart(2,'0')).join('');
const plain=(name,text)=>({name,type:'plain_text',text}), d1=(name)=>({name,type:'d1',database_id:dbid}), svc=(name,service)=>({name,type:'service',service});
function fixture(){
  const versions={source:{id:v1,resources:{bindings:[d1('DB'),plain('CARTRAY_ENVIRONMENT','test'),plain('CARTRAY_REVIEWS_SOURCE','test'),plain('CARTRAY_REVIEWS_PRODUCER','test'),{name:'CF_VERSION_METADATA',type:'version_metadata'},svc('REVIEWS_SOURCE_TRIGGER','producer')]}},
    producer:{id:v2,resources:{bindings:[d1('COMMERCE_DB'),svc('SOURCE_API','source'),svc('CONSUMER_API','consumer'),svc('REVIEWS_API','reviews'),{name:'EVENT_QUEUE',type:'queue',queue_name:'queue1'}]}},
    authority:{id:'66666666-6666-4666-8666-666666666666',resources:{bindings:[d1('COMMERCE_DB'),{name:'CONTEXT_DB',type:'d1',database_id:'77777777-7777-4777-8777-777777777777'},svc('SOURCE_IDENTITY','source'),svc('ATTESTOR','producer'),svc('REVIEWS_API','reviews'),svc('CHECKOUT','source'),{name:'CF_VERSION_METADATA',type:'version_metadata'}]}},
    consumer:{id:'55555555-5555-4555-8555-555555555555',resources:{bindings:[{name:'CF_VERSION_METADATA',type:'version_metadata'},svc('REVIEWS_API','reviews'),plain('REVIEWS_ENVIRONMENT','test'),plain('REVIEWS_ORDER_SUBSCRIBER','test'),plain('SOURCE_STORE','acorn'),plain('SOURCE_ISSUER','issuer1'),plain('SOURCE_INSTALLATION','install1')]}},
    reviews:{id:v3,resources:{bindings:[d1('CARTRAY_DB'),{name:'CF_VERSION_METADATA',type:'version_metadata'},plain('REVIEWS_ENVIRONMENT','test'),plain('REVIEWS_MODE','verified_acquisition_only'),plain('REVIEWS_AUTOMATION_INTAKE','test'),plain('REVIEWS_STORE_KEY','acorn'),plain('REVIEWS_SUBJECT_MAPS',mapping)]}}};
  let cursor=null;const sent=[];
  const database={withSession:()=>database,prepare(sql){return {args:[],bind(...args){this.args=args;return this},async run(){if (!cursor) cursor={installation_id:this.args[1],source_database_id:this.args[2],stripe_account_id:this.args[3],catalogue_version:this.args[4],mapping_sha256:this.args[5],activation_watermark:this.args[6],last_outbox_id:this.args[7]};},async first(){if(sql.includes("reviews_verified_deployment")) return installationFields(env,{source_version:v1,producer_version:v2,reviews_version:v3,consumer_version:versions.consumer.id,authority_version:env.AUTHORITY_WORKER ? versions.authority.id : null},1000) && {...installationFields(env,{source_version:v1,producer_version:v2,reviews_version:v3,consumer_version:versions.consumer.id,authority_version:env.AUTHORITY_WORKER ? versions.authority.id : null},1000),revision:1};return {...cursor}},async all(){
    if(sql.startsWith('SELECT name,sql FROM sqlite_master')) return {results:schema};
    if(sql.startsWith('SELECT id')) return {results:[9,11,12].filter(x=>x>this.args[0]).map(id=>({id}))};
    if(sql.startsWith('UPDATE')) {if(cursor.last_outbox_id!==this.args[2]) return {results:[]};cursor.last_outbox_id=this.args[0];return {results:[{last_outbox_id:cursor.last_outbox_id}]};} throw Error('unexpected sql');}}}};
  const env={REVIEWS_ENVIRONMENT:'test',REVIEWS_SOURCE_PRODUCER:'test',SOURCE_WORKER:'source',PRODUCER_WORKER:'producer',REVIEWS_WORKER:'reviews',AUTHORITY_WORKER:'authority',AUTHORITY_CONTEXT_DB_ID:'77777777-7777-4777-8777-777777777777',PURCHASE_ATTESTATION_KEY:'45'.repeat(32),PURCHASE_ATTESTATION_TOKEN:'p'.repeat(32),CONSUMER_WORKER:'consumer',EVENT_QUEUE_NAME:'queue1',EVENT_QUEUE_ID:'e'.repeat(32),DEAD_LETTER_QUEUE_NAME:'dlq1',DEAD_LETTER_QUEUE_ID:'d'.repeat(32),SOURCE_STORE:'acorn',SOURCE_ISSUER:'issuer1',SOURCE_INSTALLATION:'install1',CF_ACCOUNT_ID:'a'.repeat(32),SOURCE_DATABASE_ID:dbid,STRIPE_ACCOUNT_ID:'acct_123',CATALOGUE_VERSION:catalogue,MAPPING_SHA256:digest,SOURCE_EVENT_KEY:'12'.repeat(32),CF_READ_TOKEN:'c'.repeat(32),SOURCE_IDENTITY_TOKEN:'s'.repeat(32),REVIEWS_ACTIVATION_TOKEN:'a'.repeat(32),TRIGGER_TOKEN:'t'.repeat(32),POLICY_VERSION:'policy1',DELAY_SECONDS:10,MAX_LATENESS:100,CF_VERSION_METADATA:{id:v2},COMMERCE_DB:database,
    EVENT_QUEUE:{async send(x){sent.push(x)}},SOURCE_API:{async fetch(){return Response.json({version_id:v1,stripe_account:'acct_123',catalogue_version:catalogue})}},CONSUMER_API:{async fetch(){return Response.json({version_id:'55555555-5555-4555-8555-555555555555'})}},REVIEWS_API:{async fetch(url){return Response.json(url.includes('runtime-identity')?{version_id:v3}:{status:'active',outbox_watermark:10})}}};
  return {env,versions,sent,get cursor(){return cursor}};
}
function stubControl(f){return async url=>{const parts=new URL(url).pathname.split('/'); if(parts.includes('queues')) return Response.json({success:true,result:parts.at(-1)==='d'.repeat(32)?{queue_id:'d'.repeat(32),queue_name:'dlq1',consumers_total_count:0}:{queue_id:'e'.repeat(32),queue_name:'queue1',consumers_total_count:1,settings:{delivery_paused:false},consumers:[{type:'worker',script_name:'consumer',dead_letter_queue:'dlq1',settings:{max_retries:3}}]}});const i=parts.indexOf('scripts'),script=parts[i+1]; const v=f.versions[script];
  if(!v) return new Response('',{status:404});return Response.json({success:true,result:parts[i+2]==='deployments'?[{created_on:'2026-01-01',versions:[{version_id:v.id,percentage:100}]}]:v});};}
test('attested source relays only post-fence outbox IDs, with signed messages accepted by subscriber',async()=>{
  const f=fixture(), original=globalThis.fetch;globalThis.fetch=stubControl(f);
  try { assert.equal((await attest(f.env)).source_version,v1);assert.deepEqual(await relay(f.env),{last_outbox_id:12});
    assert.deepEqual(f.sent.map(x=>x.outbox_id),[11,12]);
    const receiver={REVIEWS_ENVIRONMENT:'test',REVIEWS_ORDER_SUBSCRIBER:'test',AUTHORITY_WORKER:'authority',AUTHORITY_CONTEXT_DB_ID:'77777777-7777-4777-8777-777777777777',PURCHASE_ATTESTATION_KEY:'45'.repeat(32),PURCHASE_ATTESTATION_TOKEN:'p'.repeat(32),CONSUMER_WORKER:'consumer',EVENT_QUEUE_NAME:'queue1',EVENT_QUEUE_ID:'e'.repeat(32),DEAD_LETTER_QUEUE_NAME:'dlq1',DEAD_LETTER_QUEUE_ID:'d'.repeat(32),SOURCE_STORE:'acorn',SOURCE_ISSUER:'issuer1',SOURCE_INSTALLATION:'install1',SOURCE_EVENT_KEY:f.env.SOURCE_EVENT_KEY,REVIEWS_AUTOMATION_TOKEN:'r'.repeat(32),REVIEWS_API:{fetch:async()=>Response.json({status:'accepted',due_at:100})}};
    assert.equal(await deliver(f.sent[0],receiver),'acknowledged');await relay(f.env);assert.equal(f.sent.length,2);
  } finally {globalThis.fetch=original;}
});
test('wrong actual D1 binding, active version, source account and mapping fail closed',async()=>{
  for (const kind of ['source-db','producer-db','reviews-db','version','account','mapping','service','environment','entrypoint','queue','consumer-target','duplicate-worker']) {
    const f=fixture(), original=globalThis.fetch;
    if(kind==='duplicate-worker') f.env.PRODUCER_WORKER='source';
    if(kind==='source-db') f.versions.source.resources.bindings[0].database_id=v1;
    if(kind==='producer-db') f.versions.producer.resources.bindings[0].database_id=v1;
    if(kind==='reviews-db') f.versions.reviews.resources.bindings[0].database_id=v1;
    if(kind==='version') f.env.CF_VERSION_METADATA.id=v1;
    if(kind==='account') f.env.STRIPE_ACCOUNT_ID='acct_other';
    if(kind==='mapping') f.env.MAPPING_SHA256='0'.repeat(64);
    if(kind==='service') f.versions.producer.resources.bindings[1].service='other';
    if(kind==='environment') f.versions.producer.resources.bindings[2].environment='staging';
    if(kind==='queue') f.versions.producer.resources.bindings.find(x=>x.type==='queue').queue_name='other';
    if(kind==='consumer-target') f.versions.consumer.resources.bindings.find(x=>x.type==='service').service='other';
    if(kind==='entrypoint') f.versions.producer.resources.bindings[2].entrypoint='Other';
    globalThis.fetch=stubControl(f);try {await assert.rejects(inspectDeployment(f.env,stubControl(f)));assert.equal(f.sent.length,0);assert.equal(f.cursor,null);} finally {globalThis.fetch=original;}
  }
});
test('Cloudflare production service metadata selects the attested default deployment',async()=>{
  const f=fixture();
  for (const version of Object.values(f.versions)) {
    for (const binding of version.resources.bindings) if (binding.type==='service') binding.environment='production';
  }
  assert.equal((await attest(f.env,stubControl(f))).source_version,v1);
  assert.equal((await attestPurchase(f.env,{authority_version:f.versions.authority.id,nonce:'a'.repeat(64)},stubControl(f),1000)).proof[12],f.versions.authority.id);
  for (const value of ['staging',null]) {
    const x=fixture();x.versions.source.resources.bindings.at(-1).environment=value;
    await assert.rejects(inspectDeployment(x.env,stubControl(x)));assert.equal(x.cursor,null);assert.equal(x.sent.length,0);
  }
});
test('send failure does not advance cursor; retry replays safely',async()=>{
  const f=fixture(),original=globalThis.fetch;globalThis.fetch=stubControl(f);let first=true;
  f.env.EVENT_QUEUE.send=async x=>{if(first){first=false;throw Error('offline');}f.sent.push(x);};
  try {await assert.rejects(relay(f.env));assert.equal(f.cursor.last_outbox_id,10);await relay(f.env);assert.deepEqual(f.sent.map(x=>x.outbox_id),[11,12]);}finally {globalThis.fetch=original;}
});

test('purchase proof pins the active authority and both its actual D1 and service bindings',async()=>{
  const f=fixture(), original=globalThis.fetch;globalThis.fetch=stubControl(f);
  const input={authority_version:f.versions.authority.id,nonce:'a'.repeat(64)};
  try {const result=await attestPurchase(f.env,input,globalThis.fetch,1000);
    assert.equal(result.proof[12],input.authority_version);assert.equal(result.proof[13],input.nonce);assert.equal(result.proof[15],1030);assert.match(result.signature,/^[a-f0-9]{64}$/);
    for(const kind of ['commerce','context','source','attestor','metadata','version','reviews','authority-name','checkout']) {
      const x=fixture();if(kind==='authority-name')x.env.AUTHORITY_WORKER='source';
      if(kind==='commerce')x.versions.authority.resources.bindings[0].database_id=v1;
      if(kind==='context')x.versions.authority.resources.bindings[1].database_id=v1;
      if(kind==='source')x.versions.authority.resources.bindings[2].service='other';
      if(kind==='attestor')x.versions.authority.resources.bindings[3].service='other';
      if(kind==='metadata')x.versions.authority.resources.bindings.pop();
      if(kind==='reviews')x.versions.authority.resources.bindings[4].service='other';
      if(kind==='checkout')x.versions.authority.resources.bindings[5].service='other';
      if(kind==='version')x.versions.authority.resources.bindings[0].database_id=v1;
      await assert.rejects(inspectInstallation(x.env,stubControl(x)));
    }
  } finally {globalThis.fetch=original;}
});
test('missing or unsafe dead-letter routing fails before any checkpoint or enqueue',async()=>{
  for (const kind of ['wrong-dlq','missing-dlq','zero-retries','paused','unexpected-dlq-consumer','wrong-consumer']) {
    const f=fixture(),base=stubControl(f);
    const control=async (url,options)=>{
      const response=await base(url,options); if (!url.includes('/queues/')) return response;
      const data=await response.json();const q=data.result;
      if(q.queue_name==='queue1') {
        if(kind==='wrong-dlq')q.consumers[0].dead_letter_queue='other';
        if(kind==='missing-dlq')delete q.consumers[0].dead_letter_queue;
        if(kind==='zero-retries')q.consumers[0].settings.max_retries=0;
        if(kind==='paused')q.settings.delivery_paused=true;
        if(kind==='wrong-consumer')q.consumers[0].script_name='other';
      } else if(kind==='unexpected-dlq-consumer')q.consumers_total_count=1;
      return Response.json(data);
    };
    await assert.rejects(inspectDeployment(f.env,control));assert.equal(f.sent.length,0);assert.equal(f.cursor,null);
  }
});

test('consumer identity accepts documented and observed API shapes while rejecting conflicts',async()=>{
  for (const kind of ['documented','observed','both','conflict','missing','wrong-observed','missing-pause','paused']) {
    const f=fixture(),base=stubControl(f);
    const control=async (url,options)=>{
      const response=await base(url,options);
      if (!url.includes('/queues/')) return response;
      const data=await response.json(),q=data.result;
      if(q.queue_name==='queue1') {
        const c=q.consumers[0];
        if(kind==='observed'||kind==='wrong-observed'||kind==='missing') delete c.script_name;
        if(['observed','both','conflict','wrong-observed'].includes(kind)) c.script=kind==='conflict'||kind==='wrong-observed'?'other':'consumer';
        if(kind==='missing-pause') delete q.settings.delivery_paused;
        if(kind==='paused') q.settings.delivery_paused=true;
      }
      return Response.json(data);
    };
    if(['documented','observed','both'].includes(kind)) assert.equal((await inspectDeployment(f.env,control)).source_version,v1);
    else { await assert.rejects(inspectDeployment(f.env,control)); assert.equal(f.sent.length,0); assert.equal(f.cursor,null); }
  }
});

test('private attestation route rejects unauthenticated callers and returns scoped proof',async()=>{
  const f=fixture(),original=globalThis.fetch;globalThis.fetch=stubControl(f);
  try {
    const request=(path,body,token)=>new Request(`https://producer.test${path}`,{method:'POST',headers:{authorization:`Bearer ${token}`,'content-type':'application/json'},body:JSON.stringify(body)});
    const input={authority_version:f.versions.authority.id,nonce:'a'.repeat(64)};
    assert.equal((await producer.fetch(request('/attest-purchase',input,'wrong'),f.env)).status,404);
    const response=await producer.fetch(request('/attest-purchase',input,f.env.PURCHASE_ATTESTATION_TOKEN),f.env);
    assert.equal(response.status,200);const value=await response.json();assert.equal(value.proof[13],input.nonce);
    assert.equal((await producer.fetch(request('/attest-purchase',{...input,extra:1},f.env.PURCHASE_ATTESTATION_TOKEN),f.env)).status,503);
    assert.equal((await producer.fetch(request('/',{},'wrong'),f.env)).status,404);
    assert.equal((await producer.fetch(request('/',{},f.env.TRIGGER_TOKEN),f.env)).status,204);
  } finally {globalThis.fetch=original;}
});
test('an older or modified checkpoint schema fails before activation or enqueue',async()=>{
  for (const kind of ['missing-trigger','modified-trigger','extra-trigger']) {
    const f=fixture(), db=f.env.COMMERCE_DB, prepare=db.prepare;
    db.prepare=function(sql){const statement=prepare.call(this,sql);if(sql.startsWith('SELECT name,sql FROM sqlite_master')) {
      statement.all=async()=>({results:kind==='missing-trigger'?schema.slice(1):kind==='modified-trigger'?schema.map((r,i)=>i? r:{...r,sql:r.sql.replace('DELETE','UPDATE')}):[...schema,{name:'extra',sql:'CREATE TRIGGER extra'}]});
    }return statement;};
    let activations=0;f.env.REVIEWS_API.fetch=async url=>{if(url.includes('runtime-identity'))return Response.json({version_id:v3});activations++;return Response.json({status:'active',outbox_watermark:10});};
    await assert.rejects(relay(f.env,stubControl(f)),/checkpoint schema/);assert.equal(activations,0);assert.equal(f.sent.length,0);
  }
});

test('runtime relay and purchase evidence survive unavailable control plane without CF credential',async()=>{
  const f=fixture();delete f.env.CF_READ_TOKEN;
  const unavailable=()=>{throw Error('control plane unavailable')};
  f.env.SOURCE_API.fetch=async (url,options)=>{assert.equal(url,'https://source.internal/internal/reviews-runtime-identity');assert.equal(options.headers.authorization,`Bearer ${f.env.SOURCE_IDENTITY_TOKEN}`);return Response.json({version_id:v1,catalogue_version:catalogue});};
  assert.deepEqual(await relay(f.env,unavailable),{last_outbox_id:12});
  assert.equal((await attestPurchase(f.env,{authority_version:f.versions.authority.id,nonce:'a'.repeat(64)},unavailable,1000)).proof[12],f.versions.authority.id);
});
test('missing record, mismatched record, producer/source versions and authority versions fail closed',async()=>{
  for(const kind of ['missing','store','producer','source','authority','inactive']) {
    const f=fixture(),base=f.env.COMMERCE_DB.prepare;
    f.env.COMMERCE_DB.prepare=function(sql){const stmt=base.call(this,sql);if(sql.includes('reviews_verified_deployment')) {const first=stmt.first;stmt.first=async()=>{
      const r=await first();if(kind==='missing')return null;
      if(kind==='store')r.store_key='forged';if(kind==='producer')r.producer_version=v1;
      if(kind==='source')r.source_version=v2;if(kind==='authority')r.authority_version=v1;
      if(kind==='inactive')r.activation_status='disabled';return r;
    };}return stmt;};
    await assert.rejects(attestPurchase(f.env,{authority_version:f.versions.authority.id,nonce:'a'.repeat(64)}));
    assert.equal(f.sent.length,0);assert.equal(f.cursor,null);
  }
});

 test('operator source identity route is derived from authenticated Worker routing, never an arbitrary endpoint',async()=>{
  const f=fixture();
  const control=async url=>Response.json({success:true,result:url.includes('/scripts/')?{enabled:true}:{subdomain:'sandbox-account'}});
  const expected='https://source.sandbox-account.workers.dev/internal/reviews-source-identity';
  assert.equal(await sourceIdentityURL(f.env,control),expected);
  for(const url of ['https://attacker.example/identity','https://other.sandbox-account.workers.dev/internal/reviews-source-identity',expected+'?redirect=1']) {
    await assert.rejects(sourceIdentityURL({...f.env,SOURCE_IDENTITY_URL:url},control));
  }
  await assert.rejects(sourceIdentityURL(f.env,async url=>Response.json({success:true,result:url.includes('/scripts/')?{enabled:false}:{subdomain:'sandbox-account'}})));
  await assert.rejects(sourceIdentityURL(f.env,async()=>new Response(null,{status:503})));
});

test('relay-only installation retains four-worker topology and cannot issue purchaser proof',async()=>{
 const f=fixture();delete f.env.AUTHORITY_WORKER;delete f.env.AUTHORITY_CONTEXT_DB_ID;
 assert.equal((await inspectInstallation(f.env,stubControl(f))).authority_version,null);
 assert.deepEqual(await relay(f.env),{last_outbox_id:12});
 await assert.rejects(attestPurchase(f.env,{authority_version:f.versions.authority.id,nonce:'a'.repeat(64)}));
});

test('changed Reviews or consumer runtime versions stop relay and purchaser evidence',async()=>{
 for(const target of ['REVIEWS_API','CONSUMER_API']) {
  const f=fixture();f.env[target].fetch=async()=>Response.json({version_id:v1});
  await assert.rejects(relay(f.env),/runtime identity mismatch/);
  await assert.rejects(attestPurchase(f.env,{authority_version:f.versions.authority.id,nonce:'a'.repeat(64)}),/runtime identity mismatch/);
  assert.equal(f.cursor,null);assert.equal(f.sent.length,0);
 }
});

test('consumer runtime identity requires source authentication and actual version metadata',async()=>{
 const {default:subscriber}=await import('../automation-intake-queue/worker.mjs');
 const env={REVIEWS_ENVIRONMENT:'test',REVIEWS_ORDER_SUBSCRIBER:'test',SOURCE_EVENT_KEY:'12'.repeat(32),CF_VERSION_METADATA:{id:v1}};
 const request=key=>new Request('https://consumer.internal/runtime-identity',{headers:{authorization:`Bearer ${key}`}});
 assert.equal((await subscriber.fetch(request('forged'),env)).status,404);
 assert.deepEqual(await (await subscriber.fetch(request(env.SOURCE_EVENT_KEY),env)).json(),{version_id:v1});
 delete env.CF_VERSION_METADATA;assert.equal((await subscriber.fetch(request(env.SOURCE_EVENT_KEY),env)).status,503);
});

test('operator CLI SQL adapter preserves nulls and quotes without changing SQL meaning',()=>{
 assert.equal(operatorSQL('SELECT ?,?,?,?',[null,"O'Brien",42,'null']),"SELECT NULL,'O''Brien',42,'null'");
 assert.throws(()=>operatorSQL('SELECT ?',[undefined]));
 assert.throws(()=>operatorSQL('SELECT ?',[NaN]));
});
