import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {DatabaseSync} from 'node:sqlite';
import {sendTestCode} from '../test_email_transport.mjs';
const context='a'.repeat(64), reference='ref_'+'b'.repeat(64), recipient='test@example.com';
function setup() {
 const db=new DatabaseSync(':memory:'); db.exec(readFileSync(new URL('../migrations/0001_purchaser_context.sql',import.meta.url),'utf8'));
 const contextDB={prepare(sql){return {bind(...v){return {async first(){return db.prepare(sql).get(...v) ?? null},async run(){return db.prepare(sql).run(...v)}}}}}};
 const env={TEST_EMAIL_MODE:'test',CONTEXT_DB:contextDB,PURCHASE_TRANSPORT_TOKEN:'t'.repeat(40),RECIPIENT_RESOLVE_TOKEN:'r'.repeat(40),SMTP2GO_API_KEY:'api-testonly',TEST_SENDER:'reviews@acorn-test.effectivepolicy.com.au',APPROVED_TEST_EMAIL:recipient,PURCHASE_EVIDENCE:{fetch:async()=>Response.json({email:recipient})}};
 let calls=0, result={data:{succeeded:1,failed:0,failures:[],email_id:'test-receipt'}};
 const fetcher=async(_url,opts)=>{calls++;assert.equal(opts.redirect,'error');return Response.json(result)};
 const req=(token=env.PURCHASE_TRANSPORT_TOKEN)=>new Request('https://private/send',{method:'POST',headers:{'content-type':'application/json',Authorization:'Bearer '+token},body:JSON.stringify({context_hash:context,recipient_reference:reference,code:'123456'})});
 return {db,env,req,fetcher,count:()=>calls,setResult(x){result=x}};
}
test('one approved verification code is accepted once and duplicate cannot resend',async()=>{
 const x=setup(); const first=await sendTestCode(x.req(),x.env,x.fetcher); assert.equal(first.status,200);assert.deepEqual(await first.json(),{accepted:true});
 assert.equal((await sendTestCode(x.req(),x.env,x.fetcher)).status,503);assert.equal(x.count(),1);
 assert.equal(x.db.prepare('SELECT delivery_state FROM purchaser_test_email_claims').get().delivery_state,'accepted');
});
test('failed or ambiguous provider acceptance is retained and never blindly retried',async()=>{
 const x=setup(); x.setResult({data:{succeeded:0,failed:1,failures:['failed']}});
 assert.equal((await sendTestCode(x.req(),x.env,x.fetcher)).status,503);
 assert.equal((await sendTestCode(x.req(),x.env,x.fetcher)).status,503);assert.equal(x.count(),1);
 assert.equal(x.db.prepare('SELECT delivery_state FROM purchaser_test_email_claims').get().delivery_state,'uncertain');
});
test('unauthorized request, wrong resolved recipient or production configuration cannot send',async()=>{
 const x=setup(); assert.equal((await sendTestCode(x.req('wrong'),x.env,x.fetcher)).status,503);
 x.env.PURCHASE_EVIDENCE.fetch=async()=>Response.json({email:'wrong@example.com'});
 assert.equal((await sendTestCode(x.req(),x.env,x.fetcher)).status,503);assert.equal(x.count(),0);
 x.env.TEST_EMAIL_MODE='production';assert.equal((await sendTestCode(x.req(),x.env,x.fetcher)).status,503);
});
