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
    REVIEW_CONSENT_ENABLED:"test", REVIEWS_CONSENT_SECRET:SECRET, REVIEWS_CONSENT_RATE_KEY:"r".repeat(40),
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
  assert.deepEqual(await context.json(), {available:true,verified:false});
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


test("admission groups IPv6 /64 and mapped IPv4 and rejects malformed addresses", async () => {
  const {env,calls} = setup();
  const keys = [];
  env.REVIEW_CONSENT_LIMITER.limit = async ({key}) => { keys.push(key); return {success:true}; };
  async function send(address) {
    return handleConsent(request("/review-consent/v1/context", {}, {"CF-Connecting-IP": address}), env);
  }
  assert.equal((await send("192.0.2.1")).status, 200);
  assert.equal((await send("::ffff:192.0.2.1")).status, 200);
  assert.equal((await send("::ffff:c000:201")).status, 200);
  assert.equal(keys[0], keys[1]);
  assert.equal(keys[1], keys[2]);
  assert.equal((await send("2001:db8:1234:5678::1")).status, 200);
  assert.equal((await send("2001:0db8:1234:5678:ffff:ffff:ffff:ffff")).status, 200);
  assert.equal(keys[3], keys[4]);
  assert.equal((await send("2001:db8:1234:5679::1")).status, 200);
  assert.notEqual(keys[4], keys[5]);
  const before = calls.length;
  for (const ip of ["", "192.00.2.1", "192.0.2.256", "[::1]", "fe80::1%eth0", "bad::ip"]) {
    assert.equal((await send(ip)).status, 503);
  }
  assert.equal(calls.length, before);
  env.REVIEWS_CONSENT_RATE_KEY = SECRET;
  assert.equal((await send("192.0.2.1")).status, 503);
});

test("oversized authority responses fail closed before Reviews is called", async () => {
  const {env,calls} = setup();
  env.REVIEW_CONSENT_AUTHORITY.fetch = async () => new Response("x".repeat(4097));
  assert.equal((await handleConsent(request("/review-consent/v1/grant",{accept:true}),env)).status,503);
  assert.equal(calls.length,0);
});

test("stalled authority is aborted and fails closed", async () => {
  const {env} = setup();
  let aborted = false;
  env.REVIEW_CONSENT_AUTHORITY.fetch = async (_url, options) => new Promise((_resolve,reject) => {
    options.signal.addEventListener("abort", () => {aborted = true; reject(new Error("aborted"));});
  });
  const result = await handleConsent(request("/review-consent/v1/grant",{accept:true}),env);
  assert.equal(result.status,503);
  assert.equal(aborted,true);
});

test("stalled Reviews request is aborted and its outcome remains unavailable", async () => {
  const {env} = setup();
  let aborted = false;
  env.REVIEWS.fetch = async (_url, options) => new Promise((_resolve,reject) => {
    options.signal.addEventListener("abort", () => {aborted = true; reject(new Error("aborted"));});
  });
  const result = await handleConsent(request("/review-consent/v1/grant",{accept:true}),env);
  assert.equal(result.status,503);
  assert.equal(aborted,true);
});
test('verified recipient evidence is signed privately and retried on context without leaking to browser', async () => {
  const {env}=setup(), calls=[];
  const evidence={outbox_id:12,recipient_context_hash:'a'.repeat(64),binding_version:'v1',evidence_reference:'possession_1',verified_at:10};
  env.REVIEWS_RECIPIENT_EVIDENCE_SECRET='p'.repeat(40);
  env.REVIEW_CONSENT_AUTHORITY.fetch=async(url)=>Response.json(url.endsWith('/context') ? {available:true,verified:true,recipient_evidence:evidence} : {verified:true,recipient_evidence:evidence});
  env.REVIEWS.fetch=async(url,options)=>{calls.push([url,options]); return Response.json({status:'recorded'});};
  const verified=await handleConsent(request('/review-consent/v1/verify',{code:'123456'}),env);
  assert.deepEqual(await verified.json(),{verified:true}); assert.equal(calls.length,1);
  const canonical=JSON.stringify(['reviews-automation-recipient-v1','acorn-store','test',{binding_version:'v1',evidence_reference:'possession_1',outbox_id:12,recipient_context_hash:'a'.repeat(64),verified_at:10}]);
  assert.equal(calls[0][1].headers['X-Reviews-Recipient-Signature'],createHmac('sha256','p'.repeat(40)).update(canonical).digest('hex'));
  const context=await handleConsent(request('/review-consent/v1/context',{}),env);
  assert.deepEqual(await context.json(),{available:true,verified:true}); assert.equal(calls.length,2);
  env.REVIEWS.fetch=async()=>new Response(null,{status:503});
  const retriable=await handleConsent(request('/review-consent/v1/context',{}),env);
  assert.deepEqual(await retriable.json(),{available:true,verified:false});
});
