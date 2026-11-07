import {test} from 'node:test';
import assert from 'node:assert/strict';
import {DatabaseSync} from 'node:sqlite';
import {readFileSync} from 'node:fs';
import worker,{reconcile,discover} from '../provider-reconciliation/worker.mjs';
const generation='a'.repeat(64),delivery='b'.repeat(64);
function setup(){
 const db=new DatabaseSync(':memory:');db.exec(`CREATE TABLE invitation_generations(store_key TEXT,environment TEXT,acquisition_id TEXT,generation_id TEXT,delivery_identity TEXT,state TEXT,prepared_at INTEGER,request_key TEXT,PRIMARY KEY(store_key,environment,acquisition_id,generation_id)); CREATE TABLE review_automation_intents(store_key TEXT,environment TEXT,acquisition_id TEXT,outbox_id INTEGER);`);
 db.exec(readFileSync(new URL('../migrations/0031_provider_delivery_receipts.sql',import.meta.url),'utf8'));
 db.prepare('INSERT INTO invitation_generations VALUES (?,?,?,?,?,?,?,?)').run('acorn','test','purchase',generation,delivery,'uncertain',100,'initial_10');
 db.prepare('INSERT INTO review_automation_intents VALUES (?,?,?,?)').run('acorn','test','purchase',10);
 db.prepare('INSERT INTO review_provider_deliveries VALUES (?,?,?,?,?,?,?,?)').run('acorn','test','purchase',generation,delivery,'smtp2go','smtp_123',100);
 const prep=sql=>({bind(...args){return{async first(){return db.prepare(sql).get(...args)??null},async run(){db.prepare(sql).run(...args)}}}});
 const env={REVIEWS_ENVIRONMENT:'test',SMTP2GO_RECONCILIATION:'test',REVIEWS_STORE:'acorn',SMTP2GO_ACTIVITY_KEY:'api-synthetic',RECONCILIATION_TOKEN:'z'.repeat(40),HISTORICAL_RECIPIENT_TOKEN:'r'.repeat(40),APPROVED_TEST_EMAIL:'test@example.org',PURCHASE_EVIDENCE:{fetch:async()=>Response.json({email:'test@example.org'})},REVIEWS_DB:{prepare:prep,async batch(stmts){for(const s of stmts)await s.run()}}};
 return {db,env,input:{acquisition_id:'purchase',generation_id:generation,delivery_identity:delivery}};
}
function provider(events,additional={}){return async(url,opts)=>{assert.equal(url,'https://api.smtp2go.com/v3/activity/search');assert.equal(JSON.parse(opts.body).search_email_id,'smtp_123');return Response.json({data:{events,total_events:events.length,...additional}})}}
test('records distinct authoritative activity idempotently and never changes delivery state',async()=>{
 for(const kind of ['processed','delivered','rejected','soft-bounced','hard-bounced']){
  const {db,env,input}=setup(),events=[{email_id:'smtp_123',event:kind,date:'1970-01-01T00:02:00Z'}];
  assert.deepEqual(await reconcile(input,env,provider(events),200),{status:'observed',events:[kind]});
  await reconcile(input,env,provider(events),200);
  assert.equal(db.prepare('SELECT count(*) AS n FROM review_provider_activity').get().n,1);
  assert.equal(db.prepare('SELECT count(*) AS n FROM review_provider_deliveries').get().n,1);
 }
});
test('missing identity or empty and expired activity remain unknown and cannot authorize a resend',async()=>{
 const {db,env,input}=setup(); assert.deepEqual(await reconcile(input,env,provider([]),200),{status:'unknown',reason:'no_activity'});
 const wrong={...input,delivery_identity:'c'.repeat(64)};let calls=0;
 assert.deepEqual(await reconcile(wrong,env,async()=>{calls++;},200),{status:'unknown',reason:'no_provider_identity'});assert.equal(calls,0);
});
test('rejects wrong identity, incomplete, malformed and provider outage without recording observations',async()=>{
 const {db,env,input}=setup();
 for(const f of [provider([{email_id:'different',event:'delivered',date:'1970-01-01T00:02:00Z'}]),provider([],{total_events:101,continue_token:'more'}),provider([{email_id:'smtp_123',event:'delivered',date:'not-a-date'}]),async()=>new Response('',{status:503})])await assert.rejects(reconcile(input,env,f,200));
 assert.equal(db.prepare('SELECT count(*) AS n FROM review_provider_activity').get().n,0);
});
test('private endpoint rejects unauthorized, wrong-scope and malformed callers',async()=>{
 const {env,input}=setup();const req=(token,body)=>new Request('https://private/reconcile',{method:'POST',headers:{'Content-Type':'application/json',Authorization:'Bearer '+token},body:JSON.stringify(body)});
 assert.equal((await worker.fetch(req('bad',input),env)).status,503);
 assert.equal((await worker.fetch(req(env.RECONCILIATION_TOKEN,{...input,email:'attacker@example.org'}),env)).status,503);
 env.REVIEWS_ENVIRONMENT='live';assert.equal((await worker.fetch(req(env.RECONCILIATION_TOKEN,input),env)).status,503);
});
test('discovers a lost provider response by exact delivery header across bounded pages without activating',async()=>{
 const {db,env,input}=setup();let calls=0;
 const fetcher=async(_url,opts)=>{const body=JSON.parse(opts.body);assert.equal(body.search_recipient,'test@example.org');
  calls++;if(!body.continue_token)return Response.json({data:{events:[{to:'test@example.org',recipient:'test@example.org',email_id:'other',event:'delivered',date:'1970-01-01T00:02:00Z',custom_headers:{'X-Reviews-Delivery-Identity':'c'.repeat(64)}}],total_events:2,continue_token:'next'}});
  return Response.json({data:{events:[{to:'test@example.org',recipient:'test@example.org',email_id:'smtp_123',event:'delivered',date:'1970-01-01T00:02:00Z',custom_headers:{'X-Reviews-Delivery-Identity':delivery}}],total_events:2}})};
 assert.deepEqual(await discover(input,env,fetcher,200),{status:'matched_needs_operator',events:['delivered']});assert.equal(calls,2);
 assert.equal(db.prepare('SELECT count(*) n FROM review_provider_identity_discoveries').get().n,1);
 assert.equal(db.prepare('SELECT state FROM invitation_generations').get().state,'uncertain');
 await discover(input,env,fetcher,200);assert.equal(db.prepare('SELECT count(*) n FROM review_provider_identity_discoveries').get().n,1);
});
test('discovery fails closed on incomplete, ambiguous and expired evidence',async()=>{
 const {db,env,input}=setup();const event=id=>({to:'test@example.org',recipient:'test@example.org',email_id:id,event:'processed',date:'1970-01-01T00:02:00Z',custom_headers:{'X-Reviews-Delivery-Identity':delivery}});
 const result=await discover(input,env,async()=>Response.json({data:{events:[event('id1'),event('id2')],total_events:2}}),200);
 assert.deepEqual(result,{status:'unknown',reason:'ambiguous'});
 await assert.rejects(discover(input,env,async()=>Response.json({data:{events:[event('id1')],total_events:2}}),200));
 assert.deepEqual(await discover(input,env,async()=>{throw Error('not called')},500000),{status:'unknown',reason:'history_window'});
 assert.equal(db.prepare('SELECT count(*) n FROM review_provider_identity_discoveries').get().n,0);
});
test('discovery resolves only scoped historical recipient with its own bearer, even after suppression',async()=>{
 const {db,env,input}=setup();let history=0,provider=0;
 env.PURCHASE_EVIDENCE.fetch=async(url,options)=>{history++;assert.equal(new URL(url).pathname,'/historical-recipient');assert.equal(options.headers.Authorization,'Bearer '+env.HISTORICAL_RECIPIENT_TOKEN);assert.deepEqual(JSON.parse(options.body),{outbox_id:10,acquisition_id:'purchase',generation_id:generation,delivery_identity:delivery});return Response.json({email:'test@example.org'})};
 const event={to:'test@example.org',email_id:'smtp_123',event:'delivered',date:'1970-01-01T00:02:00Z',custom_headers:{'X-Reviews-Delivery-Identity':delivery}};
 assert.deepEqual(await discover(input,env,async()=>{provider++;return Response.json({data:{events:[event],total_events:1}})},200),{status:'matched_needs_operator',events:['delivered']});
 assert.equal(history,1);assert.equal(provider,1);assert.equal(db.prepare('SELECT state FROM invitation_generations').get().state,'uncertain');
 env.HISTORICAL_RECIPIENT_TOKEN=env.RECONCILIATION_TOKEN;await assert.rejects(discover(input,env,async()=>{throw Error('must not fetch')},200));
});
test('discovery safely handles header lists and rejects ambiguous or malformed provider headers',async()=>{
 const {db,env,input}=setup();
 const event=headers=>({to:'test@example.org',email_id:'smtp_123',event:'delivered',date:'1970-01-01T00:02:00Z',custom_headers:headers});
 const lookup=headers=>async()=>Response.json({data:{events:[event(headers)],total_events:1}});
 for(const headers of [
  [{header:'X-Reviews-Delivery-Identity',value:delivery},{header:'x-reviews-delivery-identity',value:'c'.repeat(64)}],
  'not-an-object',
 ]) await assert.rejects(discover(input,env,lookup(headers),200));
 assert.equal(db.prepare('SELECT count(*) n FROM review_provider_identity_discoveries').get().n,0);
 const valid=[{header:'X-Other-Header',value:'irrelevant'},{header:'x-reviews-delivery-identity',value:delivery}];
 assert.deepEqual(await discover(input,env,lookup(valid),200),{status:'matched_needs_operator',events:['delivered']});
 assert.equal(db.prepare('SELECT count(*) n FROM review_provider_identity_discoveries').get().n,1);
 assert.equal(db.prepare('SELECT state FROM invitation_generations').get().state,'uncertain');
});
