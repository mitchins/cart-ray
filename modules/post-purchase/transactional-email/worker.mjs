/** Test-only registered handler for a confirmed CartRay purchase action. */
const fail = status => Response.json({error: 'delivery unavailable'}, {status, headers: {'Cache-Control': 'no-store'}});
const identifier = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/;

function testRecipientPolicy(env) {
  const config = JSON.parse(env.DELIVERY_CONFIG ?? '{}');
  if (!config || typeof config !== 'object' || Array.isArray(config) ||
      Object.keys(config).some(key => key !== 'test_recipient_allowlist')) throw new Error('invalid test policy');
  const policy = config.test_recipient_allowlist;
  if (policy === undefined) return recipient => recipient.toLowerCase() === env.APPROVED_TEST_RECIPIENT.toLowerCase();
  if (!policy || Object.keys(policy).sort().join(',') !== 'addresses,domains') throw new Error('invalid test policy');
  const domain = /^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$/;
  const lists = {};
  for (const key of ['addresses','domains']) {
    if (!Array.isArray(policy[key]) || policy[key].length > 32) throw new Error('invalid test policy');
    lists[key] = policy[key].map(value => {
      if (typeof value !== 'string' || !/^[\x21-\x7e]{1,254}$/.test(value)) throw new Error('invalid test policy');
      value = value.toLowerCase();
      if (key === 'domains') {
        if (value.length > 253 || !domain.test(value)) throw new Error('invalid test domain');
      } else {
        const parts = value.split('@');
        if (parts.length !== 2 || !/^[a-z0-9.!#$%&'*+/=?^_`{|}~-]{1,64}$/.test(parts[0]) ||
            parts[0].startsWith('.') || parts[0].endsWith('.') || parts[0].includes('..') || !domain.test(parts[1]))
          throw new Error('invalid test address');
      }
      return value;
    });
    if (new Set(lists[key]).size !== lists[key].length) throw new Error('duplicate test destination');
  }
  if (!lists.addresses.length && !lists.domains.length) throw new Error('empty test policy');
  return recipient => {
    const normalized = recipient.toLowerCase();
    return lists.addresses.includes(normalized) || lists.domains.includes(normalized.split('@')[1]);
  };
}

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
      ? {from: config.sender, to: config.recipient, subject: config.subject, text, ...(config.headers ? {headers:Object.fromEntries(config.headers.map(x=>[x.name,x.value]))} : {})}
      : {sender: config.sender, to: [config.recipient], subject: config.subject, text_body: text, ...(config.headers ? {custom_headers:config.headers.map(x=>({header:x.name,value:x.value}))} : {})}),
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
    // Stripe charge units differ from locale display defaults for some currencies.
    const zero = new Set(['bif','clp','djf','gnf','jpy','kmf','krw','mga','pyg','rwf','vnd','vuv','xaf','xof','xpf']);
    const digits = zero.has(session.currency) ? 0 : 2;
    const displayDigits = ['isk', 'ugx'].includes(session.currency) ? 0 : digits;
    const format = new Intl.NumberFormat('en', {style:'currency', currency:session.currency.toUpperCase(),
      minimumFractionDigits:displayDigits, maximumFractionDigits:displayDigits});
    const units = 10 ** digits;
    return `\nTotal: ${format.format(session.amount_total / units)}`;
  } catch { return ''; }
}

/** The source Worker has already claimed one order receipt; this handler never retries. */
export async function handle(request, env, fetcher = fetch) {
  if (request.method !== 'POST' || !['test','live'].includes(env.ENVIRONMENT) ||
      ![env.ENVIRONMENT, 'disabled'].includes(env.DELIVERY_MODE) ||
      typeof env.HANDLER_TOKEN !== 'string' || env.HANDLER_TOKEN.length < 32 ||
      !constantEqual(request.headers.get('authorization'), `Bearer ${env.HANDLER_TOKEN}`)) return fail(403);
  try {
    const provider = sendingProvider(env);
    const body = await readBounded(request);
    if (env.DELIVERY_MODE === 'disabled' && body?.type !== 'recipient_evidence') return fail(403);
    const sender = env.ENVIRONMENT === 'test' ? env.APPROVED_TEST_SENDER : env.MERCHANT_SENDER;
    const email = /^[^\s@<>]+@[^\s@<>]+\.[A-Za-z]{2,}$/;
    const mode = env.ENVIRONMENT;
    const permittedRecipient = mode === 'test' ? testRecipientPolicy(env) : null;
    if (!body || !['order_confirmation','review_followup','recipient_evidence'].includes(body.type) || body.environment !== mode ||
        !identifier.test(body.merchant || '') || body.merchant !== env.MERCHANT_ID ||
        !identifier.test(body.order_id || '') ||
        !(mode === 'test' ? /^cs_test_[A-Za-z0-9_]{1,200}$/ : /^cs_live_[A-Za-z0-9_]{1,200}$/).test(body.session_id || '') ||
        typeof env.MERCHANT_NAME !== 'string' || !env.MERCHANT_NAME.trim() || [...env.MERCHANT_NAME].length > 100 ||
        /[\x00-\x1f\x7f]/.test(env.MERCHANT_NAME) || !Array.isArray(body.items) ||
        !body.items.length || body.items.length > 100 || body.items.some(item =>
          !item || !identifier.test(item.product_key || '') || !Number.isSafeInteger(item.quantity) ||
          item.quantity < 1 || typeof item.title !== 'string' || !item.title.trim() ||
          item.title.length > 200 || /[\x00-\x1f\x7f]/.test(item.title)) ||
        !/^acct_[A-Za-z0-9]+$/.test(env.STRIPE_ACCOUNT_ID || '') ||
        (!env.STRIPE_API_KEY?.startsWith(`sk_${mode}_`) && !env.STRIPE_API_KEY?.startsWith(`rk_${mode}_`)) ||
        !email.test(sender || '') || (mode === 'test' && !email.test(env.APPROVED_TEST_RECIPIENT || '')) ||
        !orderLink(body.access_url, env.STOREFRONT_ORIGIN) || !provider) return fail(400);

    const deadline = AbortSignal.timeout(7000);
    const account = await fetcher('https://api.stripe.com/v1/account', {
      headers: {Authorization: `Bearer ${env.STRIPE_API_KEY}`}, redirect: 'manual', signal: deadline,
    });
    if (!account.ok) return fail(502);
    if ((await account.json()).id !== env.STRIPE_ACCOUNT_ID) return fail(403);
    const stripe = await fetcher(`https://api.stripe.com/v1/checkout/sessions/${body.session_id}`, {
      headers: {Authorization: `Bearer ${env.STRIPE_API_KEY}`}, redirect: 'manual', signal: deadline,
    });
    if (!stripe.ok) return fail(502);
    const session = await stripe.json();
    if (session.id !== body.session_id || session.livemode !== (mode === 'live') || session.mode !== 'payment' ||
        session.status !== 'complete' || !Number.isSafeInteger(session.amount_total) || session.amount_total < 0 ||
        !(session.payment_status === 'paid' || (session.amount_total === 0 && session.payment_status === 'no_payment_required')) ||
        session.metadata?.cr_order_id !== body.order_id ||
        typeof session.customer_details?.email !== 'string' || !email.test(session.customer_details.email) ||
        (mode === 'test' &&
         !permittedRecipient(session.customer_details.email))) return fail(422);
    const recipient = mode === 'test' ? session.customer_details.email.toLowerCase() : session.customer_details.email;

    if (body.type === 'recipient_evidence') return Response.json({email:recipient}, {headers:{'Cache-Control':'no-store'}});

    const lines = body.items.map(item => `${item.title} × ${item.quantity}`);
    const total = formatTotal(session);
    const access = body.access_url ? `View your purchased resources: ${body.access_url}`
      : 'Your purchased resources will be available when ready. Please contact the merchant if needed.';
    let text = `Thank you for your order from ${env.MERCHANT_NAME}.\n\nOrder: ${body.order_id}\n\n` +
      lines.join('\n') + total + `\n\n${access}`;
    let reviews = body.reviews;
    const identity = body.delivery_identity ?? [...new Uint8Array(await crypto.subtle.digest('SHA-256',
      new TextEncoder().encode(JSON.stringify([body.merchant,mode,body.order_id,body.type]))))]
      .map(x=>x.toString(16).padStart(2,'0')).join('');
    if (!/^[a-f0-9]{64}$/.test(identity)) return fail(400);
    const identityHeaders = [{name:'X-CartRay-Message-Identity',value:identity},
      ...(body.delivery_identity ? [{name:'X-Reviews-Delivery-Identity',value:identity}] : [])];
    let unsubscribeHeaders;
    try {
    if (reviews !== undefined && reviews !== null) {
      const landing = new URL(reviews.url), stop = new URL(reviews.unsubscribe_url);
      if (landing.origin !== env.STOREFRONT_ORIGIN || stop.origin !== env.STOREFRONT_ORIGIN ||
          landing.protocol !== 'https:' || stop.protocol !== 'https:' || landing.username || stop.username ||
          landing.password || stop.password || landing.search || stop.hash ||
          !/^\/(?:[A-Za-z0-9_-]+\/)*review-unsubscribe$/.test(stop.pathname) ||
          !/^\?token=[a-f0-9]{64}$/.test(stop.search) ||
          !/^#invitation=[a-f0-9]{64}(?:&featured=[A-Za-z0-9_,%.-]+)?$/.test(landing.hash) ||
          !Array.isArray(reviews.subjects) || !reviews.subjects.length || reviews.subjects.length > 100 ||
          reviews.subjects.some(x => !identifier.test(x)) ||
          typeof env.MERCHANT_CONTACT !== 'string' || !env.MERCHANT_CONTACT.trim() ||
          env.MERCHANT_CONTACT.length > 200 || /[\x00-\x1f\x7f]/.test(env.MERCHANT_CONTACT)) throw new Error('review content unavailable');
      unsubscribeHeaders = [{name:'List-Unsubscribe',value:`<${stop}>`},
        {name:'List-Unsubscribe-Post',value:'List-Unsubscribe=One-Click'}];
      const titles = reviews.subjects.map(subject => reviews.names?.[subject] || subject);
      const invitation = `Share your experience with ${env.MERCHANT_NAME}: ${landing}\n` +
        `Featured products: ${titles.join(', ')}\n` +
        `Stop review requests: ${stop}\nContact: ${env.MERCHANT_CONTACT}`;
      text = body.type === 'review_followup' ? invitation : text + '\n\n' + invitation;
    } else if (body.type === 'review_followup') throw new Error('review content unavailable');
    } catch {
      if (body.type === 'review_followup') return fail(400);
      unsubscribeHeaders = undefined; // Optional Reviews cannot block the transactional receipt.
    }
    const reference = await sendEmail(env, {sender, recipient, headers:[...identityHeaders,...(unsubscribeHeaders || [])],
      subject:body.type === 'review_followup' ? `Review your purchase from ${env.MERCHANT_NAME}` : `Your order from ${env.MERCHANT_NAME}`}, provider, deadline, fetcher, text);
    if (!reference) return fail(502);
    return Response.json({provider_reference: reference}, {headers: {'Cache-Control': 'no-store'}});
  } catch { return fail(503); }
}

export default {fetch(request, env) { return handle(request, env); }};
