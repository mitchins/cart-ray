/** Real Workerd and D1 execution; provider boundary is intentionally synthetic. */
import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {Miniflare} from 'miniflare';
const generation='a'.repeat(64),delivery='b'.repeat(64);
test('private SMTP2GO activity evidence persists idempotently in Workerd D1',async()=>{
 const script=readFileSync(new URL('../provider-reconciliation/worker.mjs',import.meta.url),'utf8');
 const eventDate=new Date().toISOString();
 const outboundService={type:'fetcher',handler:async request=>{assert.equal(new URL(request.url).pathname,'/v3/activity/search');const body=await request.json();assert.equal(body.search_email_id,'smtp_123');return Response.json({data:{events:[{email_id:'smtp_123',event:'delivered',date:eventDate}],total_events:1}})}};
 const mf=new Miniflare({workers:[{config:{name:'provider-reconciliation',type:'worker',compatibilityDate:'2026-08-31',
  manifest:{mainModule:'worker.mjs',modules:{'worker.mjs':{type:'esm',contents:script}}},env:{
   REVIEWS_DB:{type:'d1',id:'reconcile-test'},REVIEWS_ENVIRONMENT:{type:'json',value:'test'},
   SMTP2GO_RECONCILIATION:{type:'json',value:'test'},REVIEWS_STORE:{type:'json',value:'acorn'},
   SMTP2GO_ACTIVITY_KEY:{type:'json',value:'api-synthetic'},RECONCILIATION_TOKEN:{type:'json',value:'z'.repeat(40)}}},dev:{outboundService}}]});
 try{
  const db=await mf.getD1Database('REVIEWS_DB');
  await db.exec('CREATE TABLE invitation_generations(store_key TEXT,environment TEXT,acquisition_id TEXT,generation_id TEXT,PRIMARY KEY(store_key,environment,acquisition_id,generation_id));');
  const migration=readFileSync(new URL('../migrations/0031_provider_delivery_receipts.sql',import.meta.url),'utf8');
  const tables=migration.match(/CREATE TABLE[\s\S]+?\);/g);
  assert.equal(tables.length,3);await db.batch(tables.map(sql=>db.prepare(sql)));
  await db.prepare('INSERT INTO invitation_generations VALUES (?,?,?,?)').bind('acorn','test','purchase',generation).run();
  await db.prepare('INSERT INTO review_provider_deliveries VALUES (?,?,?,?,?,?,?,?)').bind('acorn','test','purchase',generation,delivery,'smtp2go','smtp_123',Math.floor(Date.now()/1000)-30).run();
  const input={acquisition_id:'purchase',generation_id:generation,delivery_identity:delivery};
  for(let i=0;i<2;i++){
   const response=await mf.dispatchFetch('https://reconcile.invalid/reconcile',{method:'POST',headers:{'Content-Type':'application/json',Authorization:'Bearer '+'z'.repeat(40)},body:JSON.stringify(input)});
   assert.equal(response.status,200);assert.deepEqual(await response.json(),{status:'observed',events:['delivered']});
  }
  assert.equal((await db.prepare('SELECT count(*) n FROM review_provider_activity').first()).n,1);
 }finally{await mf.dispose()}
});
test('test-only SMTP2GO recovery adapter returns receipt through Workerd',async()=>{
 const script=readFileSync(new URL('../smtp2go-recovery/worker.mjs',import.meta.url),'utf8');
 const outboundService={type:'fetcher',handler:async request=>{assert.equal(new URL(request.url).pathname,'/v3/email/send');const body=await request.json();assert.equal(body.custom_headers[0].value,delivery);return Response.json({data:{succeeded:1,failed:0,failures:[],email_id:'smtp_recovery_1'}})}};
 const mf=new Miniflare({workers:[{config:{name:'recovery-mail',type:'worker',compatibilityDate:'2026-08-31',manifest:{mainModule:'worker.mjs',modules:{'worker.mjs':{type:'esm',contents:script}}},env:{
  REVIEWS_ENVIRONMENT:{type:'json',value:'test'},SMTP2GO_RECOVERY_MODE:{type:'json',value:'test'},APPROVED_TEST_EMAIL:{type:'json',value:'test@example.org'},RECOVERY_FROM:{type:'json',value:'reviews@example.org'},SMTP2GO_API_KEY:{type:'json',value:'api-synthetic'},SMTP2GO_MAIL_TOKEN:{type:'json',value:'x'.repeat(40)}}},dev:{outboundService}}]});
 try{const response=await mf.dispatchFetch('https://mail.invalid/send',{method:'POST',headers:{'Content-Type':'application/json',Authorization:'Bearer '+'x'.repeat(40)},body:JSON.stringify({from:'reviews@example.org',to:'test@example.org',subject:'Review link',text:'Synthetic link',headers:{'X-Reviews-Delivery-Identity':delivery}})});assert.equal(response.status,200);assert.deepEqual(await response.json(),{email_id:'smtp_recovery_1'});}finally{await mf.dispose()}
});
