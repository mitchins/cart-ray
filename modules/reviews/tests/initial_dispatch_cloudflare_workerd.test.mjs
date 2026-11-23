/** Execute the real sender in Workerd using synthetic private services and provider. */
import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {Miniflare} from 'miniflare';

test('test-only Cloudflare initial sender runs in Workerd and never resends on replay', async () => {
  const sender=readFileSync(new URL('../initial-dispatch/worker.mjs',import.meta.url),'utf8');
  const wrapper=`import {dispatchInitial} from './sender.mjs';
let state='prepared', sends=0, recorded=null;
const h='a'.repeat(64), hash='b'.repeat(64), gen='c'.repeat(64), delivery='d'.repeat(64);
const env={REVIEWS_ENVIRONMENT:'test',INITIAL_SEND_MODE:'test',REVIEWS_EMAIL_PROVIDER:'cloudflare',
  REVIEWS_AUTOMATION_TOKEN:'a'.repeat(40),AUTOMATION_RECIPIENT_TOKEN:'b'.repeat(40),REVIEWS_DELIVERY_TOKEN:'c'.repeat(40),
  APPROVED_TEST_EMAIL:'test@example.org',TEST_SENDER:'reviews@example.org',MERCHANT_NAME:'Merchant',MERCHANT_CONTACT:'help@example.org',
  CLOUDFLARE_EMAIL_ACCOUNT_ID:'a'.repeat(32),CLOUDFLARE_EMAIL_API_TOKEN:'x'.repeat(40),
  REVIEWS_LANDING_URL:'https://store.example.org/review',UNSUBSCRIBE_BASE_URL:'https://store.example.org/unsubscribe',
  PURCHASE_EVIDENCE:{fetch:async()=>Response.json({email:'test@example.org',recipient_context_hash:hash})},
  REVIEWS_API:{fetch:async(url,opts)=>{const path=new URL(url).pathname;
    if(path.endsWith('automation-initial-prepare'))return Response.json({state,token:h,generation_id:gen,delivery_identity:delivery,acquisition_id:'order',recipient_context_hash:hash,unsubscribe_token:'12.'+'e'.repeat(64)});
    if(path.endsWith('automation-initial-claim')){state='dispatching';return Response.json({state})}
    if(path.endsWith('delivery-outcome')){recorded=JSON.parse(opts.body);state=recorded.outcome;return Response.json({state})}
    return new Response('',{status:404})}}};
const provider=async(url,opts)=>{sends++;const msg=JSON.parse(opts.body);
  if(!url.startsWith('https://api.cloudflare.com/') || msg.to!=='test@example.org' ||
     msg.headers['List-Unsubscribe-Post']!=='List-Unsubscribe=One-Click') throw Error('bad request');
  return Response.json({success:true,errors:[],result:{message_id:'<cf@example.test>',delivered:[],queued:['test@example.org'],permanent_bounces:[],suppressed_recipients:[]}})};
export default {async fetch(){const first=await dispatchInitial({outbox_id:12},env,provider,()=>100);
 const second=await dispatchInitial({outbox_id:12},env,provider,()=>100);return Response.json({first,second,sends,recorded})}};`;
  const mf=new Miniflare({workers:[{config:{name:'reviews-cf-test',type:'worker',compatibilityDate:'2026-08-31',
    manifest:{mainModule:'wrapper.mjs',modules:{'wrapper.mjs':{type:'esm',contents:wrapper},
      'sender.mjs':{type:'esm',contents:sender}}}}}]});
  try {
    const response=await mf.dispatchFetch('https://test.invalid/');assert.equal(response.status,200);
    const data=await response.json();assert.equal(data.first,'sent');assert.equal(data.second,'done');assert.equal(data.sends,1);
    assert.equal(data.recorded.provider,'cloudflare');assert.equal(data.recorded.provider_email_id,'<cf@example.test>');
  } finally {await mf.dispose()}
});
