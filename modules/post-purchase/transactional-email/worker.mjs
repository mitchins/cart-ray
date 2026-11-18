/** Test-only registered handler for a confirmed CartRay purchase action. */
const fail = status => Response.json({error: 'delivery unavailable'}, {status, headers: {'Cache-Control': 'no-store'}});
const identifier = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/;
const sessionId = /^cs_test_[A-Za-z0-9_]{1,200}$/;

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

/** The service token and config are operator provisioned, never browser inputs. */
export async function handle(request, env, fetcher = fetch) {
  if (request.method !== 'POST' || env.DELIVERY_MODE !== 'test' || env.ENVIRONMENT !== 'test' ||
      typeof env.HANDLER_TOKEN !== 'string' || env.HANDLER_TOKEN.length < 32 ||
      !constantEqual(request.headers.get('authorization'), `Bearer ${env.HANDLER_TOKEN}`)) return fail(403);
  try {
    const body = await readBounded(request);
    const raw = env.DELIVERY_CONFIG;
    if (typeof raw !== 'string' || raw.length > 16384) return fail(503);
    const registry = JSON.parse(raw);
    if (!registry || !Array.isArray(registry.actions) || !registry.actions.length || registry.actions.length > 100)
      return fail(503);
    const matches = registry.actions.filter(action => action && action.workflow_key === body.workflow_key);
    if (matches.length !== 1) return fail(400);
    const config = matches[0];
    // Configuration digests bind the exact JSON serialization of each action,
    // allowing separate matching workflows to share one private handler.
    const serialized = JSON.stringify(config);
    const hash = Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(serialized))),
      x => x.toString(16).padStart(2, '0')).join('');
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
        !Array.isArray(config.links) || !config.links.length || config.links.length > 20 ||
        !config.links.every(allowedLink) || typeof config.recipient !== 'string' ||
        typeof config.sender !== 'string' || typeof config.subject !== 'string' || config.subject.length > 180 ||
        !/^[^\s@<>]+@[^\s@<>]+\.[A-Za-z]{2,}$/.test(config.recipient) ||
        !/^[^\s@<>]+@[^\s@<>]+\.[A-Za-z]{2,}$/.test(config.sender) || /[\r\n]/.test(config.subject) ||
        config.recipient.toLowerCase() !== env.APPROVED_TEST_RECIPIENT?.toLowerCase() ||
        config.sender.toLowerCase() !== env.APPROVED_TEST_SENDER?.toLowerCase() ||
        !/^acct_[A-Za-z0-9]+$/.test(config.stripe_account || '') || config.stripe_account !== env.STRIPE_ACCOUNT_ID ||
        (!env.STRIPE_API_KEY?.startsWith('sk_test_') && !env.STRIPE_API_KEY?.startsWith('rk_test_')) ||
        !env.SMTP2GO_API_KEY?.startsWith('api-')) return fail(400);

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
    const sent = await fetcher('https://api.smtp2go.com/v3/email/send', {
      method: 'POST', redirect: 'manual', signal: deadline,
      headers: {'Content-Type': 'application/json', 'X-Smtp2go-Api-Key': env.SMTP2GO_API_KEY},
      body: JSON.stringify({sender: config.sender, to: [config.recipient], subject: config.subject,
        text_body: 'This test purchase contains dummy materials only.\n\n' + config.links.join('\n')}),
    });
    if (!sent.ok) return fail(502);
    const data = (await sent.json()).data;
    if (data?.succeeded !== 1 || data.failed !== 0 || typeof data.email_id !== 'string' || !data.email_id) return fail(502);
    return Response.json({provider_reference: data.email_id}, {headers: {'Cache-Control': 'no-store'}});
  } catch { return fail(503); }
}

export default {fetch(request, env) { return handle(request, env); }};
