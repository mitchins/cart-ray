/** Test-only registered handler for a confirmed CartRay purchase action. */
const fail = status => Response.json({error: 'delivery unavailable'}, {status, headers: {'Cache-Control': 'no-store'}});
const identifier = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/;
const sessionId = /^cs_test_[A-Za-z0-9_]{1,200}$/;

function sendingProvider(env) {
  const provider = env.EMAIL_PROVIDER ?? 'smtp2go';
  if (provider === 'smtp2go' && env.SMTP2GO_API_KEY?.startsWith('api-')) return provider;
  if (provider === 'cloudflare' && /^[a-f0-9]{32}$/.test(env.CLOUDFLARE_EMAIL_ACCOUNT_ID || '') &&
      typeof env.CLOUDFLARE_EMAIL_API_TOKEN === 'string' &&
      /^[\x21-\x7e]{20,1024}$/.test(env.CLOUDFLARE_EMAIL_API_TOKEN)) return provider;
  return null;
}

async function sendEmail(env, config, provider, deadline, fetcher) {
  const text = 'This test purchase contains dummy materials only.\n\n' + config.links.join('\n');
  const cloudflare = provider === 'cloudflare';
  const sent = await fetcher(cloudflare
    ? `https://api.cloudflare.com/client/v4/accounts/${env.CLOUDFLARE_EMAIL_ACCOUNT_ID}/email/sending/send`
    : 'https://api.smtp2go.com/v3/email/send', {
    method: 'POST', redirect: 'manual', signal: deadline,
    headers: {'Content-Type': 'application/json', ...(cloudflare
      ? {Authorization: `Bearer ${env.CLOUDFLARE_EMAIL_API_TOKEN}`}
      : {'X-Smtp2go-Api-Key': env.SMTP2GO_API_KEY})},
    body: JSON.stringify(cloudflare
      ? {from: config.sender, to: config.recipient, subject: config.subject, text}
      : {sender: config.sender, to: [config.recipient], subject: config.subject, text_body: text}),
  });
  if (!sent.ok) return null;
  const response = await sent.json();
  if (!cloudflare) {
    const data = response.data;
    return data?.succeeded === 1 && data.failed === 0 && typeof data.email_id === 'string' && data.email_id
      ? data.email_id : null;
  }
  const result = response.result;
  if (response.success !== true || !Array.isArray(response.errors) || response.errors.length || !result ||
      typeof result.message_id !== 'string' ||
      !/^[\x21-\x7e]{1,512}$/.test(result.message_id || '') ||
      !Array.isArray(result.delivered) || !Array.isArray(result.queued) ||
      !Array.isArray(result.permanent_bounces) || result.permanent_bounces.length ||
      !Array.isArray(result.suppressed_recipients) || result.suppressed_recipients.length) return null;
  const accepted = [...result.delivered, ...result.queued];
  // A queued receipt is provider acceptance, not proof of inbox delivery. No automatic retry follows.
  return accepted.length === 1 && typeof accepted[0] === 'string' &&
    accepted[0].toLowerCase() === config.recipient.toLowerCase() ? result.message_id : null;
}

function constantEqual(a, b) {
  if (typeof a !== 'string' || typeof b !== 'string' || a.length !== b.length) return false;
  let difference = 0;
  for (let i = 0; i < a.length; i++) difference |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return difference === 0;
}

async function readBounded(request) {
  const reader = request.body?.getReader();
  if (!reader) throw new Error('missing body');
  let total = 0;
  const chunks = [];
  try {
    for (;;) {
      const {done, value} = await reader.read();
      if (done) break;
      total += value.byteLength;
      // Allow the full 100-line checkout envelope, including action identifiers.
      if (total > 32768) throw new Error('oversized body');
      chunks.push(value);
    }
  } finally {
    reader.releaseLock();
  }
  const data = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) { data.set(chunk, offset); offset += chunk.byteLength; }
  return JSON.parse(new TextDecoder('utf-8', {fatal: true}).decode(data));
}

function allowedLink(value) {
  try {
    const url = new URL(value);
    return url.protocol === 'https:' && url.hostname === 'www.dropbox.com' &&
      !url.username && !url.password && !url.port && !url.search && !url.hash && /^\/t\/[A-Za-z0-9]+$/.test(url.pathname);
  } catch { return false; }
}

function approvedAccessLinks(body, config, env) {
  if (!Object.hasOwn(body, 'access_links')) {
    return Array.isArray(config.links) && config.links.length > 0 && config.links.length <= 20 &&
      config.links.every(allowedLink) ? config.links : null;
  }
  try {
    const origin = new URL(env.FULFILMENT_PUBLIC_ORIGIN);
    if (origin.protocol !== 'https:' || origin.username || origin.password || origin.pathname !== '/' ||
        origin.search || origin.hash || origin.origin !== env.FULFILMENT_PUBLIC_ORIGIN ||
        (config.fulfilment_origin !== undefined && config.fulfilment_origin !== origin.origin) ||
        !Array.isArray(body.access_links) || body.access_links.length < 1 || body.access_links.length > 40) return null;
    const lines = [];
    for (const entry of body.access_links) {
      if (!Array.isArray(entry) || entry.length !== 2 || typeof entry[0] !== 'string' ||
          !entry[0] || entry[0].length > 180 || /[\x00-\x1f\x7f/\\]/.test(entry[0]) ||
          typeof entry[1] !== 'string') return null;
      const url = new URL(entry[1]);
      if (url.origin !== origin.origin || url.username || url.password || url.pathname !== '/fulfilment/access' ||
          url.hash || !/^\?token=[A-Za-z0-9_-]{43}$/.test(url.search) ||
          entry[1] !== origin.origin + url.pathname + url.search) return null;
      lines.push(`${entry[0]}: ${entry[1]}`);
    }
    return lines;
  } catch { return null; }
}

/** The service token and config are operator provisioned, never browser inputs. */
export async function handle(request, env, fetcher = fetch) {
  if (request.method !== 'POST' || env.DELIVERY_MODE !== 'test' || env.ENVIRONMENT !== 'test' ||
      typeof env.HANDLER_TOKEN !== 'string' || env.HANDLER_TOKEN.length < 32 ||
      !constantEqual(request.headers.get('authorization'), `Bearer ${env.HANDLER_TOKEN}`)) return fail(403);
  try {
    const provider = sendingProvider(env);
    const body = await readBounded(request);
    const raw = env.DELIVERY_CONFIG;
    if (typeof raw !== 'string' || raw.length > 16384) return fail(503);
    const registry = JSON.parse(raw);
    if (!registry || !Array.isArray(registry.actions) || !registry.actions.length || registry.actions.length > 100)
      return fail(503);
    const candidates = registry.actions.filter(action => action && action.workflow_key === body.workflow_key);
    const matches = [];
    for (const candidate of candidates) {
      const serialized = JSON.stringify(candidate);
      const hash = Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(serialized))),
        x => x.toString(16).padStart(2, '0')).join('');
      if (constantEqual(body.config_digest, hash)) matches.push({config:candidate, hash});
    }
    // Old revisions stay addressable by their immutable digest. Duplicate
    // matches are ambiguous and must not send.
    if (matches.length !== 1) return fail(400);
    const {config, hash} = matches[0];
    const links = approvedAccessLinks(body, config, env);
    if (!config || config.environment !== 'test' || !identifier.test(config.merchant) ||
        !identifier.test(config.workflow_key) || typeof config.config_ref !== 'string' ||
        body.merchant !== config.merchant || body.environment !== 'test' ||
        body.workflow_key !== config.workflow_key || body.config_ref !== config.config_ref ||
        !constantEqual(body.config_digest, hash) || typeof body.order_id !== 'string' || !identifier.test(body.order_id) ||
        typeof body.session_id !== 'string' || !sessionId.test(body.session_id) ||
        !Array.isArray(body.items) || !body.items.length || body.items.length > 100 ||
        !Array.isArray(config.offer_keys) || !config.offer_keys.length ||
        !body.items.some(item => Array.isArray(item) && config.offer_keys.includes(item[0]) &&
          Number.isSafeInteger(item[1]) && item[1] > 0) ||
        !links || typeof config.recipient !== 'string' ||
        typeof config.sender !== 'string' || typeof config.subject !== 'string' || config.subject.length > 180 ||
        !/^[^\s@<>]+@[^\s@<>]+\.[A-Za-z]{2,}$/.test(config.recipient) ||
        !/^[^\s@<>]+@[^\s@<>]+\.[A-Za-z]{2,}$/.test(config.sender) || /[\r\n]/.test(config.subject) ||
        config.recipient.toLowerCase() !== env.APPROVED_TEST_RECIPIENT?.toLowerCase() ||
        config.sender.toLowerCase() !== env.APPROVED_TEST_SENDER?.toLowerCase() ||
        !/^acct_[A-Za-z0-9]+$/.test(config.stripe_account || '') || config.stripe_account !== env.STRIPE_ACCOUNT_ID ||
        (!env.STRIPE_API_KEY?.startsWith('sk_test_') && !env.STRIPE_API_KEY?.startsWith('rk_test_')) ||
        !provider) return fail(400);

    // One shared budget bounds the complete provider sequence, not each request separately.
    const deadline = AbortSignal.timeout(7000);
    const account = await fetcher('https://api.stripe.com/v1/account', {
      headers: {Authorization: `Bearer ${env.STRIPE_API_KEY}`}, redirect: 'manual', signal: deadline,
    });
    if (!account.ok || (await account.json()).id !== config.stripe_account) return fail(403);
    const stripe = await fetcher(`https://api.stripe.com/v1/checkout/sessions/${body.session_id}`, {
      headers: {Authorization: `Bearer ${env.STRIPE_API_KEY}`}, redirect: 'manual', signal: deadline,
    });
    if (!stripe.ok) return fail(502);
    const session = await stripe.json();
    if (session.id !== body.session_id || session.livemode !== false || session.mode !== 'payment' ||
        session.status !== 'complete' || session.payment_status !== 'paid' || session.metadata?.cr_order_id !== body.order_id ||
        typeof session.customer_details?.email !== 'string' ||
        session.customer_details.email.toLowerCase() !== config.recipient.toLowerCase()) return fail(403);

    // The caller has already durably claimed (merchant, order, workflow).
    // Failure or timeout is uncertain and must never trigger an automatic resend.
    const reference = await sendEmail(env, {...config, links}, provider, deadline, fetcher);
    if (!reference) return fail(502);
    return Response.json({provider_reference: reference}, {headers: {'Cache-Control': 'no-store'}});
  } catch { return fail(503); }
}

export default {fetch(request, env) { return handle(request, env); }};
