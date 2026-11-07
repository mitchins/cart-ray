/** Test-only independent OrderConfirmed subscriber. Queue messages are hints, not proof of settlement. */
const id = /^[A-Za-z0-9_-]{1,128}$/;
const hex = /^[0-9a-f]{64}$/;
const encoder = new TextEncoder();
const fields = ['environment', 'installation', 'issuer', 'outbox_id', 'signature', 'store', 'version'];

function keyBytes(value) {
  if (typeof value !== 'string' || !hex.test(value)) throw new Error('source key unavailable');
  return Uint8Array.from(value.match(/../g), x => parseInt(x, 16));
}

function validShape(message) {
  if (!message || typeof message !== 'object' || Array.isArray(message) ||
      Object.keys(message).length !== fields.length || !fields.every(field => Object.hasOwn(message, field)) || message.version !== 1 ||
      typeof message.store !== 'string' || !id.test(message.store) ||
      typeof message.issuer !== 'string' || !id.test(message.issuer) ||
      typeof message.installation !== 'string' || !id.test(message.installation) ||
      message.environment !== 'test' || !Number.isSafeInteger(message.outbox_id) || message.outbox_id < 1 ||
      typeof message.signature !== 'string' || !hex.test(message.signature)) return false;
  return true;
}

async function authenticate(message, env) {
  if (message.store !== env.SOURCE_STORE || message.environment !== env.REVIEWS_ENVIRONMENT ||
      message.installation !== env.SOURCE_INSTALLATION || message.issuer !== env.SOURCE_ISSUER) return false;
  const key = await crypto.subtle.importKey('raw', keyBytes(env.SOURCE_EVENT_KEY), {name:'HMAC', hash:'SHA-256'}, false, ['verify']);
  const payload = JSON.stringify(['cartray-reviews-order-confirmed-v1', message.issuer, message.installation,
    message.store, message.environment, message.outbox_id]);
  return crypto.subtle.verify('HMAC', key, Uint8Array.from(message.signature.match(/../g), x => parseInt(x,16)), encoder.encode(payload));
}

export async function deliver(message, env) {
  if (env.REVIEWS_ENVIRONMENT !== 'test' || env.REVIEWS_ORDER_SUBSCRIBER !== 'test' ||
      typeof env.REVIEWS_AUTOMATION_TOKEN !== 'string' || env.REVIEWS_AUTOMATION_TOKEN.length < 32 ||
      !env.REVIEWS_API || !id.test(env.SOURCE_STORE || '') || !id.test(env.SOURCE_ISSUER || '') ||
      !id.test(env.SOURCE_INSTALLATION || '')) throw new Error('subscriber unavailable');
  // Validate the configured key even for malformed traffic: absent security configuration must not acknowledge messages.
  keyBytes(env.SOURCE_EVENT_KEY);
  if (!validShape(message)) return 'discard';
  // A well-formed message with a bad signature or scope can indicate a bad
  // installation or key rotation. Retry to the bounded DLQ for reconciliation;
  // acknowledging it here could permanently lose a committed order.
  if (!await authenticate(message, env)) throw new Error('source authentication failed');
  const controller = new AbortController();
  let timer, reader;
  try {
    const request = (async () => {
      const response = await env.REVIEWS_API.fetch('https://reviews.internal/reviews-internal/v1/automation-intake', {
        method:'POST', signal:controller.signal,
        headers:{'content-type':'application/json', authorization:`Bearer ${env.REVIEWS_AUTOMATION_TOKEN}`},
        body:JSON.stringify({outbox_id:message.outbox_id}),
      });
      if (!response.ok || !response.body) throw new Error('intake unavailable');
      reader = response.body.getReader();
      const decoder = new TextDecoder(); let size = 0, text = '';
      for (;;) {
        const part = await reader.read(); if (part.done) break;
        size += part.value.byteLength; if (size > 4096) throw new Error('intake response too large');
        text += decoder.decode(part.value,{stream:true});
      }
      text += decoder.decode();
      const receipt = JSON.parse(text);
      if (!receipt || !['accepted','pre_activation','late'].includes(receipt.status) ||
          !Number.isSafeInteger(receipt.due_at) || receipt.due_at < 0) throw new Error('invalid intake receipt');
    })();
    await Promise.race([request, new Promise((_,reject)=>{timer=setTimeout(()=>reject(new Error('intake timeout')),2000);})]);
  } finally {
    clearTimeout(timer); controller.abort();
    if (reader) void reader.cancel().catch(()=>{});
  }
  return 'acknowledged';
}

export default {
  async queue(batch, env) {
    for (const msg of batch.messages) {
      try { await deliver(msg.body, env); msg.ack(); }
      catch { msg.retry({delaySeconds:60}); }
    }
  },
};
