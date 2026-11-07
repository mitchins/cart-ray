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

// Canonicalize IPv4-mapped IPv6 into the same IPv4 bucket; other IPv6 uses /64.
function admissionAddress(value) {
  if (typeof value !== "string" || value.length > 64 || !/^[0-9a-fA-F:.]+$/.test(value)) throw new Error("invalid IP");
  if (!value.includes(":")) {
    const parts = value.split(".");
    if (parts.length !== 4 || parts.some((part) => !/^(0|[1-9][0-9]{0,2})$/.test(part) || Number(part) > 255)) {
      throw new Error("invalid IP");
    }
    return parts.join(".");
  }
  // URL parsing accepts compressed IPv6 and IPv4 suffixes, but rejects malformed addresses.
  const hostname = new URL(`http://[${value}]/`).hostname;
  if (!hostname.startsWith("[") || !hostname.endsWith("]")) throw new Error("invalid IP");
  const canonical = hostname.slice(1, -1);
  const sections = canonical.split("::");
  if (sections.length > 2) throw new Error("invalid IP");
  const left = sections[0] ? sections[0].split(":") : [];
  const right = sections.length === 2 && sections[1] ? sections[1].split(":") : [];
  const zeros = sections.length === 2 ? 8 - left.length - right.length : 0;
  if (zeros < 0) throw new Error("invalid IP");
  const groups = [...left, ...Array(zeros).fill("0"), ...right].map((part) => Number.parseInt(part, 16));
  if (groups.length !== 8 || groups.some((part) => !Number.isInteger(part) || part < 0 || part > 65535)) {
    throw new Error("invalid IP");
  }
  if (groups.slice(0, 5).every((part) => part === 0) && groups[5] === 65535) {
    return [groups[6] >> 8, groups[6] & 255, groups[7] >> 8, groups[7] & 255].join(".");
  }
  return groups.slice(0, 4).map((part) => part.toString(16).padStart(4, "0")).join(":") + "::/64";
}

async function readSmallBody(request, maximum = 128) {
  const length = request.headers.get("Content-Length");
  if (length !== null && (!/^\d{1,6}$/.test(length) || Number(length) > maximum)) throw new Error("invalid length");
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
      if (size > maximum) throw new Error("large body");
      text += decoder.decode(part.value, {stream: true});
    }
  } finally {
    clearTimeout(timer);
    void reader.cancel().catch(() => {});
  }
}

// Abort both an unanswered service call and its body. A timed-out provider outcome
// is unknown; the authority must return the same durable evidence on retry.
async function serviceRequest(binding, url, options, readJson, timeoutMs = 2000) {
  const controller = new AbortController();
  let timer;
  const deadline = new Promise((_, reject) => {
    timer = setTimeout(() => { controller.abort(); reject(new Error("service timeout")); }, timeoutMs);
  });
  try {
    return await Promise.race([
      (async () => {
        const result = await binding.fetch(url, {...options, signal: controller.signal});
        if (!result.ok) throw new Error("service unavailable");
        if (!readJson) {
          if (result.body) void result.body.cancel().catch(() => {});
          return null;
        }
        return JSON.parse(await readSmallBody(result, 4096));
      })(), deadline,
    ]);
  } finally {
    clearTimeout(timer);
    controller.abort();
  }
}

/** The authority MUST authenticate the browser and persist an explicit decision before returning evidence. */
export async function handleConsent(request, env) {
  const path = new URL(request.url).pathname;
  if (!["/review-consent/v1/context", "/review-consent/v1/challenge", "/review-consent/v1/verify", "/review-consent/v1/grant"].includes(path)) return response(404, {error: "unavailable"});
  if (!validOrigin(env.STOREFRONT_ORIGIN) || !idPattern.test(env.REVIEWS_STORE_KEY || "") ||
      env.REVIEWS_ENVIRONMENT !== "test" || env.REVIEW_CONSENT_ENABLED !== "test" ||
      !env.REVIEW_CONSENT_AUTHORITY?.fetch || !env.REVIEWS?.fetch ||
      !env.REVIEW_CONSENT_LIMITER?.limit || typeof env.REVIEWS_CONSENT_SECRET !== "string" ||
      env.REVIEWS_CONSENT_SECRET.length < 32 || typeof env.REVIEWS_CONSENT_RATE_KEY !== "string" ||
      env.REVIEWS_CONSENT_RATE_KEY.length < 32 || env.REVIEWS_CONSENT_RATE_KEY === env.REVIEWS_CONSENT_SECRET) return response(503, {error: "unavailable"});
  if (request.headers.get("Origin") !== env.STOREFRONT_ORIGIN) return response(403, {error: "unavailable"});
  if (request.method !== "POST") return response(405, {error: "unavailable"});
  if ((request.headers.get("Content-Type") || "").toLowerCase() !== "application/json") return response(400, {error: "unavailable"});
  const action = path.split('/').pop();
  try {
    const address = admissionAddress(request.headers.get("CF-Connecting-IP"));
    const limiterKey = await signature(env.REVIEWS_CONSENT_RATE_KEY,
      `consent-ingress-v1\0${env.REVIEWS_STORE_KEY}\0test\0${address}`);
    if (!(await env.REVIEW_CONSENT_LIMITER.limit({key: limiterKey})).success) return response(429, {error: "unavailable"});
    let input;
    try { input = JSON.parse(await readSmallBody(request)); } catch { return response(400, {error: "unavailable"}); }
    if (!input || Array.isArray(input) || (action === 'grant' ? Object.keys(input).length !== 1 || input.accept !== true :
        action === 'verify' ? Object.keys(input).length !== 1 || !/^[0-9]{6}$/.test(input.code || '') : Object.keys(input).length !== 0)) return response(400, {error: "unavailable"});
    const headers = new Headers();
    const cookie = request.headers.get("Cookie");
    if (cookie && cookie.length <= 4096) headers.set("Cookie", cookie);
    headers.set("Content-Type", "application/json");
    const data = await serviceRequest(env.REVIEW_CONSENT_AUTHORITY, "https://consent-authority.internal" + path, {
      method: "POST", headers, body: JSON.stringify(input),
    }, true, action === 'challenge' ? 9000 : 2000);
    if (action === 'context') return data?.available === true ? response(200, {available: true, verified:data.verified===true}) : response(503, {error: "unavailable"});
    if (action === 'challenge') return data?.sent === true ? response(200, {sent:true}) : response(503, {error: "unavailable"});
    if (action === 'verify') return data?.verified === true ? response(200, {verified:true}) : response(503, {error: "unavailable"});
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
    await serviceRequest(env.REVIEWS, "https://reviews.internal/reviews-internal/v1/consents", {
      method: "POST", headers: {"Content-Type": "application/json", "X-Reviews-Consent-Signature": signed}, body: JSON.stringify(evidence),
    }, false);
    return response(200, {recorded: true});
  } catch { return response(503, {error: "unavailable"}); }
}

export default {fetch: handleConsent};
