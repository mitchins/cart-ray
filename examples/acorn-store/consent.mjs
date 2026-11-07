/** Optional purpose-specific consent UI; the server authenticates purchase context. */
export async function setupConsent({root, fetchImpl = fetch}) {
  if (!root) return;
  const endpoint = "/review-consent/v1/";
  const post = (name, data) => fetchImpl(endpoint + name, {
    method: "POST", headers: {"Content-Type": "application/json"},
    credentials: "same-origin", cache: "no-store", body: JSON.stringify(data),
  });
  try {
    const context = await post("context", {});
    if (!context.ok || (await context.json()).available !== true) return;
  } catch { return; }
  root.hidden = false;
  const form = root.querySelector("form");
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
  void setupConsent({root: document.querySelector("#review-consent")});
}
