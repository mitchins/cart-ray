import test from 'node:test';
import assert from 'node:assert/strict';
import {webcrypto} from 'node:crypto';
import {attest, relay} from '../automation-intake-producer/worker.mjs';
import {deliver} from '../automation-intake-queue/worker.mjs';
if (!globalThis.crypto) globalThis.crypto=webcrypto;
const v1='11111111-1111-4111-8111-111111111111',v2='22222222-2222-4222-8222-222222222222',v3='33333333-3333-4333-8333-333333333333',dbid='44444444-4444-4444-8444-444444444444';
const catalogue='sha256:'+'b'.repeat(64), mapping=JSON.stringify({[catalogue]:{kit:'subject'}});
const digest=Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',new TextEncoder().encode(mapping))),x=>x.toString(16).padStart(2,'0')).join('');
const plain=(name,text)=>({name,type:'plain_text',text}), d1=(name)=>({name,type:'d1',database_id:dbid}), svc=(name,service)=>({name,type:'service',service});
function fixture(){
  const versions={source:{id:v1,resources:{bindings:[d1('DB'),plain('CARTRAY_ENVIRONMENT','test'),plain('CARTRAY_REVIEWS_SOURCE','test'),plain('CARTRAY_REVIEWS_PRODUCER','test'),{name:'CF_VERSION_METADATA',type:'version_metadata'},svc('REVIEWS_SOURCE_TRIGGER','producer')]}},
    producer:{id:v2,resources:{bindings:[d1('COMMERCE_DB'),svc('SOURCE_API','source'),svc('REVIEWS_API','reviews')]}},
    reviews:{id:v3,resources:{bindings:[d1('CARTRAY_DB'),plain('REVIEWS_ENVIRONMENT','test'),plain('REVIEWS_MODE','verified_acquisition_only'),plain('REVIEWS_AUTOMATION_INTAKE','test'),plain('REVIEWS_STORE_KEY','acorn'),plain('REVIEWS_SUBJECT_MAPS',mapping)]}}};
  let cursor=null;const sent=[];
  const database={withSession:()=>database,prepare(sql){return {args:[],bind(...args){this.args=args;return this},async run(){if (!cursor) cursor={installation_id:this.args[1],source_database_id:this.args[2],stripe_account_id:this.args[3],catalogue_version:this.args[4],mapping_sha256:this.args[5],activation_watermark:this.args[6],last_outbox_id:this.args[7]};},async first(){return {...cursor}},async all(){
    if(sql.startsWith('SELECT id')) return {results:[9,11,12].filter(x=>x>this.args[0]).map(id=>({id}))};
    if(sql.startsWith('UPDATE')) {if(cursor.last_outbox_id!==this.args[2]) return {results:[]};cursor.last_outbox_id=this.args[0];return {results:[{last_outbox_id:cursor.last_outbox_id}]};} throw Error('unexpected sql');}}}};
  const env={REVIEWS_ENVIRONMENT:'test',REVIEWS_SOURCE_PRODUCER:'test',SOURCE_WORKER:'source',PRODUCER_WORKER:'producer',REVIEWS_WORKER:'reviews',SOURCE_STORE:'acorn',SOURCE_ISSUER:'issuer1',SOURCE_INSTALLATION:'install1',CF_ACCOUNT_ID:'a'.repeat(32),SOURCE_DATABASE_ID:dbid,STRIPE_ACCOUNT_ID:'acct_123',CATALOGUE_VERSION:catalogue,MAPPING_SHA256:digest,SOURCE_EVENT_KEY:'12'.repeat(32),CF_READ_TOKEN:'c'.repeat(32),SOURCE_IDENTITY_TOKEN:'s'.repeat(32),REVIEWS_ACTIVATION_TOKEN:'a'.repeat(32),TRIGGER_TOKEN:'t'.repeat(32),POLICY_VERSION:'policy1',DELAY_SECONDS:10,MAX_LATENESS:100,CF_VERSION_METADATA:{id:v2},COMMERCE_DB:database,
    EVENT_QUEUE:{async send(x){sent.push(x)}},SOURCE_API:{async fetch(){return Response.json({version_id:v1,stripe_account:'acct_123',catalogue_version:catalogue})}},REVIEWS_API:{async fetch(){return Response.json({status:'active',outbox_watermark:10})}}};
  return {env,versions,sent,get cursor(){return cursor}};
}
function stubControl(f){return async url=>{const parts=new URL(url).pathname.split('/');const i=parts.indexOf('scripts'),script=parts[i+1]; const v=f.versions[script];
  if(!v) return new Response('',{status:404});return Response.json({success:true,result:parts[i+2]==='deployments'?[{created_on:'2026-01-01',versions:[{version_id:v.id,percentage:100}]}]:v});};}
test('attested source relays only post-fence outbox IDs, with signed messages accepted by subscriber',async()=>{
  const f=fixture(), original=globalThis.fetch;globalThis.fetch=stubControl(f);
  try { assert.equal((await attest(f.env)).source_version,v1);assert.deepEqual(await relay(f.env),{last_outbox_id:12});
    assert.deepEqual(f.sent.map(x=>x.outbox_id),[11,12]);
    const receiver={REVIEWS_ENVIRONMENT:'test',REVIEWS_ORDER_SUBSCRIBER:'test',SOURCE_STORE:'acorn',SOURCE_ISSUER:'issuer1',SOURCE_INSTALLATION:'install1',SOURCE_EVENT_KEY:f.env.SOURCE_EVENT_KEY,REVIEWS_AUTOMATION_TOKEN:'r'.repeat(32),REVIEWS_API:{fetch:async()=>Response.json({status:'accepted',due_at:100})}};
    assert.equal(await deliver(f.sent[0],receiver),'acknowledged');await relay(f.env);assert.equal(f.sent.length,2);
  } finally {globalThis.fetch=original;}
});
test('wrong actual D1 binding, active version, source account and mapping fail closed',async()=>{
  for (const kind of ['source-db','producer-db','reviews-db','version','account','mapping','service']) {
    const f=fixture(), original=globalThis.fetch;
    if(kind==='source-db') f.versions.source.resources.bindings[0].database_id=v1;
    if(kind==='producer-db') f.versions.producer.resources.bindings[0].database_id=v1;
    if(kind==='reviews-db') f.versions.reviews.resources.bindings[0].database_id=v1;
    if(kind==='version') f.env.CF_VERSION_METADATA.id=v1;
    if(kind==='account') f.env.STRIPE_ACCOUNT_ID='acct_other';
    if(kind==='mapping') f.env.MAPPING_SHA256='0'.repeat(64);
    if(kind==='service') f.versions.producer.resources.bindings[1].service='other';
    globalThis.fetch=stubControl(f);try {await assert.rejects(relay(f.env));assert.equal(f.sent.length,0);assert.equal(f.cursor,null);} finally {globalThis.fetch=original;}
  }
});
test('send failure does not advance cursor; retry replays safely',async()=>{
  const f=fixture(),original=globalThis.fetch;globalThis.fetch=stubControl(f);let first=true;
  f.env.EVENT_QUEUE.send=async x=>{if(first){first=false;throw Error('offline');}f.sent.push(x);};
  try {await assert.rejects(relay(f.env));assert.equal(f.cursor.last_outbox_id,10);await relay(f.env);assert.deepEqual(f.sent.map(x=>x.outbox_id),[11,12]);}finally {globalThis.fetch=original;}
});
