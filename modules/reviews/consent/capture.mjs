/** Purpose-specific consent bridge. Bind only to an independently authenticated consent authority. */
const encoder = new TextEncoder();
const idPattern = /^[A-Za-z0-9_-]{1,128}$/;
const referencePattern = /^[A-Za-z0-9_-]{1,200}$/;
const hashPattern = /^[a-f0-9]{64}$/;

function response(status, body) {
  return new Response(JSON.stringify(body), {status, headers: {"Content-Type": "application/json", "Cache-Control": "no-store"}});
}

function validOrigin(value) {
  try {
    const parsed = new URL(value);
    return parsed.protocol === "https:" && parsed.origin === value && !parsed.username && !parsed.password;
  } catch { return false; }
}

async function signature(secret, value) {
  const key = await crypto.subtle.importKey("raw", encoder.encode(secret), {name: "HMAC", hash: "SHA-256"}, false, ["sign"]);
  const bytes = await crypto.subtle.sign("HMAC", key, encoder.encode(value));
  return [...new Uint8Array(bytes)].map((x) => x.toString(16).padStart(2, "0")).join("");
}

async function readSmallBody(request) {
  const length = request.headers.get("Content-Length");
  if (length !== null && (!/^\d{1,3}$/.test(length) || Number(length) > 128)) throw new Error("invalid length");
  const reader = request.body?.getReader();
  if (!reader) throw new Error("missing body");
  let timer;
  const deadline = new Promise((_, reject) => { timer = setTimeout(() => reject(new Error("timeout")), 2000); });
  try {
    let size = 0, text = "";
    const decoder = new TextDecoder();
    while (true) {
      const part = await Promise.race([reader.read(), deadline]);
      if (part.done) return text + decoder.decode();
      size += part.value.byteLength;
      if (size > 128) throw new Error("large body");
      text += decoder.decode(part.value, {stream: true});
    }
  } finally {
    clearTimeout(timer);
    void reader.cancel().catch(() => {});
  }
}

/** The authority MUST authenticate the browser and persist an explicit decision before returning evidence. */
export async function handleConsent(request, env) {
  const path = new URL(request.url).pathname;
  if (path !== "/review-consent/v1/context" && path !== "/review-consent/v1/grant") return response(404, {error: "unavailable"});
  if (!validOrigin(env.STOREFRONT_ORIGIN) || !idPattern.test(env.REVIEWS_STORE_KEY || "") ||
      env.REVIEWS_ENVIRONMENT !== "test" || env.REVIEW_CONSENT_ENABLED !== "test" ||
      !env.REVIEW_CONSENT_AUTHORITY?.fetch || !env.REVIEWS?.fetch ||
      !env.REVIEW_CONSENT_LIMITER?.limit || typeof env.REVIEWS_CONSENT_SECRET !== "string" ||
      env.REVIEWS_CONSENT_SECRET.length < 32) return response(503, {error: "unavailable"});
  if (request.headers.get("Origin") !== env.STOREFRONT_ORIGIN) return response(403, {error: "unavailable"});
  if (request.method !== "POST") return response(405, {error: "unavailable"});
  if ((request.headers.get("Content-Type") || "").toLowerCase() !== "application/json") return response(400, {error: "unavailable"});
  let input;
  try { input = JSON.parse(await readSmallBody(request)); } catch { return response(400, {error: "unavailable"}); }
  const context = path.endsWith("context");
  if (!input || Array.isArray(input) || (context ? Object.keys(input).length !== 0 :
      Object.keys(input).length !== 1 || input.accept !== true)) return response(400, {error: "unavailable"});
  const ip = request.headers.get("CF-Connecting-IP");
  if (!ip || ip.length > 64) return response(503, {error: "unavailable"});
  try {
    if (!(await env.REVIEW_CONSENT_LIMITER.limit({key: ip})).success) return response(429, {error: "unavailable"});
    const headers = new Headers();
    const cookie = request.headers.get("Cookie");
    if (cookie && cookie.length <= 4096) headers.set("Cookie", cookie);
    headers.set("Content-Type", "application/json");
    const authorityResponse = await env.REVIEW_CONSENT_AUTHORITY.fetch("https://consent-authority.internal" + path, {
      method: "POST", headers, body: context ? "{}" : '{"accept":true}',
    });
    if (!authorityResponse.ok) return response(503, {error: "unavailable"});
    const data = await authorityResponse.json();
    if (context) return data?.available === true ? response(200, {available: true}) : response(503, {error: "unavailable"});
    if (!data || data.store !== env.REVIEWS_STORE_KEY || data.environment !== "test" ||
        data.purpose !== "review_invitation" || data.decision !== "granted" ||
        !idPattern.test(data.event_id || "") || !hashPattern.test(data.recipient_context_hash || "") ||
        !referencePattern.test(data.evidence_reference || "") || !Number.isSafeInteger(data.granted_at) ||
        data.granted_at <= 0 || data.granted_at > Math.floor(Date.now() / 1000)) return response(503, {error: "unavailable"});
    // Sort object keys to match the existing Reviews HMAC contract.
    const evidence = {event_id: data.event_id, recipient_context_hash: data.recipient_context_hash,
      evidence_reference: data.evidence_reference, granted_at: data.granted_at};
    const canonical = JSON.stringify(["reviews-consent-v1", env.REVIEWS_STORE_KEY, "test", {
      event_id: evidence.event_id, evidence_reference: evidence.evidence_reference,
      granted_at: evidence.granted_at, recipient_context_hash: evidence.recipient_context_hash,
    }]);
    const signed = await signature(env.REVIEWS_CONSENT_SECRET, canonical);
    const result = await env.REVIEWS.fetch("https://reviews.internal/reviews-internal/v1/consents", {
      method: "POST", headers: {"Content-Type": "application/json", "X-Reviews-Consent-Signature": signed}, body: JSON.stringify(evidence),
    });
    return result.ok ? response(200, {recorded: true}) : response(503, {error: "unavailable"});
  } catch { return response(503, {error: "unavailable"}); }
}

export default {fetch: handleConsent};
