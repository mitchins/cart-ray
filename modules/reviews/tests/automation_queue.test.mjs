import {test} from "node:test";
import assert from "node:assert/strict";
import worker, {evaluate} from "../automation-queue/worker.mjs";

const message = {acquisition_id:"order_1", claim_token:"a".repeat(64)};
const env = () => ({REVIEWS_ENVIRONMENT:"test", REVIEWS_AUTOMATION_QUEUE:"test", REVIEWS_AUTOMATION_TOKEN:"t".repeat(40),
  REVIEWS_API:{async fetch(url, request) {
    assert.equal(new URL(url).pathname,"/reviews-internal/v1/automation-due-evaluate");
    assert.equal(request.headers.authorization, `Bearer ${"t".repeat(40)}`);
    assert.deepEqual(JSON.parse(request.body),message);
    return Response.json({status:"held"});
  }}});

test("acknowledges held receipts and discards malformed messages", async () => {
  const e=env(); let ack=0, retry=0;
  await worker.queue({messages:[{body:message,ack(){ack++;},retry(){retry++;}},
    {body:{...message,extra:1},ack(){ack++;},retry(){retry++;}}]},e);
  assert.equal(ack,2); assert.equal(retry,0);
});

test("early, failed or wrongly configured events retry and cannot send", async () => {
  for (const kind of ["early","outage","live","disabled"]) {
    const e=env(); let ack=0, retry=0;
    if(kind === "early") e.REVIEWS_API.fetch=async()=>Response.json({status:"unavailable"});
    if(kind === "outage") e.REVIEWS_API.fetch=async()=>{throw Error("unavailable");};
    if(kind === "live") e.REVIEWS_ENVIRONMENT="live";
    if(kind === "disabled") e.REVIEWS_AUTOMATION_QUEUE="disabled";
    await worker.queue({messages:[{body:message,ack(){ack++;},retry(options){assert.equal(options.delaySeconds,60);retry++;}}]},e);
    assert.equal(ack,0); assert.equal(retry,1);
  }
  assert.equal(await evaluate({acquisition_id:"order",claim_token:"bad"},env()),"discard");
});
