import {test} from "node:test";
import assert from "node:assert/strict";
import worker, {processCommand} from "../dispatch/worker.mjs";

const recipient = "buyer@example.test";
const digest = async text => Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text))), x => x.toString(16).padStart(2, "0")).join("");

async function fixture() {
  let state = "pending", sends = 0, outcomeAttempts = 0, breakOutcome = false, apiCalls = 0;
  const hash = await digest(recipient);
  const pending = {acquisition_id:"order", request_id:"a".repeat(64), source_event_id:"event", recipient_context_hash:hash, outbox_id:10};
  const api = {async fetch(url, init) {
    apiCalls++;
    const path = new URL(url).pathname;
    const expected = path.endsWith("delivery-claim") || path.endsWith("delivery-outcome") ? "delivery" : "issuer";
    assert.equal(init.headers.authorization, `Bearer ${expected}`);
    if (path.endsWith("recovery-command")) return Response.json({pending:state === "accepted" ? null : pending});
    if (path.endsWith("recovery-prepare")) return Response.json(state === "pending" ? {state:"prepared",token:"b".repeat(64),generation_id:"c".repeat(64),delivery_identity:"d".repeat(64)} : {state});
    if (path.endsWith("delivery-claim")) { if (state !== "pending") return new Response("", {status:409}); state="dispatching"; return Response.json({state}); }
    if (path.endsWith("delivery-outcome")) { outcomeAttempts++; if (breakOutcome) throw new Error("lost response"); state=JSON.parse(init.body).outcome; return Response.json({state}); }
    throw new Error(path);
  }};
  const env = {REVIEWS_ENVIRONMENT:"test", REVIEWS_ISSUER:"issuer", REVIEWS_DELIVERY_TOKEN:"delivery", REVIEWS_API:api,
    SYNTHETIC_RECIPIENT:recipient, SYNTHETIC_SENDER:"noreply@example.test", REVIEWS_LANDING_URL:"https://example.test/review/", REVIEWS_PUBLIC_STORE_NAME:"Store",
    MAIL:{async send(message) {sends++; assert.equal(message.to,recipient); assert.equal(message.text.split("\n").find(line => line.startsWith("https://")), "https://example.test/review/#invitation=" + "b".repeat(64));}}};
  return {env, command:{token_hash:"e".repeat(64),request_id:"f".repeat(64),issued_at:100}, sends:()=>sends, outcomeAttempts:()=>outcomeAttempts, apiCalls:()=>apiCalls, failOutcome:()=>{breakOutcome=true;}};
}

test("duplicate delivery commands cannot resend an accepted invitation", async () => {
  const f=await fixture();
  assert.equal(await processCommand(f.command,f.env,()=>101),"done");
  assert.equal(await processCommand({...f.command,request_id:"1".repeat(64)},f.env,()=>102),"done");
  assert.equal(f.sends(),1);
});

test("lost provider acknowledgement never resends a claimed generation", async () => {
  const f=await fixture(); f.failOutcome(); const delays=[];
  await assert.rejects(processCommand(f.command,f.env,()=>101,async ms=>{delays.push(ms);}));
  assert.deepEqual(delays,[1000,1000]);
  assert.equal(f.outcomeAttempts(),3);
  await assert.rejects(processCommand(f.command,f.env,()=>102));
  assert.equal(f.sends(),1);
});

test("queue retains failed commands for retry", async () => {
  const f=await fixture(); f.env.SYNTHETIC_RECIPIENT="other@example.test";
  let retries=0, acks=0;
  await worker.queue({messages:[{body:f.command,ack(){acks++;},retry(){retries++;}}]},f.env);
  assert.equal(retries,1); assert.equal(acks,0); assert.equal(f.sends(),0);
});

test("future and stale commands remain retryable, not silently acknowledged", async () => {
  const f=await fixture();
  let retries=0, acks=0;
  const message={body:{...f.command,issued_at:Math.floor(Date.now()/1000)+60},ack(){acks++;},retry(){retries++;}};
  await worker.queue({messages:[message]},f.env);
  assert.equal(retries,1); assert.equal(acks,0); assert.equal(f.sends(),0);
  f.env.REVIEWS_API={async fetch(){return new Response("stale",{status:409});}};
  message.body.issued_at=1;
  await worker.queue({messages:[message]},f.env);
  assert.equal(retries,2); assert.equal(acks,0); assert.equal(f.sends(),0);
});

test("missing delivery configuration fails closed", async () => {
  const f=await fixture(); delete f.env.MAIL;
  let retries=0, acks=0;
  await worker.queue({messages:[{body:f.command,ack(){acks++;},retry(){retries++;}}]},f.env);
  assert.equal(retries,1); assert.equal(acks,0); assert.equal(f.sends(),0);
});


test("invalid delivery configuration fails before any claim or API mutation", async () => {
  for (const [key, value] of [["REVIEWS_LANDING_URL", "http://example.test/review/"], ["REVIEWS_LANDING_URL", "https://example.test/?query=1"], ["SYNTHETIC_SENDER", ""], ["SYNTHETIC_SENDER", "invalid"], ["REVIEWS_PUBLIC_STORE_NAME", ""], ["REVIEWS_PUBLIC_STORE_NAME", "Store\nInjected"], ["REVIEWS_PUBLIC_STORE_NAME", "Store\x00Injected"]]) {
    const f=await fixture(); f.env[key]=value;
    await assert.rejects(processCommand(f.command,f.env,()=>101));
    assert.equal(f.apiCalls(),0); assert.equal(f.sends(),0);
  }
});

test('SMTP2GO recovery records the trusted provider ID while generic MAIL remains compatible', async () => {
  const f=await fixture();f.env.SYNTHETIC_MAIL_PROVIDER='smtp2go';let captured;
  const api=f.env.REVIEWS_API.fetch;
  f.env.REVIEWS_API.fetch=async(url,init)=>{if(url.endsWith('/delivery-outcome'))captured=JSON.parse(init.body);return api(url,init)};
  f.env.SMTP2GO_MAIL_TOKEN='x'.repeat(40);f.env.SMTP2GO_MAIL={fetch:async()=>Response.json({email_id:'smtp_recovery_1'})};
  f.env.AUTOMATION_RECIPIENT_TOKEN='r'.repeat(40);f.env.PURCHASE_EVIDENCE={fetch:async()=>Response.json({email:recipient,recipient_context_hash:await digest(recipient)})};
  assert.equal(await processCommand(f.command,f.env,()=>101),'done');assert.equal(captured.provider_email_id,'smtp_recovery_1');
});
test('SMTP2GO recovery with an ambiguous receipt remains uncertain and is never resent', async () => {
  const f=await fixture();f.env.SYNTHETIC_MAIL_PROVIDER='smtp2go';let calls=0;
  f.env.SMTP2GO_MAIL_TOKEN='x'.repeat(40);f.env.SMTP2GO_MAIL={fetch:async()=>{calls++;return Response.json({})}};
  f.env.AUTOMATION_RECIPIENT_TOKEN='r'.repeat(40);f.env.PURCHASE_EVIDENCE={fetch:async()=>Response.json({email:recipient,recipient_context_hash:await digest(recipient)})};
  await assert.rejects(processCommand(f.command,f.env,()=>101),/reconciliation/);
  await assert.rejects(processCommand(f.command,f.env,()=>102));assert.equal(calls,1);
});
test('SMTP2GO recovery requires the authoritative purchase recipient and binding', async () => {
 for(const response of [{email:'other@example.org',recipient_context_hash:await digest(recipient)}, {email:recipient,recipient_context_hash:'f'.repeat(64)}]) {
  const f=await fixture();f.env.SYNTHETIC_MAIL_PROVIDER='smtp2go';f.env.SMTP2GO_MAIL_TOKEN='x'.repeat(40);
  f.env.SMTP2GO_MAIL={fetch:async()=>{throw Error('must not send')}};f.env.AUTOMATION_RECIPIENT_TOKEN='r'.repeat(40);
  f.env.PURCHASE_EVIDENCE={fetch:async()=>Response.json(response)};
  await assert.rejects(processCommand(f.command,f.env,()=>101),/recipient/);assert.equal(f.sends(),0);
 }
});
