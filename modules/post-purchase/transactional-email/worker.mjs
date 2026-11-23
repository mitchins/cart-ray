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

async function sendEmail(env, config, provider, deadline, fetcher, text) {
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
      if (total > 262144) throw new Error('oversized body');
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

function orderLink(value, origin) {
  if (value === null) return true;
  try {
    const url = new URL(value);
    return url.protocol === 'https:' && url.origin === origin && !url.username && !url.password && !url.search &&
      /^\/(?:[A-Za-z0-9_-]+\/)*order-access$/.test(url.pathname) && /^#token=[A-Za-z0-9_-]{43}$/.test(url.hash);
  } catch { return false; }
}

function formatTotal(session) {
  if (!Number.isSafeInteger(session.amount_total) || session.amount_total < 0 ||
      !/^[a-z]{3}$/.test(session.currency || '')) return '';
  try {
    const format = new Intl.NumberFormat('en', {style:'currency', currency:session.currency.toUpperCase()});
    // Stripe charges ISK and UGX in hundredths for backwards compatibility,
    // even though their normal display format has no decimal places.
    const units = ['isk', 'ugx'].includes(session.currency)
      ? 100 : 10 ** format.resolvedOptions().maximumFractionDigits;
    return `\nTotal: ${format.format(session.amount_total / units)}`;
  } catch { return ''; }
}

/** The source Worker has already claimed one order receipt; this handler never retries. */
export async function handle(request, env, fetcher = fetch) {
  if (request.method !== 'POST' || env.DELIVERY_MODE !== 'test' || env.ENVIRONMENT !== 'test' ||
      typeof env.HANDLER_TOKEN !== 'string' || env.HANDLER_TOKEN.length < 32 ||
      !constantEqual(request.headers.get('authorization'), `Bearer ${env.HANDLER_TOKEN}`)) return fail(403);
  try {
    const provider = sendingProvider(env);
    const body = await readBounded(request);
    if (!body || body.type !== 'order_confirmation' || body.environment !== 'test' ||
        !identifier.test(body.merchant || '') || body.merchant !== env.MERCHANT_ID ||
        !identifier.test(body.order_id || '') || !sessionId.test(body.session_id || '') ||
        typeof env.MERCHANT_NAME !== 'string' || !env.MERCHANT_NAME.trim() || [...env.MERCHANT_NAME].length > 100 ||
        /[\x00-\x1f\x7f]/.test(env.MERCHANT_NAME) || !Array.isArray(body.items) ||
        !body.items.length || body.items.length > 100 || body.items.some(item =>
          !item || !identifier.test(item.product_key || '') || !Number.isSafeInteger(item.quantity) ||
          item.quantity < 1 || typeof item.title !== 'string' || !item.title.trim() ||
          item.title.length > 200 || /[\x00-\x1f\x7f]/.test(item.title)) ||
        !/^acct_[A-Za-z0-9]+$/.test(env.STRIPE_ACCOUNT_ID || '') ||
        (!env.STRIPE_API_KEY?.startsWith('sk_test_') && !env.STRIPE_API_KEY?.startsWith('rk_test_')) ||
        !/^[^\s@<>]+@[^\s@<>]+\.[A-Za-z]{2,}$/.test(env.APPROVED_TEST_RECIPIENT || '') ||
        !/^[^\s@<>]+@[^\s@<>]+\.[A-Za-z]{2,}$/.test(env.APPROVED_TEST_SENDER || '') ||
        !orderLink(body.access_url, env.STOREFRONT_ORIGIN) || !provider) return fail(400);

    const deadline = AbortSignal.timeout(7000);
    const account = await fetcher('https://api.stripe.com/v1/account', {
      headers: {Authorization: `Bearer ${env.STRIPE_API_KEY}`}, redirect: 'manual', signal: deadline,
    });
    if (!account.ok || (await account.json()).id !== env.STRIPE_ACCOUNT_ID) return fail(403);
    const stripe = await fetcher(`https://api.stripe.com/v1/checkout/sessions/${body.session_id}`, {
      headers: {Authorization: `Bearer ${env.STRIPE_API_KEY}`}, redirect: 'manual', signal: deadline,
    });
    if (!stripe.ok) return fail(502);
    const session = await stripe.json();
    if (session.id !== body.session_id || session.livemode !== false || session.mode !== 'payment' ||
        session.status !== 'complete' || !Number.isSafeInteger(session.amount_total) || session.amount_total < 0 ||
        !(session.payment_status === 'paid' || (session.amount_total === 0 && session.payment_status === 'no_payment_required')) ||
        session.metadata?.cr_order_id !== body.order_id ||
        typeof session.customer_details?.email !== 'string' ||
        session.customer_details.email.toLowerCase() !== env.APPROVED_TEST_RECIPIENT.toLowerCase()) return fail(403);

    const lines = body.items.map(item => `${item.title} × ${item.quantity}`);
    const total = formatTotal(session);
    const access = body.access_url ? `View your purchased resources: ${body.access_url}`
      : 'Your purchased resources will be available when ready. Please contact the merchant if needed.';
    const text = `Thank you for your order from ${env.MERCHANT_NAME}.\n\nOrder: ${body.order_id}\n\n` +
      lines.join('\n') + total + `\n\n${access}`;
    const reference = await sendEmail(env, {sender:env.APPROVED_TEST_SENDER, recipient:env.APPROVED_TEST_RECIPIENT,
      subject:`Your order from ${env.MERCHANT_NAME}`}, provider, deadline, fetcher, text);
    if (!reference) return fail(502);
    return Response.json({provider_reference: reference}, {headers: {'Cache-Control': 'no-store'}});
  } catch { return fail(503); }
}

export default {fetch(request, env) { return handle(request, env); }};
