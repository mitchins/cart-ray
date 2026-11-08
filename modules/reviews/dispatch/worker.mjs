/** Test-only queue consumer. Only service bindings can call the Reviews API. */

async function post(service, path, value, token) {
  const response = await service.fetch(`https://reviews.internal${path}`, {
    method: "POST",
    headers: {"content-type": "application/json", authorization: `Bearer ${token}`},
    body: JSON.stringify(value),
  });
  if (!response.ok) throw new Error(`internal request failed: ${response.status}`);
  return response.json();
}

async function digest(text) {
  const bytes = new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text)));
  return Array.from(bytes, x => x.toString(16).padStart(2, "0")).join("");
}

/** A delivery claim is single-use. Once a send might have happened, never resend. */
export async function processCommand(command, env, now = () => Math.floor(Date.now() / 1000), pause = ms => new Promise(resolve => setTimeout(resolve, ms))) {
  if (env.REVIEWS_ENVIRONMENT !== "test" || !env.REVIEWS_ISSUER || !env.REVIEWS_DELIVERY_TOKEN || !env.REVIEWS_API ||
      (env.SYNTHETIC_MAIL_PROVIDER === "smtp2go" ? !env.SMTP2GO_MAIL?.fetch : !env.MAIL?.send)) {
    throw new Error("test delivery not configured");
  }
  if (env.SYNTHETIC_MAIL_PROVIDER && !["smtp2go", "generic"].includes(env.SYNTHETIC_MAIL_PROVIDER)) {
    throw new Error("provider configuration unavailable");
  }
  if (env.SYNTHETIC_MAIL_PROVIDER === "smtp2go" &&
      (typeof env.SMTP2GO_MAIL_TOKEN !== "string" || env.SMTP2GO_MAIL_TOKEN.length < 32 ||
       [env.REVIEWS_ISSUER, env.REVIEWS_DELIVERY_TOKEN].includes(env.SMTP2GO_MAIL_TOKEN) ||
       !env.PURCHASE_EVIDENCE?.fetch || typeof env.AUTOMATION_RECIPIENT_TOKEN !== "string" ||
       env.AUTOMATION_RECIPIENT_TOKEN.length < 32 ||
       [env.REVIEWS_ISSUER, env.REVIEWS_DELIVERY_TOKEN, env.SMTP2GO_MAIL_TOKEN].includes(env.AUTOMATION_RECIPIENT_TOKEN))) {
    throw new Error("provider configuration unavailable");
  }
  const sender = env.SYNTHETIC_SENDER;
  const storeName = env.REVIEWS_PUBLIC_STORE_NAME;
  if (typeof sender !== "string" || sender.length > 254 || sender.split("@").length !== 2 ||
      sender.split("@").some(part => !part) || !sender.split("@")[1].includes(".") ||
      /[^\x21-\x7e]|[<>,;:"\\()[\]]/.test(sender) ||
      typeof storeName !== "string" || !storeName.trim() || storeName.length > 200 ||
      (env.SYNTHETIC_MAIL_PROVIDER === "smtp2go" && `Share your experience with ${storeName}`.length > 200) ||
      /[\x00-\x1f\x7f-\x9f]/.test(storeName)) {
    throw new Error("test delivery not configured");
  }
  let landing;
  try { landing = new URL(env.REVIEWS_LANDING_URL); }
  catch (_) { throw new Error("invalid landing URL"); }
  if (landing.protocol !== "https:" || landing.search || landing.hash || landing.username || landing.password) throw new Error("invalid landing URL");
  const isHex = value => typeof value === "string" && /^[0-9a-f]{64}$/.test(value);
  if (!command || !isHex(command.token_hash) || !isHex(command.request_id) || !Number.isSafeInteger(command.issued_at)) return "discard";
  const age = now() - command.issued_at;
  if (age < 0) throw new Error("command clock ahead");
  const api = env.REVIEWS_API;
  const prefix = "/reviews-internal/v1/invitations/";
  const {pending} = await post(api, prefix + "recovery-command", {
    token_hash: command.token_hash, request_id: command.request_id, issued_at: command.issued_at,
  }, env.REVIEWS_ISSUER);
  if (!pending) return "done";
  const recipient = env.SYNTHETIC_RECIPIENT;
  if (env.SYNTHETIC_MAIL_PROVIDER === "smtp2go") {
    if (!Number.isSafeInteger(pending.outbox_id) || pending.outbox_id < 1) throw new Error("trusted test recipient unavailable");
    const resolved = await post(env.PURCHASE_EVIDENCE, "/automation-recipient", {outbox_id:pending.outbox_id}, env.AUTOMATION_RECIPIENT_TOKEN);
    if (typeof recipient !== "string" || resolved.email !== recipient || resolved.recipient_context_hash !== pending.recipient_context_hash) {
      throw new Error("trusted test recipient unavailable");
    }
  } else if (typeof recipient !== "string" || await digest(recipient) !== pending.recipient_context_hash) {
    throw new Error("trusted test recipient unavailable");
  }
  const draft = await post(api, prefix + "recovery-prepare", {
    event_id: pending.source_event_id, request_id: pending.request_id,
  }, env.REVIEWS_ISSUER);
  // A dispatching or uncertain generation must be reconciled, never sent again.
  if (draft.state !== "prepared" || !isHex(draft.token)) throw new Error("delivery reconciliation required");
  const claim = {acquisition_id: pending.acquisition_id, generation_id: draft.generation_id, delivery_identity: draft.delivery_identity};
  const claimed = await post(api, prefix + "delivery-claim", claim, env.REVIEWS_DELIVERY_TOKEN);
  if (claimed.state !== "dispatching") throw new Error("delivery not claimed");
  landing.hash = `invitation=${draft.token}`;
  let providerEmailId;
  try {
    const message = {
      from: env.SYNTHETIC_SENDER, to: recipient,
      subject: `Share your experience with ${env.REVIEWS_PUBLIC_STORE_NAME}`,
      text: `Thank you for your purchase.\n\nIf you would like to share your experience, visit:\n\n${landing}\n\nYour review will be checked before it appears publicly. Anyone with this link can use it.`,
      headers: {"X-Reviews-Delivery-Identity": claim.delivery_identity},
    };
    let receipt;
    if (env.SYNTHETIC_MAIL_PROVIDER === "smtp2go") {
      const controller = new AbortController(); let timer;
      try {
        const deadline = new Promise((_, reject) => { timer = setTimeout(() => { controller.abort(); reject(new Error("timeout")); }, 15000); });
        receipt = await Promise.race([(async () => {
          const response = await env.SMTP2GO_MAIL.fetch("https://recovery-mail.internal/send", {
            method:"POST", signal:controller.signal, headers:{"content-type":"application/json",authorization:`Bearer ${env.SMTP2GO_MAIL_TOKEN}`}, body:JSON.stringify(message),
          });
          if (!response.ok || !response.body) throw new Error("provider unavailable");
          const reader=response.body.getReader(); let size=0, text=""; const decoder=new TextDecoder();
          try { for (;;) {const part=await reader.read(); if(part.done) break; size+=part.value.byteLength;
            if(size>4096) throw new Error("provider response too large"); text+=decoder.decode(part.value,{stream:true});}
            return JSON.parse(text+decoder.decode());
          } finally {void reader.cancel().catch(()=>{});}
        })(),deadline]);
      } finally {clearTimeout(timer);controller.abort();}
    } else {
      receipt = await env.MAIL.send(message);
    }
    // A generic MAIL binding need not return a provider receipt. A specifically
    // configured SMTP2GO adapter must return its authoritative email ID.
    if (env.SYNTHETIC_MAIL_PROVIDER === "smtp2go") {
      if (!receipt || typeof receipt.email_id !== "string" || !/^[A-Za-z0-9_-]{1,128}$/.test(receipt.email_id)) {
        throw new Error("provider receipt unavailable");
      }
      providerEmailId = receipt.email_id;
    }
  } catch (error) {
    // Even errors from a provider may follow acceptance. Never infer failure.
    try { await post(api, prefix + "delivery-outcome", {...claim, outcome: "uncertain"}, env.REVIEWS_DELIVERY_TOKEN); } catch (_) { /* reconcile the claimed delivery */ }
    throw new Error("delivery reconciliation required", {cause: error});
  }
  const acceptedAt = now();
  const outcome = {...claim, outcome: "accepted", accepted_at: acceptedAt,
    ...(providerEmailId ? {provider_email_id: providerEmailId} : {})};
  for (let attempt = 0; attempt < 3; attempt++) {
    try {
      await post(api, prefix + "delivery-outcome", outcome, env.REVIEWS_DELIVERY_TOKEN);
      return "done";
    } catch (_) {
      // The acknowledgement is idempotent; give transient failures and clock
      // skew time to resolve without repeating the email send.
      if (attempt < 2) await pause(1000);
    }
  }
  throw new Error("delivery reconciliation required");
}

export default {
  async queue(batch, env) {
    for (const message of batch.messages) {
      try { await processCommand(message.body, env); message.ack(); }
      catch (_) { message.retry({delaySeconds: 60}); }
    }
  },
};
