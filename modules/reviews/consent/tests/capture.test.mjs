import {test} from "node:test";
import assert from "node:assert/strict";
import {createHmac, webcrypto} from "node:crypto";
import {handleConsent} from "../capture.mjs";

globalThis.crypto ??= webcrypto;
const ORIGIN = "https://acorn.test";
const SECRET = "s".repeat(40);
const EVIDENCE = {store:"acorn-store", environment:"test", purpose:"review_invitation", decision:"granted",
  event_id:"event_1", recipient_context_hash:"a".repeat(64), evidence_reference:"consent_1", granted_at:10};

function setup(evidence = EVIDENCE) {
  const calls = [];
  const env = {STOREFRONT_ORIGIN: ORIGIN, REVIEWS_STORE_KEY:"acorn-store", REVIEWS_ENVIRONMENT:"test",
    REVIEW_CONSENT_ENABLED:"test", REVIEWS_CONSENT_SECRET:SECRET,
    REVIEW_CONSENT_LIMITER:{limit:async () => ({success:true})},
    REVIEW_CONSENT_AUTHORITY:{fetch:async (url, options) => {
      calls.push([url, options]);
      return Response.json(url.endsWith("/context") ? {available:true} : evidence);
    }},
    REVIEWS:{fetch:async (url, options) => {calls.push([url, options]); return Response.json({status:"recorded"}, {status:201});}},
  };
  return {env, calls};
}
function request(path, body, headers={}) {
  return new Request(ORIGIN + path, {method:"POST", headers:{Origin:ORIGIN, "Content-Type":"application/json",
    "CF-Connecting-IP":"192.0.2.1", Cookie:"session=opaque", ...headers}, body:JSON.stringify(body)});
}

test("only an explicit decision crosses the trusted authority and Reviews receives signed scoped evidence", async () => {
  const {env,calls} = setup();
  const context = await handleConsent(request("/review-consent/v1/context", {}), env);
  assert.deepEqual(await context.json(), {available:true});
  assert.equal(calls.length, 1);
  const result = await handleConsent(request("/review-consent/v1/grant", {accept:true}), env);
  assert.deepEqual(await result.json(), {recorded:true});
  assert.equal(calls.length, 3);
  assert.equal(calls[1][1].headers.get("Cookie"), "session=opaque");
  assert.equal(calls[1][1].body, '{"accept":true}');
  const sent = JSON.parse(calls[2][1].body);
  assert.deepEqual(sent, {event_id:"event_1", recipient_context_hash:"a".repeat(64), evidence_reference:"consent_1", granted_at:10});
  const canonical = JSON.stringify(["reviews-consent-v1", "acorn-store", "test", {
    event_id:"event_1", evidence_reference:"consent_1", granted_at:10, recipient_context_hash:"a".repeat(64),
  }]);
  assert.equal(calls[2][1].headers["X-Reviews-Consent-Signature"], createHmac("sha256", SECRET).update(canonical).digest("hex"));
});

test("untrusted browser cannot select recipient or bypass decision, origin, rate limit or scope", async () => {
  for (const [payload, headers] of [[{accept:false},{}], [{accept:true,event_id:"forged"},{}], [{accept:true},{Origin:"https://evil.test"}]]) {
    const {env,calls} = setup();
    assert.notEqual((await handleConsent(request("/review-consent/v1/grant",payload,headers),env)).status,200);
    assert.equal(calls.length,0);
  }
  for (const key of ["REVIEW_CONSENT_AUTHORITY", "REVIEWS", "REVIEW_CONSENT_LIMITER"]) {
    const {env,calls} = setup(); delete env[key];
    assert.equal((await handleConsent(request("/review-consent/v1/grant",{accept:true}),env)).status,503);
    assert.equal(calls.length,0);
  }
  for (const evidence of [{...EVIDENCE,store:"other"}, {...EVIDENCE,purpose:"marketing"}, {...EVIDENCE,decision:"denied"}]) {
    const {env,calls} = setup(evidence);
    assert.equal((await handleConsent(request("/review-consent/v1/grant",{accept:true}),env)).status,503);
    assert.equal(calls.length,1);
  }
});

test("authority failure, Reviews outage and live configuration fail closed", async () => {
  const {env,calls} = setup();
  env.REVIEWS.fetch = async () => new Response(null, {status:503});
  assert.equal((await handleConsent(request("/review-consent/v1/grant",{accept:true}),env)).status,503);
  assert.equal(calls.length,1);
  env.REVIEWS_ENVIRONMENT = "live";
  assert.equal((await handleConsent(request("/review-consent/v1/grant",{accept:true}),env)).status,503);
});
