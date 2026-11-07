/** Optional purpose-specific consent UI; the server authenticates purchase context. */
export function isPostPurchase({pathname, storage, now = Date.now()}) {
  if (pathname.replace(/\/+$/, "") === "/purchase-complete") return true;
  if (pathname !== "/") return false;
  try {
    const raw = storage.getItem("acorn-review-return");
    if (!raw || !/^\d+$/.test(raw)) return false;
    const age = now - Number(raw);
    return Number.isSafeInteger(age) && age >= 0 && age <= 7200000;
  } catch { return false; }
}
export async function setupConsent({root, fetchImpl = fetch, postPurchase = false, reload = () => location.reload()}) {
  if (!root) return;
  const endpoint = "/review-consent/v1/";
  const post = (name, data) => fetchImpl(endpoint + name, {
    method: "POST", headers: {"Content-Type": "application/json"},
    credentials: "same-origin", cache: "no-store", body: JSON.stringify(data),
  });
  let verified;
  const unavailable = () => {
    if (!postPurchase) return;
    root.hidden = false;
    root.querySelector('[role="status"]').textContent = "We couldn’t load the optional review request preference. Please try again later.";
    const retry = root.querySelector("#review-retry");
    retry.hidden = false;
    retry.addEventListener("click", reload);
  };
  try {
    const context = await post("context", {});
    if (!context.ok) { unavailable(); return; }
    const data = await context.json();
    if (data.available !== true) { unavailable(); return; }
    verified = data.verified === true;
  } catch { unavailable(); return; }
  root.hidden = false;
  const form = root.querySelector("#review-grant");
  const verify = root.querySelector("#review-verify");
  const codeForm = root.querySelector("#review-code-form");
  const send = root.querySelector("#review-send-code");
  const code = codeForm.querySelector("input[name=code]");
  verify.hidden = verified;
  form.hidden = !verified;
  send.addEventListener("click", async () => {
    send.disabled = true;
    try { const r = await post("challenge", {}); if (!r.ok || (await r.json()).sent !== true) throw new Error(); codeForm.hidden = false; }
    catch { status.textContent = "We couldn’t send a code. Please try later."; }
  });
  codeForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!/^[0-9]{6}$/.test(code.value)) { status.textContent = "Enter the six-digit code."; return; }
    try { const r = await post("verify", {code:code.value}); if (!r.ok || (await r.json()).verified !== true) throw new Error(); verify.hidden=true; form.hidden=false; status.textContent="Email verified."; code.value=""; }
    catch { status.textContent="We couldn’t verify that code. Please try again."; }
  });
  const accept = form.querySelector('input[name="accept"]');
  const button = form.querySelector("button");
  const status = root.querySelector('[role="status"]');
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!accept.checked) { status.textContent = "Please select the box if you would like a review request."; return; }
    button.disabled = true;
    status.textContent = "Saving your preference…";
    try {
      const result = await post("grant", {accept: true});
      if (!result.ok || (await result.json()).recorded !== true) throw new Error("unavailable");
      status.textContent = "Your preference has been saved.";
      form.hidden = true;
    } catch {
      status.textContent = "We couldn’t save your preference. Please try again later.";
      button.disabled = false;
    }
  });
}

if (typeof document !== "undefined" && window.CARTRAY_STOREFRONT?.reviewConsentEnabled) {
  void setupConsent({root: document.querySelector("#review-consent"), postPurchase: isPostPurchase({pathname: location.pathname, storage: window.sessionStorage})});
}
