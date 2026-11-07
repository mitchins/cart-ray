/** Synthetic due-event consumer. A held evaluation never authorizes delivery. */
const idPattern = /^[A-Za-z0-9_-]{1,128}$/;
const tokenPattern = /^[a-f0-9]{64}$/;

export async function evaluate(message, env) {
  if (env.REVIEWS_ENVIRONMENT !== "test" || env.REVIEWS_AUTOMATION_QUEUE !== "test" ||
      typeof env.REVIEWS_AUTOMATION_TOKEN !== "string" || env.REVIEWS_AUTOMATION_TOKEN.length < 32 ||
      !env.REVIEWS_API) throw new Error("test queue unavailable");
  if (!message || typeof message !== "object" || Array.isArray(message) ||
      Object.keys(message).length !== 2 || !Object.hasOwn(message, "acquisition_id") ||
      !Object.hasOwn(message, "claim_token") ||
      typeof message.acquisition_id !== "string" || !idPattern.test(message.acquisition_id) ||
      typeof message.claim_token !== "string" || !tokenPattern.test(message.claim_token)) return "discard";
  const controller = new AbortController();
  let timer, reader;
  try {
    const request = (async () => {
      const response = await env.REVIEWS_API.fetch("https://reviews.internal/reviews-internal/v1/automation-due-evaluate", {
        method: "POST", signal: controller.signal,
        headers: {"content-type": "application/json", authorization: `Bearer ${env.REVIEWS_AUTOMATION_TOKEN}`},
        body: JSON.stringify(message),
      });
      if (!response.ok || !response.body) throw new Error("evaluation unavailable");
      reader = response.body.getReader();
      let size = 0, text = "";
      const decoder = new TextDecoder();
      while (true) {
        const part = await reader.read();
        if (part.done) break;
        size += part.value.byteLength;
        if (size > 4096) throw new Error("evaluation response too large");
        text += decoder.decode(part.value, {stream:true});
      }
      text += decoder.decode();
      if (JSON.parse(text)?.status !== "held") throw new Error("evaluation not due or unavailable");
    })();
    await Promise.race([request, new Promise((_, reject) => {
      timer = setTimeout(() => reject(new Error("evaluation timed out")), 2000);
    })]);
  } finally {
    clearTimeout(timer);
    controller.abort();
    // Cancellation may itself wait on an unresponsive stream: do not await it.
    if (reader) void reader.cancel().catch(() => {});
  }
  return "held";
}

export default {
  async queue(batch, env) {
    // Process sequentially to keep resource use bounded; each evaluation has a deadline.
    for (const message of batch.messages) {
      try { await evaluate(message.body, env); message.ack(); }
      catch (_) { /* A failure must be retried, not acknowledged. */ message.retry({delaySeconds: 60}); }
    }
  },
};
