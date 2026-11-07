/** Exercise the checkout evidence payloads against the real Python Worker/D1. */
import assert from 'node:assert/strict';
import {createHmac, webcrypto} from 'node:crypto';
import {buildAutomationEvidence} from '../purchase_evidence.mjs';
globalThis.crypto ??= webcrypto;
const outbox=Number(process.env.ACORN_TEST_OUTBOX), event=process.env.ACORN_TEST_EVENT, base=process.env.ACORN_TEST_REVIEWS_BASE;
const result={outbox_id:outbox,event_id:event,acquisition_id:process.env.ACORN_TEST_ORDER,recipient_context_hash:'a'.repeat(64),binding_version:'v1',settled_at:1,
 grant:{wording_version:'review-invitation-v1',wording:'Email me one request to review my purchase.',granted_at:1,merchant_default_selected:0,selection_basis:'express_opt_in'}};
const env={RECIPIENT_HASH_KEY:'h'.repeat(40),REVIEWS_RECIPIENT_EVIDENCE_SECRET:'p'.repeat(40),REVIEWS_CONSENT_SECRET:'e'.repeat(40)};
for(const [route,header,secret,data,label] of await buildAutomationEvidence(result,env)) {
 const signature=createHmac('sha256',secret).update(JSON.stringify([label,'example-store','test',data])).digest('hex');
 const send=()=>fetch(base+'/reviews-internal/v1/'+route,{method:'POST',headers:{'content-type':'application/json',[header]:signature},body:JSON.stringify(data)});
 for(let n=0;n<2;n++) {const response=await send(); assert.ok(response.ok,`${route}: ${response.status} ${await response.text()}`);}
}
console.log('Automatic checkout evidence accepted and replayed by real Reviews Worker/D1');
