/** Synthetic due-event consumer. A held evaluation never authorizes delivery. */
const idPattern = /^[A-Za-z0-9_-]{1,128}$/;
const tokenPattern = /^[a-f0-9]{64}$/;

export async function evaluate(message, env) {
  if (env.REVIEWS_ENVIRONMENT !== "test" || env.REVIEWS_AUTOMATION_QUEUE !== "test" ||
      typeof env.REVIEWS_AUTOMATION_TOKEN !== "string" || env.REVIEWS_AUTOMATION_TOKEN.length < 32 ||
      !env.REVIEWS_API) throw new Error("test queue unavailable");
  if (!message || typeof message !== "object" || Array.isArray(message) ||
      Object.keys(message).sort().join(",") !== "acquisition_id,claim_token" ||
      typeof message.acquisition_id !== "string" || !idPattern.test(message.acquisition_id) ||
      typeof message.claim_token !== "string" || !tokenPattern.test(message.claim_token)) return "discard";
  const response = await env.REVIEWS_API.fetch("https://reviews.internal/reviews-internal/v1/automation-due-evaluate", {
    method: "POST",
    headers: {"content-type": "application/json", authorization: `Bearer ${env.REVIEWS_AUTOMATION_TOKEN}`},
    body: JSON.stringify(message),
  });
  if (!response.ok) throw new Error("evaluation unavailable");
  const result = await response.json();
  if (result?.status !== "held") throw new Error("evaluation not due or unavailable");
  return "held";
}

export default {
  async queue(batch, env) {
    for (const message of batch.messages) {
      try { await evaluate(message.body, env); message.ack(); }
      catch (_) { message.retry({delaySeconds: 60}); }
    }
  },
};
