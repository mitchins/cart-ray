/** Real Miniflare queue delivery to the independently running local Reviews Worker. */
import assert from "node:assert/strict";
import {Miniflare} from "miniflare";
import {readFileSync} from "node:fs";

const [base, acquisition, bearer] = process.argv.slice(2);
const event = {acquisition_id:acquisition, claim_token:"a".repeat(64)};
let seen = 0, finish, firstDelivery, dlqFinish, unavailable = 0;
const deadLetter = new Promise(resolve => { dlqFinish = resolve; });
const observed = new Promise(resolve => { finish = resolve; });
const mf = new Miniflare({
  workers:[{config:{
    name:"synthetic-consumer", type:"worker", compatibilityDate:"2026-08-31",
    triggers:[{type:"queue",name:"synthetic-due",maxBatchSize:1,maxBatchTimeout:1,maxRetries:1,deadLetterQueue:"synthetic-due-dlq"}],
    manifest:{mainModule:"worker.mjs",modules:{"worker.mjs":{
      type:"esm", contents:readFileSync(new URL("../automation-queue/worker.mjs", import.meta.url),"utf8"),
    }}},
    env:{
      REVIEWS_ENVIRONMENT:{type:"json",value:"test"}, REVIEWS_AUTOMATION_QUEUE:{type:"json",value:"test"},
      REVIEWS_AUTOMATION_TOKEN:{type:"json",value:bearer}, DUE:{type:"queue",name:"synthetic-due"},
      REVIEWS_API:{type:"fetcher",handler:async request => {
        const response = await fetch(base + new URL(request.url).pathname, {
          method:request.method, headers:request.headers, body:await request.text(),
        });
        const body = await response.clone().json();
        if (body.status === "unavailable") unavailable++;
        if (response.ok && body.status === "held") {
          firstDelivery ??= Date.now();
          if (++seen === 2) finish();
        }
        return response;
      }},
    },
  }},{config:{
    name:"synthetic-dlq", type:"worker", compatibilityDate:"2026-08-31",
    triggers:[{type:"queue",name:"synthetic-due-dlq",maxBatchSize:1,maxBatchTimeout:1,maxRetries:1}],
    manifest:{mainModule:"dlq.mjs",modules:{"dlq.mjs":{type:"esm",contents:
      'export default {async queue(batch, env) {await env.NOTIFY.fetch("https://notify/"); for(const message of batch.messages) message.ack();}}'}}},
    env:{NOTIFY:{type:"fetcher",handler:async () => {dlqFinish();return new Response("ok");}}},
  }}],
});
try {
  const queue = await mf.getQueueProducer("DUE", "synthetic-consumer");
  const enqueuedAt = Date.now();
  await queue.send(event, {delaySeconds:1});
  await queue.send(event, {delaySeconds:1});
  let timer;
  try {
    await Promise.race([observed, new Promise((_, reject) => { timer = setTimeout(() => reject(Error("queue delivery timeout")), 30000); })]);
  } finally { clearTimeout(timer); }
  assert.equal(seen,2);
  assert.ok(firstDelivery - enqueuedAt >= 900, "queue dispatched before synthetic delay");
  await queue.send({acquisition_id:"missing", claim_token:"b".repeat(64)});
  try {
    await Promise.race([deadLetter, new Promise((_, reject) => { timer = setTimeout(() => reject(Error("queue DLQ timeout")), 85000); })]);
  } finally { clearTimeout(timer); }
  assert.equal(unavailable,2, "expected initial attempt and one runtime retry");
  process.stdout.write("Miniflare delayed/replayed due events and retried unavailable event to DLQ\n");
} finally { await mf.dispose(); }
