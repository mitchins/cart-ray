/** Test-only, addressed, durable due scheduling. This never authorizes email. */
const id = /^[A-Za-z0-9_-]{1,128}$/;
const MAX_DELAY = 43200; // Conservative per-message delay bound, seconds.

function configured(env) {
  if (env.REVIEWS_ENVIRONMENT !== 'test' || env.REVIEWS_DUE_SCHEDULER !== 'test' ||
      !id.test(env.REVIEWS_STORE || '') || !env.REVIEWS_DB?.prepare ||
      !env.SCHEDULE_QUEUE?.send || !env.DUE_QUEUE?.send) throw new Error('scheduler unavailable');
}
function token() {
  return Array.from(crypto.getRandomValues(new Uint8Array(32)), x => x.toString(16).padStart(2,'0')).join('');
}
function validateOutbox(outboxId) {
  if (!Number.isSafeInteger(outboxId) || outboxId < 1) throw new Error('invalid outbox identity');
}

/** Called only after authenticated intake; repeated calls re-enqueue the same stored claim. */
export async function schedule(outboxId, env, now = Math.floor(Date.now()/1000)) {
  configured(env); validateOutbox(outboxId);
  if (!Number.isSafeInteger(now) || now < 0) throw new Error('invalid clock');
  await env.REVIEWS_DB.prepare(`INSERT INTO review_automation_due_schedule
    (store_key,environment,acquisition_id,outbox_id,claim_token,due_at)
    SELECT store_key,environment,acquisition_id,outbox_id,?,due_at FROM review_automation_intents
    WHERE store_key=? AND environment='test' AND outbox_id=?
    ON CONFLICT(store_key,environment,acquisition_id) DO NOTHING`)
    .bind(token(), env.REVIEWS_STORE, outboxId).run();
  const row = await env.REVIEWS_DB.prepare(`SELECT acquisition_id,claim_token,due_at FROM review_automation_due_schedule
    WHERE store_key=? AND environment='test' AND outbox_id=?`).bind(env.REVIEWS_STORE,outboxId).first();
  if (!row) return 'not_accepted'; // Includes pre-activation and late receipts.
  await env.SCHEDULE_QUEUE.send({outbox_id:outboxId}, {delaySeconds:Math.min(MAX_DELAY, Math.max(0,row.due_at-now))});
  return 'scheduled';
}

/** Queue messages contain only an outbox hint; the persisted schedule is authoritative. */
export async function deliverHint(message, env, now = Math.floor(Date.now()/1000)) {
  configured(env);
  if (!message || typeof message !== 'object' || Array.isArray(message) || Object.keys(message).length !== 1 ||
      !Number.isSafeInteger(message.outbox_id) || message.outbox_id < 1) return 'discard';
  if (!Number.isSafeInteger(now) || now < 0) throw new Error('invalid clock');
  const row = await env.REVIEWS_DB.prepare(`SELECT acquisition_id,claim_token,due_at FROM review_automation_due_schedule
    WHERE store_key=? AND environment='test' AND outbox_id=?`).bind(env.REVIEWS_STORE,message.outbox_id).first();
  if (!row) return 'discard';
  if (row.due_at > now) {
    await env.SCHEDULE_QUEUE.send(message, {delaySeconds:Math.min(MAX_DELAY,row.due_at-now)});
    return 'deferred';
  }
  await env.DUE_QUEUE.send({acquisition_id:row.acquisition_id,claim_token:row.claim_token});
  return 'enqueued';
}
async function requestSchedule(request, env) {
  if (env.REVIEWS_ENVIRONMENT !== 'test' || env.REVIEWS_DUE_SCHEDULER !== 'test' ||
      typeof env.REVIEWS_DUE_SCHEDULER_TOKEN !== 'string' || env.REVIEWS_DUE_SCHEDULER_TOKEN.length < 32)
    return new Response(null,{status:503});
  const provided = request.headers.get('authorization') || '';
  const expected = `Bearer ${env.REVIEWS_DUE_SCHEDULER_TOKEN}`;
  let difference = provided.length ^ expected.length;
  for (let i=0; i<expected.length; i++) difference |= expected.charCodeAt(i) ^ (provided.charCodeAt(i) || 0);
  if (difference) return new Response(null,{status:401});
  if (request.method !== 'POST' || new URL(request.url).pathname !== '/schedule') return new Response(null,{status:404});
  if (request.headers.get('content-type') !== 'application/json' || Number(request.headers.get('content-length')) > 128)
    return new Response(null,{status:400});
  // A missing or forged content-length cannot authorize an unbounded read.
  if (!request.body) return new Response(null,{status:400});
  const reader = request.body.getReader(); let size = 0; const chunks = [];
  try {
    for (;;) { const item = await reader.read(); if (item.done) break; size += item.value.length;
      if (size > 128) { void reader.cancel().catch(()=>{}); return new Response(null,{status:400}); }
      chunks.push(item.value); }
    const data = new Uint8Array(size); let offset=0;
    for (const chunk of chunks) { data.set(chunk,offset); offset+=chunk.length; }
    const body=JSON.parse(new TextDecoder().decode(data));
    if (!body || typeof body !== 'object' || Array.isArray(body) || Object.keys(body).length !== 1 ||
        !Number.isSafeInteger(body.outbox_id) || body.outbox_id < 1) return new Response(null,{status:400});
    return Response.json({status:await schedule(body.outbox_id,env)});
  } catch { return new Response(null,{status:503}); }
}
export default {fetch:requestSchedule, async queue(batch,env) {
  for (const message of batch.messages) {
    try { await deliverHint(message.body,env); message.ack(); }
    catch { message.retry({delaySeconds:60}); }
  }
}};
