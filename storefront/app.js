export function checkoutPayload(catalogueVersion, cart, checkoutRequestId) {
  return {
    checkout_request_id: checkoutRequestId,
    manifest_version: catalogueVersion,
    items: [...cart.entries()].map(([productKey, quantity]) => ({ product_key: productKey, quantity })),
  };
}

export async function startCheckout(configuration, payload, fetchImpl = fetch) {
  if (!configuration.checkoutEnabled) {
    return null;
  }
  const response = await fetchImpl(`${configuration.apiBaseUrl}/checkout`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!response.ok) {
    throw new Error("Checkout could not be started.");
  }
  const result = await response.json();
  if (typeof result.checkout_url !== "string") {
    throw new Error("Checkout response was incomplete.");
  }
  return result.checkout_url;
}

export async function loadCatalogue(configuration, fetchImpl = fetch) {
  if (configuration.previewCatalogue) {
    return configuration.previewCatalogue;
  }
  if (!configuration.apiBaseUrl) {
    throw new Error("Storefront configuration has no catalogue source.");
  }
  const response = await fetchImpl(`${configuration.apiBaseUrl}/catalogue`);
  if (!response.ok) {
    throw new Error("Catalogue could not be loaded.");
  }
  return response.json();
}

function money(amountMinor, currency) {
  return new Intl.NumberFormat(undefined, { style: "currency", currency }).format(amountMinor / 100);
}

function checkoutRequestId() {
  return `checkout_${crypto.randomUUID().replaceAll("-", "")}`;
}

async function boot() {
  const configuration = window.CARTRAY_STOREFRONT;
  const status = document.querySelector("#status");
  const productsElement = document.querySelector("#products");
  const cartElement = document.querySelector("#cart");
  const checkoutButton = document.querySelector("#checkout");
  const cart = new Map();
  if (!configuration) {
    status.textContent = "Storefront configuration is missing. Run the storefront build before serving this page.";
    return;
  }

  let catalogue;
  try {
    catalogue = await loadCatalogue(configuration);
  } catch {
    status.textContent = "Catalogue is currently unavailable.";
    return;
  }

  function renderCart() {
    cartElement.replaceChildren();
    for (const product of catalogue.products) {
      const quantity = cart.get(product.product_key);
      if (!quantity) continue;
      const line = document.createElement("li");
      line.textContent = `${product.title} × ${quantity}`;
      cartElement.append(line);
    }
    checkoutButton.disabled = !configuration.checkoutEnabled || cart.size === 0;
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
    add.addEventListener("click", () => {
      cart.set(product.product_key, Math.min((cart.get(product.product_key) || 0) + 1, product.max_quantity));
      renderCart();
    });
    card.append(title, price, add);
    productsElement.append(card);
  }
  status.textContent = configuration.checkoutEnabled
    ? ""
    : "Preview catalogue: cart interactions are enabled; checkout is disabled.";
  renderCart();
  if (!configuration.checkoutEnabled) {
    return;
  }
  checkoutButton.addEventListener("click", async () => {
    checkoutButton.disabled = true;
    try {
      const url = await startCheckout(configuration, checkoutPayload(catalogue.version, cart, checkoutRequestId()));
      window.location.assign(url);
    } catch {
      status.textContent = "Checkout is currently unavailable.";
      renderCart();
    }
  });
}

if (typeof document !== "undefined") {
  boot();
}
