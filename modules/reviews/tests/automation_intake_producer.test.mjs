import test from 'node:test';
import {schema} from './fixtures/producer-workerd/schema-fixture.mjs';
import assert from 'node:assert/strict';
import {webcrypto} from 'node:crypto';
import producer, {attest, attestPurchase, relay} from '../automation-intake-producer/worker.mjs';
import {deliver} from '../automation-intake-queue/worker.mjs';
if (!globalThis.crypto) globalThis.crypto=webcrypto;
const v1='11111111-1111-4111-8111-111111111111',v2='22222222-2222-4222-8222-222222222222',v3='33333333-3333-4333-8333-333333333333',dbid='44444444-4444-4444-8444-444444444444';
const catalogue='sha256:'+'b'.repeat(64), mapping=JSON.stringify({[catalogue]:{kit:'subject'}});
const digest=Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',new TextEncoder().encode(mapping))),x=>x.toString(16).padStart(2,'0')).join('');
const plain=(name,text)=>({name,type:'plain_text',text}), d1=(name)=>({name,type:'d1',database_id:dbid}), svc=(name,service)=>({name,type:'service',service});
function fixture(){
  const versions={source:{id:v1,resources:{bindings:[d1('DB'),plain('CARTRAY_ENVIRONMENT','test'),plain('CARTRAY_REVIEWS_SOURCE','test'),plain('CARTRAY_REVIEWS_PRODUCER','test'),{name:'CF_VERSION_METADATA',type:'version_metadata'},svc('REVIEWS_SOURCE_TRIGGER','producer')]}},
    producer:{id:v2,resources:{bindings:[d1('COMMERCE_DB'),svc('SOURCE_API','source'),svc('REVIEWS_API','reviews'),{name:'EVENT_QUEUE',type:'queue',queue_name:'queue1'}]}},
    authority:{id:'66666666-6666-4666-8666-666666666666',resources:{bindings:[d1('COMMERCE_DB'),{name:'CONTEXT_DB',type:'d1',database_id:'77777777-7777-4777-8777-777777777777'},svc('SOURCE_IDENTITY','source'),svc('ATTESTOR','producer'),svc('REVIEWS_API','reviews'),svc('CHECKOUT','source'),{name:'CF_VERSION_METADATA',type:'version_metadata'}]}},
    consumer:{id:'55555555-5555-4555-8555-555555555555',resources:{bindings:[svc('REVIEWS_API','reviews'),plain('REVIEWS_ENVIRONMENT','test'),plain('REVIEWS_ORDER_SUBSCRIBER','test'),plain('SOURCE_STORE','acorn'),plain('SOURCE_ISSUER','issuer1'),plain('SOURCE_INSTALLATION','install1')]}},
    reviews:{id:v3,resources:{bindings:[d1('CARTRAY_DB'),plain('REVIEWS_ENVIRONMENT','test'),plain('REVIEWS_MODE','verified_acquisition_only'),plain('REVIEWS_AUTOMATION_INTAKE','test'),plain('REVIEWS_STORE_KEY','acorn'),plain('REVIEWS_SUBJECT_MAPS',mapping)]}}};
  let cursor=null;const sent=[];
  const database={withSession:()=>database,prepare(sql){return {args:[],bind(...args){this.args=args;return this},async run(){if (!cursor) cursor={installation_id:this.args[1],source_database_id:this.args[2],stripe_account_id:this.args[3],catalogue_version:this.args[4],mapping_sha256:this.args[5],activation_watermark:this.args[6],last_outbox_id:this.args[7]};},async first(){return {...cursor}},async all(){
    if(sql.startsWith('SELECT name,sql FROM sqlite_master')) return {results:schema};
    if(sql.startsWith('SELECT id')) return {results:[9,11,12].filter(x=>x>this.args[0]).map(id=>({id}))};
    if(sql.startsWith('UPDATE')) {if(cursor.last_outbox_id!==this.args[2]) return {results:[]};cursor.last_outbox_id=this.args[0];return {results:[{last_outbox_id:cursor.last_outbox_id}]};} throw Error('unexpected sql');}}}};
  const env={REVIEWS_ENVIRONMENT:'test',REVIEWS_SOURCE_PRODUCER:'test',SOURCE_WORKER:'source',PRODUCER_WORKER:'producer',REVIEWS_WORKER:'reviews',AUTHORITY_WORKER:'authority',AUTHORITY_CONTEXT_DB_ID:'77777777-7777-4777-8777-777777777777',PURCHASE_ATTESTATION_KEY:'45'.repeat(32),PURCHASE_ATTESTATION_TOKEN:'p'.repeat(32),CONSUMER_WORKER:'consumer',EVENT_QUEUE_NAME:'queue1',EVENT_QUEUE_ID:'e'.repeat(32),DEAD_LETTER_QUEUE_NAME:'dlq1',DEAD_LETTER_QUEUE_ID:'d'.repeat(32),SOURCE_STORE:'acorn',SOURCE_ISSUER:'issuer1',SOURCE_INSTALLATION:'install1',CF_ACCOUNT_ID:'a'.repeat(32),SOURCE_DATABASE_ID:dbid,STRIPE_ACCOUNT_ID:'acct_123',CATALOGUE_VERSION:catalogue,MAPPING_SHA256:digest,SOURCE_EVENT_KEY:'12'.repeat(32),CF_READ_TOKEN:'c'.repeat(32),SOURCE_IDENTITY_TOKEN:'s'.repeat(32),REVIEWS_ACTIVATION_TOKEN:'a'.repeat(32),TRIGGER_TOKEN:'t'.repeat(32),POLICY_VERSION:'policy1',DELAY_SECONDS:10,MAX_LATENESS:100,CF_VERSION_METADATA:{id:v2},COMMERCE_DB:database,
    EVENT_QUEUE:{async send(x){sent.push(x)}},SOURCE_API:{async fetch(){return Response.json({version_id:v1,stripe_account:'acct_123',catalogue_version:catalogue})}},REVIEWS_API:{async fetch(){return Response.json({status:'active',outbox_watermark:10})}}};
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
    if(kind==='queue') f.versions.producer.resources.bindings[3].queue_name='other';
    if(kind==='consumer-target') f.versions.consumer.resources.bindings[0].service='other';
    if(kind==='entrypoint') f.versions.producer.resources.bindings[2].entrypoint='Other';
    globalThis.fetch=stubControl(f);try {await assert.rejects(relay(f.env));assert.equal(f.sent.length,0);assert.equal(f.cursor,null);} finally {globalThis.fetch=original;}
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
      if(kind==='version')x.versions.authority.id=v1;
      await assert.rejects(attestPurchase(x.env,input,stubControl(x),1000));
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
    await assert.rejects(relay(f.env,control));assert.equal(f.sent.length,0);assert.equal(f.cursor,null);
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
    let activations=0;f.env.REVIEWS_API.fetch=async()=>{activations++;return Response.json({status:'active',outbox_watermark:10});};
    await assert.rejects(relay(f.env,stubControl(f)),/checkpoint schema/);assert.equal(activations,0);assert.equal(f.sent.length,0);
  }
});
