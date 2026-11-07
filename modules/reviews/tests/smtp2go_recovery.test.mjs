import {test} from 'node:test';import assert from 'node:assert/strict';
import worker,{send} from '../smtp2go-recovery/worker.mjs';
const identity='a'.repeat(64);
const env={REVIEWS_ENVIRONMENT:'test',SMTP2GO_RECOVERY_MODE:'test',APPROVED_TEST_EMAIL:'test@example.org',RECOVERY_FROM:'reviews@example.org',SMTP2GO_API_KEY:'api-test',SMTP2GO_MAIL_TOKEN:'x'.repeat(40)};
const message={from:'reviews@example.org',to:'test@example.org',subject:'Your link',text:'Synthetic review link',headers:{'X-Reviews-Delivery-Identity':identity}};
test('SMTP2GO recovery returns its provider identity for the claimed delivery',async()=>{
 let count=0;const fetcher=async(url,opts)=>{count++;assert.equal(url,'https://api.smtp2go.com/v3/email/send');const body=JSON.parse(opts.body);assert.deepEqual(body.to,['test@example.org']);assert.equal(body.custom_headers[0].value,identity);return Response.json({data:{succeeded:1,failed:0,failures:[],email_id:'id_1'}})};
 assert.deepEqual(await send(message,env,fetcher),{email_id:'id_1'});assert.equal(count,1);
});
test('uncertain provider response and errors do not assert acceptance',async()=>{
 for(const response of [()=>Response.json({data:{succeeded:1,failed:0,failures:[]}}),()=>new Response('',{status:503}),()=>{throw Error('network')}])await assert.rejects(send(message,env,response));
});
test('missing authorization, unapproved recipient, wrong sender and live mode never call provider',async()=>{
 let calls=0;const f=async()=>{calls++;};
 await assert.rejects(send({...message,to:'attacker@example.org'},env,f));await assert.rejects(send({...message,from:'other@example.org'},env,f));
 await assert.rejects(send(message,{...env,REVIEWS_ENVIRONMENT:'live'},f));assert.equal(calls,0);
 const request=new Request('https://private/send',{method:'POST',headers:{'content-type':'application/json',authorization:'Bearer wrong'},body:JSON.stringify(message)});
 assert.equal((await worker.fetch(request,env)).status,503);
});
