const CART_STORAGE_KEY = "cartray.cart.v1";
const PENDING_CHECKOUT_STORAGE_KEY = "cartray.pending-checkout.v1";
const CHECKOUT_SESSION_ID_RE = /^cs_[A-Za-z0-9_]+$/;
const RETURN_POLL_DELAYS_MS = [0, 1_000, 2_000, 4_000, 8_000, 15_000];

export function checkoutPayload(catalogueVersion, cart, checkoutRequestId) {
  return {
    checkout_request_id: checkoutRequestId,
    manifest_version: catalogueVersion,
    items: cartSnapshot(cart).map(([productKey, quantity]) => ({ product_key: productKey, quantity })),
  };
}

export function addToCart(cart, product) {
  const quantity = Math.min((cart.get(product.product_key) || 0) + 1, product.max_quantity);
  cart.set(product.product_key, quantity);
}

export function cartSnapshot(cart) {
  return [...cart.entries()].sort(([left], [right]) => left.localeCompare(right));
}

export function loadPersistedCart(storage, catalogue) {
  const fallback = { cart: new Map(), revision: 0 };
  if (!storage) return fallback;
  try {
    const saved = JSON.parse(storage.getItem(CART_STORAGE_KEY) || "null");
    if (
      !saved || typeof saved !== "object" || saved.catalogueVersion !== catalogue.version ||
      !Number.isSafeInteger(saved.revision) || saved.revision < 0 || !Array.isArray(saved.items)
    ) {
      storage.removeItem(CART_STORAGE_KEY);
      return fallback;
    }
    const products = new Map(catalogue.products.map((product) => [product.product_key, product]));
    const cart = new Map();
    for (const item of saved.items) {
      if (!Array.isArray(item) || item.length !== 2 || typeof item[0] !== "string" || !Number.isSafeInteger(item[1])) {
        storage.removeItem(CART_STORAGE_KEY);
        return fallback;
      }
      const product = products.get(item[0]);
      if (!product || item[1] < 1 || cart.has(item[0])) {
        storage.removeItem(CART_STORAGE_KEY);
        return fallback;
      }
      cart.set(item[0], Math.min(item[1], product.max_quantity));
    }
    return { cart, revision: saved.revision };
  } catch {
    try { storage.removeItem(CART_STORAGE_KEY); } catch { /* storage is optional */ }
    return fallback;
  }
}

export function persistCart(storage, catalogueVersion, cart, revision) {
  if (!storage) return;
  try {
    storage.setItem(CART_STORAGE_KEY, JSON.stringify({ catalogueVersion, items: cartSnapshot(cart), revision }));
  } catch { /* storage is optional */ }
}

export function storePendingCheckout(storage, checkout, catalogueVersion, cart, revision) {
  if (!storage || !validSessionId(checkout.sessionId)) return;
  try {
    storage.setItem(
      PENDING_CHECKOUT_STORAGE_KEY,
      JSON.stringify({ sessionId: checkout.sessionId, catalogueVersion, items: cartSnapshot(cart), revision }),
    );
  } catch { /* storage is optional */ }
}

export function loadPendingCheckout(storage) {
  if (!storage) return null;
  try {
    const pending = JSON.parse(storage.getItem(PENDING_CHECKOUT_STORAGE_KEY) || "null");
    if (
      !pending || typeof pending !== "object" || !validSessionId(pending.sessionId) ||
      typeof pending.catalogueVersion !== "string" || !Number.isSafeInteger(pending.revision) ||
      pending.revision < 0 || !validSnapshot(pending.items)
    ) return null;
    return pending;
  } catch {
    return null;
  }
}

export function matchingPendingCheckout(sessionId, pending, currentCart, currentRevision, catalogueVersion) {
  return (
    validSessionId(sessionId) && pending?.sessionId === sessionId && pending.catalogueVersion === catalogueVersion &&
    pending.revision === currentRevision && snapshotsEqual(pending.items, cartSnapshot(currentCart))
  );
}

export function consumePendingCheckout(storage, sessionId) {
  const pending = loadPendingCheckout(storage);
  if (pending?.sessionId === sessionId) {
    try { storage.removeItem(PENDING_CHECKOUT_STORAGE_KEY); } catch { /* storage is optional */ }
  }
}

export async function startCheckout(configuration, payload, fetchImpl = fetch) {
  if (!configuration.checkoutEnabled) return null;
  const response = await fetchImpl(`${configuration.apiBaseUrl}/checkout`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!response.ok) throw new Error("Checkout could not be started.");
  const result = await response.json();
  if (typeof result.checkout_url !== "string" || !validSessionId(result.session_id)) {
    throw new Error("Checkout response was incomplete.");
  }
  return { url: result.checkout_url, sessionId: result.session_id };
}

export async function createCheckoutAndStorePending({ configuration, catalogueVersion, cart, revision, requestId, storage, fetchImpl }) {
  const submittedCart = new Map(cart);
  const submittedRevision = revision;
  const checkout = await startCheckout(
    configuration,
    checkoutPayload(catalogueVersion, submittedCart, requestId),
    fetchImpl,
  );
  storePendingCheckout(storage, checkout, catalogueVersion, submittedCart, submittedRevision);
  return checkout;
}

export async function checkoutStatus(configuration, sessionId, fetchImpl = fetch, signal) {
  if (!validSessionId(sessionId)) return null;
  try {
    const response = await fetchImpl(
      `${configuration.apiBaseUrl}/checkout-status?session_id=${encodeURIComponent(sessionId)}`,
      signal ? { signal } : undefined,
    );
    if (!response.ok) return null;
    const result = await response.json();
    return result?.state === "pending" || result?.state === "confirmed" ? result.state : null;
  } catch {
    return null;
  }
}

export async function pollCheckoutStatus(
  configuration, sessionId, fetchImpl = fetch, sleep = delay, deadlineMs = 30_000, now = Date.now,
) {
  const deadline = now() + deadlineMs;
  let lastState = null;
  for (const delayMs of RETURN_POLL_DELAYS_MS) {
    const beforeSleep = deadline - now();
    if (beforeSleep <= 0) return lastState;
    if (delayMs) await sleep(Math.min(delayMs, beforeSleep));
    const remaining = deadline - now();
    if (remaining <= 0) return lastState;
    const state = await checkoutStatusBeforeDeadline(configuration, sessionId, fetchImpl, remaining);
    if (state === "confirmed") return state;
    if (state === "pending") lastState = state;
    else return lastState;
  }
  return lastState;
}

export async function processCheckoutReturn({ configuration, sessionId, storage, catalogue, cart, revision, poll }) {
  const state = await poll(configuration, sessionId);
  if (state !== "confirmed") return { state, cleared: false };
  const current = loadPersistedCart(storage, catalogue);
  const pending = loadPendingCheckout(storage);
  const cleared = matchingPendingCheckout(sessionId, pending, current.cart, current.revision, catalogue.version);
  if (cleared) {
    cart.clear();
    try { storage?.removeItem(CART_STORAGE_KEY); } catch { /* storage is optional */ }
  }
  consumePendingCheckout(storage, sessionId);
  return { state, cleared };
}

export function successReturnPath(pathname, state) {
  return state === "confirmed" ? `${pathname}?checkout=complete` : null;
}

export async function loadCatalogue(configuration, fetchImpl = fetch) {
  if (configuration.previewCatalogue) return configuration.previewCatalogue;
  if (!configuration.apiBaseUrl) throw new Error("Storefront configuration has no catalogue source.");
  const response = await fetchImpl(`${configuration.apiBaseUrl}/catalogue`);
  if (!response.ok) throw new Error("Catalogue could not be loaded.");
  return response.json();
}

function validSessionId(value) {
  return typeof value === "string" && CHECKOUT_SESSION_ID_RE.test(value);
}

function validSnapshot(value) {
  return Array.isArray(value) && value.every((item) => Array.isArray(item) && item.length === 2 && typeof item[0] === "string" && Number.isSafeInteger(item[1]) && item[1] > 0);
}

function snapshotsEqual(left, right) {
  return JSON.stringify(left) === JSON.stringify(right);
}

function delay(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

async function checkoutStatusBeforeDeadline(configuration, sessionId, fetchImpl, millisecondsRemaining) {
  if (millisecondsRemaining <= 0) return null;
  const controller = new AbortController();
  let timeoutId;
  const timeout = new Promise((resolve) => {
    timeoutId = setTimeout(() => {
      controller.abort();
      resolve(null);
    }, millisecondsRemaining);
  });
  try {
    return await Promise.race([checkoutStatus(configuration, sessionId, fetchImpl, controller.signal), timeout]);
  } finally {
    clearTimeout(timeoutId);
  }
}

function money(amountMinor, currency) {
  return new Intl.NumberFormat(undefined, { style: "currency", currency }).format(amountMinor / 100);
}

function checkoutRequestId() {
  return `checkout_${crypto.randomUUID().replaceAll("-", "")}`;
}

function browserStorage() {
  try { return window.localStorage; } catch { return null; }
}

function returnDetails(location) {
  const params = new URLSearchParams(location.search);
  return { checkout: params.get("checkout"), sessionId: params.get("session_id") };
}

async function boot() {
  const configuration = window.CARTRAY_STOREFRONT;
  const status = document.querySelector("#status");
  const productsElement = document.querySelector("#products");
  const cartElement = document.querySelector("#cart");
  const checkoutButton = document.querySelector("#checkout");
  const continueShopping = document.querySelector("#continue-shopping");
  if (!configuration) {
    status.textContent = "Storefront configuration is missing. Run the storefront build before serving this page.";
    return;
  }
  let catalogue;
  try { catalogue = await loadCatalogue(configuration); } catch {
    status.textContent = "Catalogue is currently unavailable.";
    return;
  }
  const storage = browserStorage();
  const restored = loadPersistedCart(storage, catalogue);
  const cart = restored.cart;
  let revision = restored.revision;
  const returning = returnDetails(window.location);
  let checkoutLocked = returning.checkout === "complete";
  const addButtons = [];

  function renderCart() {
    cartElement.replaceChildren();
    for (const product of catalogue.products) {
      const quantity = cart.get(product.product_key);
      if (!quantity) continue;
      const line = document.createElement("li");
      line.textContent = `${product.title} × ${quantity}`;
      cartElement.append(line);
    }
    checkoutButton.disabled = checkoutLocked || !configuration.checkoutEnabled || cart.size === 0;
    for (const add of addButtons) add.disabled = checkoutLocked;
  }

  function updateCart(product) {
    addToCart(cart, product);
    revision += 1;
    persistCart(storage, catalogue.version, cart, revision);
    renderCart();
  }

  for (const product of catalogue.products) {
    const card = document.createElement("article");
    card.className = "product";
    const title = document.createElement("h3");
    title.textContent = product.title;
    const price = document.createElement("p");
    price.textContent = money(product.amount_minor, product.currency);
    const add = document.createElement("button");
    add.type = "button";
    add.textContent = "Add to cart";
    add.addEventListener("click", () => updateCart(product));
    addButtons.push(add);
    card.append(title, price, add);
    productsElement.append(card);
  }
  status.textContent = configuration.checkoutEnabled ? "" : "Preview catalogue: cart interactions are enabled; checkout is disabled.";
  renderCart();
  if (returning.checkout === "cancelled") {
    status.textContent = "Checkout cancelled.";
    window.history.replaceState(null, "", window.location.pathname);
  }
  if (returning.checkout === "complete") {
    status.textContent = "Confirming your order…";
    const result = await processCheckoutReturn({
      configuration,
      sessionId: returning.sessionId,
      storage,
      catalogue,
      cart,
      revision,
      poll: pollCheckoutStatus,
    });
    const { state } = result;
    if (state === "confirmed") {
      if (result.cleared) revision = 0;
      window.history.replaceState(null, "", successReturnPath(window.location.pathname, state));
      continueShopping.href = window.location.pathname;
      continueShopping.hidden = false;
      status.textContent = "Order confirmed.";
    } else if (state === "pending") {
      status.textContent = "Your order is still being confirmed. You can safely close this page.";
    } else {
      status.textContent = "We could not confirm this checkout.";
    }
    renderCart();
    return;
  }
  if (!configuration.checkoutEnabled) return;
  checkoutButton.addEventListener("click", async () => {
    checkoutLocked = true;
    renderCart();
    try {
      const checkout = await createCheckoutAndStorePending({
        configuration,
        catalogueVersion: catalogue.version,
        cart,
        revision,
        requestId: checkoutRequestId(),
        storage,
      });
      window.location.assign(checkout.url);
    } catch {
      checkoutLocked = false;
      status.textContent = "Checkout is currently unavailable.";
      renderCart();
    }
  });
}

if (typeof document !== "undefined") boot();
