const CART_STORAGE_KEY = "cartray.cart.v1";
const PENDING_CHECKOUT_STORAGE_KEY = "cartray.pending-checkout.v1";
const CHECKOUT_SESSION_ID_RE = /^cs_[A-Za-z0-9_]+$/;
const RETURN_POLL_DELAYS_MS = [0, 1_000, 2_000, 4_000, 8_000, 15_000];

export function checkoutPayload(catalogueVersion, cart, checkoutRequestId, reviewInvitationOptIn = undefined) {
  return {
    checkout_request_id: checkoutRequestId,
    ...(typeof reviewInvitationOptIn === "boolean" ? { review_invitation_opt_in: reviewInvitationOptIn } : {}),
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
  if (!response.ok) {
    const error = new Error("Checkout could not be started.");
    error.checkoutStatus = response.status;
    throw error;
  }
  const result = await response.json();
  if (typeof result.checkout_url !== "string" || !validSessionId(result.session_id)) {
    throw new Error("Checkout response was incomplete.");
  }
  return { url: result.checkout_url, sessionId: result.session_id };
}

export async function createCheckoutAndStorePending({ configuration, catalogueVersion, cart, revision, requestId, storage, fetchImpl, reviewInvitationOptIn = undefined }) {
  const submittedCart = new Map(cart);
  const submittedRevision = revision;
  const checkout = await startCheckout(
    configuration,
    checkoutPayload(catalogueVersion, submittedCart, requestId, reviewInvitationOptIn),
    fetchImpl,
  );
  storePendingCheckout(storage, checkout, catalogueVersion, submittedCart, submittedRevision);
  return checkout;
}

export async function checkoutStatus(configuration, sessionId, fetchImpl = fetch, signal) {
  return (await checkoutStatusDetails(configuration, sessionId, fetchImpl, signal))?.state ?? null;
}

export async function checkoutStatusDetails(configuration, sessionId, fetchImpl = fetch, signal) {
  if (!validSessionId(sessionId)) return null;
  try {
    const response = await fetchImpl(
      `${configuration.apiBaseUrl}/checkout-status?session_id=${encodeURIComponent(sessionId)}`,
      signal ? { signal } : undefined,
    );
    if (!response.ok) return null;
    const result = await response.json();
    if (!["pending", "confirmed", "expired"].includes(result?.state)) return null;
    const receipt = result.state === "confirmed" ? verifiedReceipt(result.receipt) : null;
    return { state: result.state, ...(receipt ? { receipt } : {}) };
  } catch {
    return null;
  }
}

export async function pollCheckoutStatus(
  configuration, sessionId, fetchImpl = fetch, sleep = delay, deadlineMs = 30_000, now = Date.now,
) {
  return (await pollCheckoutDetails(configuration, sessionId, fetchImpl, sleep, deadlineMs, now))?.state ?? null;
}

export async function pollCheckoutDetails(
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
    const result = await checkoutStatusBeforeDeadline(configuration, sessionId, fetchImpl, remaining);
    if (result?.state === "confirmed" || result?.state === "expired") return result;
    if (result?.state === "pending") lastState = result;
    else return lastState;
  }
  return lastState;
}

export async function processCheckoutReturn({ configuration, sessionId, storage, catalogue, cart, revision, poll }) {
  const result = await poll(configuration, sessionId);
  const state = typeof result === "string" ? result : result?.state ?? null;
  if (state !== "confirmed") return { state, cleared: false };
  const current = loadPersistedCart(storage, catalogue);
  const pending = loadPendingCheckout(storage);
  const cleared = matchingPendingCheckout(sessionId, pending, current.cart, current.revision, catalogue.version);
  if (cleared) {
    cart.clear();
    try { storage?.removeItem(CART_STORAGE_KEY); } catch { /* storage is optional */ }
  }
  consumePendingCheckout(storage, sessionId);
  const receipt = verifiedReceipt(result?.receipt);
  return { state, cleared, ...(receipt ? { receipt } : {}) };
}

export function successReturnPath(pathname, state) {
  return state === "confirmed" || state === "expired" ? pathname : null;
}

export function confirmedCheckoutView(cleared, receipt, products = []) {
  const verified = verifiedReceipt(receipt);
  // Current catalogue titles are display labels, not historical receipt data.
  const titles = new Map(products.map(product => [product.product_key, product.title]));
  return {
    checkoutLocked: false,
    message: cleared ? "Order confirmed. Your cart is ready for another purchase." : "Order confirmed. Your cart was kept.",
    actionLabel: "Continue shopping",
    ...(verified ? { receipt: verified, purchasedItems: verified.items.map(item => {
      const title = titles.get(item.product_key);
      const label = typeof title === "string" && title.trim() ? `${title} (${item.product_key})` : item.product_key;
      return `${label} × ${item.quantity} (${money(item.unit_amount_minor, verified.currency)} each)`;
    }),
    } : {}),
  };
}

export function expiredCheckoutView() {
  return {
    checkoutLocked: false,
    message: "This Checkout expired. Your cart is ready for another purchase.",
    actionLabel: "Continue shopping",
  };
}

export function unconfirmedCheckoutView(state) {
  if (state === "pending") {
    return {
      checkoutLocked: true,
      message: "Your order is still being confirmed. You can safely close this page.",
      actionLabel: "Check order status again",
    };
  }
  return {
    checkoutLocked: true,
    message: "We could not confirm this checkout. Check order status again.",
    actionLabel: "Check order status again",
  };
}

export function cancelledCheckoutView() {
  return { checkoutLocked: false, message: "Checkout cancelled. Your cart is ready when you are." };
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

// Receipts originate only in the status response, never a pending snapshot or current catalogue/cart.
function verifiedReceipt(value) {
  if (!value || !/^[A-Za-z]{3}$/.test(value.currency) || !Array.isArray(value.items) || !value.items.length) return null;
  const keys = new Set();
  const items = [];
  for (const item of value.items) {
    if (!item || typeof item.product_key !== "string" || !item.product_key.trim() || keys.has(item.product_key) ||
        !Number.isSafeInteger(item.quantity) || item.quantity < 1 ||
        !Number.isSafeInteger(item.unit_amount_minor) || item.unit_amount_minor < 0) return null;
    keys.add(item.product_key);
    items.push({ product_key: item.product_key, quantity: item.quantity, unit_amount_minor: item.unit_amount_minor });
  }
  return { currency: value.currency, items };
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
    return await Promise.race([checkoutStatusDetails(configuration, sessionId, fetchImpl, controller.signal), timeout]);
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
  const reviewInvitationChoice = document.querySelector("#review-invitation-choice");
  const reviewInvitationOptIn = document.querySelector("#review-invitation-opt-in");
  if (reviewInvitationChoice) reviewInvitationChoice.hidden = !configuration?.reviewInvitationsEnabled;
  if (reviewInvitationOptIn) reviewInvitationOptIn.checked = configuration?.reviewInvitationDefaultChecked === true;
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
    if (configuration.productKey && configuration.productKey !== product.product_key) continue;
    const card = document.createElement("article");
    card.className = "product";
    card.dataset.productKey = product.product_key;
    let image;
    if (typeof product.image_url === "string") {
      image = document.createElement("img");
      image.src = product.image_url;
      image.alt = "";
      image.loading = "lazy";
    }
    const title = document.createElement("h3");
    title.textContent = product.title;
    let description;
    if (typeof product.short_description === "string") {
      description = document.createElement("p");
      description.className = "product-description";
      description.textContent = product.short_description;
    }
    const price = document.createElement("p");
    price.textContent = money(product.amount_minor, product.currency);
    const add = document.createElement("button");
    add.type = "button";
    add.textContent = "Add to cart";
    add.addEventListener("click", () => updateCart(product));
    addButtons.push(add);
    if (image) card.append(image);
    card.append(title);
    const productPath = configuration.productPaths?.[product.product_key];
    if (typeof productPath === "string" && /^\/products\/[a-z0-9-]+\/$/.test(productPath)) {
      const link = document.createElement("a");
      link.href = productPath;
      link.textContent = `View ${product.title}`;
      card.append(link);
    }
    if (description) card.append(description);
    card.append(price, add);
    productsElement.append(card);
  }
  status.textContent = configuration.checkoutEnabled ? "" : "Preview catalogue: cart interactions are enabled; checkout is disabled.";
  renderCart();

  if (configuration.checkoutEnabled) {
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
          reviewInvitationOptIn: configuration.reviewInvitationsEnabled ? reviewInvitationOptIn?.checked === true : undefined,
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

  if (returning.checkout === "cancelled") {
    status.textContent = cancelledCheckoutView().message;
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
      poll: pollCheckoutDetails,
    });
    if (result.state === "confirmed") {
      if (result.cleared) revision = 0;
      const view = confirmedCheckoutView(result.cleared, result.receipt, catalogue.products);
      checkoutLocked = view.checkoutLocked;
      window.history.replaceState(null, "", successReturnPath(window.location.pathname, result.state));
      continueShopping.href = window.location.pathname;
      continueShopping.textContent = view.actionLabel;
      continueShopping.hidden = false;
      status.textContent = view.message;
      if (view.purchasedItems) {
        const receipt = document.createElement("ul");
        receipt.setAttribute("aria-label", "Purchased items");
        for (const text of view.purchasedItems) {
          const line = document.createElement("li");
          line.textContent = text;
          receipt.append(line);
        }
        status.after(receipt);
      }
    } else if (result.state === "expired") {
      const view = expiredCheckoutView();
      checkoutLocked = view.checkoutLocked;
      window.history.replaceState(null, "", successReturnPath(window.location.pathname, result.state));
      continueShopping.href = window.location.pathname;
      continueShopping.textContent = view.actionLabel;
      continueShopping.hidden = false;
      status.textContent = view.message;
    } else {
      const view = unconfirmedCheckoutView(result.state);
      checkoutLocked = view.checkoutLocked;
      continueShopping.href = window.location.href;
      continueShopping.textContent = view.actionLabel;
      continueShopping.hidden = false;
      status.textContent = view.message;
    }
    renderCart();
    return;
  }
}

if (typeof document !== "undefined" && !document.querySelector('[data-cartray-presenter="gel"]')) boot();
