/** Exercise the real bridge against the local Reviews Python Worker and D1. */
import assert from 'node:assert/strict';
import {webcrypto} from 'node:crypto';
import {handleConsent} from '../capture.mjs';
globalThis.crypto ??= webcrypto;
const outbox=Number(process.env.ACORN_TEST_OUTBOX), event=process.env.ACORN_TEST_EVENT, base=process.env.ACORN_TEST_REVIEWS_BASE;
const recipient='a'.repeat(64), recipientEvidence={outbox_id:outbox,recipient_context_hash:recipient,binding_version:'v1',evidence_reference:'possession_test',verified_at:1};
const grant={store:'example-store',environment:'test',purpose:'review_invitation',decision:'granted',event_id:event,recipient_context_hash:recipient,evidence_reference:'synthetic-consent',granted_at:1};
const env={STOREFRONT_ORIGIN:'https://acorn.test',REVIEWS_STORE_KEY:'example-store',REVIEWS_ENVIRONMENT:'test',REVIEW_CONSENT_ENABLED:'test',
 REVIEWS_CONSENT_SECRET:'e'.repeat(40),REVIEWS_CONSENT_RATE_KEY:'r'.repeat(40),REVIEWS_RECIPIENT_EVIDENCE_SECRET:'p'.repeat(40),
 REVIEW_CONSENT_LIMITER:{limit:async()=>({success:true})},
 REVIEW_CONSENT_AUTHORITY:{fetch:async(url)=>Response.json(url.endsWith('/verify')?{verified:true,recipient_evidence:recipientEvidence}:grant)},
 REVIEWS:{fetch:async(url,opts)=>fetch(base+new URL(url).pathname,opts)}};
const call=(path,body)=>handleConsent(new Request('https://acorn.test/review-consent/v1/'+path,{method:'POST',headers:{Origin:'https://acorn.test','Content-Type':'application/json','CF-Connecting-IP':'192.0.2.1',Cookie:'synthetic=opaque'},body:JSON.stringify(body)}),env);
const verified=await call('verify',{code:'123456'}); assert.equal(verified.status,200);
const duplicate=await call('verify',{code:'123456'}); assert.equal(duplicate.status,200);
const recorded=await call('grant',{accept:true}); assert.equal(recorded.status,200);
console.log('Bridge signed recipient and consent accepted by real Reviews Python Worker/D1');
