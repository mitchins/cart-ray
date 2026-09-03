import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import {
  addToCart,
  checkoutPayload,
  checkoutStatus,
  consumePendingCheckout,
  createCheckoutAndStorePending,
  loadCatalogue,
  loadPendingCheckout,
  loadPersistedCart,
  matchingPendingCheckout,
  persistCart,
  pollCheckoutStatus,
  processCheckoutReturn,
  startCheckout,
  storePendingCheckout,
  successReturnPath,
} from "../storefront/app.js";

function memoryStorage() {
  const values = new Map();
  return {
    getItem: (key) => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, value),
    removeItem: (key) => values.delete(key),
  };
}

const catalogue = {
  version: "sha256:catalogue",
  products: [
    { product_key: "TEST-TEMPLATE", max_quantity: 1 },
    { product_key: "TEST-SUPPORT-HOURS", max_quantity: 5 },
  ],
};

async function builtConfiguration() {
  const source = await readFile(new URL("../storefront-dist/storefront-config.js", import.meta.url), "utf8");
  return JSON.parse(source.replace("window.CARTRAY_STOREFRONT = ", "").trim().replace(/;$/, ""));
}

test("generated storefront configuration is safe for its declared build mode", async () => {
  const configuration = await builtConfiguration();
  if (process.env.CARTRAY_STOREFRONT_EXPECTED_MODE === "preview") {
    assert.equal(configuration.checkoutEnabled, false);
    assert.equal(configuration.apiBaseUrl, null);
    assert.match(configuration.previewCatalogue.version, /^sha256:[0-9a-f]{64}$/);
    assert.match(configuration.previewCatalogue.presentation_version, /^sha256:[0-9a-f]{64}$/);
    assert.deepEqual(Object.keys(configuration.previewCatalogue.products[0]).sort(), [
      "amount_minor",
      "currency",
      "image_url",
      "max_quantity",
      "product_key",
      "short_description",
      "title",
    ]);
    await readFile(new URL("../storefront-dist/assets/products/test-template-cover-v1.webp", import.meta.url));
    return;
  }
  assert.equal(process.env.CARTRAY_STOREFRONT_EXPECTED_MODE, "production");
  assert.deepEqual(configuration, {
    checkoutEnabled: true,
    apiBaseUrl: "https://api.test.invalid",
    previewCatalogue: null,
  });
});

test("checkout payload contains only the CartRay browser contract", () => {
  const payload = checkoutPayload("sha256:catalogue", new Map([["TEST-TEMPLATE", 1]]), "checkout_1");

  assert.deepEqual(payload, {
    checkout_request_id: "checkout_1",
    manifest_version: "sha256:catalogue",
    items: [{ product_key: "TEST-TEMPLATE", quantity: 1 }],
  });
});

test("cart interaction reaches the synthetic product maximum and preserves it in the payload", () => {
  const cart = new Map();
  const product = { product_key: "TEST-SUPPORT-HOURS", max_quantity: 5 };
  for (let index = 0; index < 6; index += 1) {
    addToCart(cart, product);
  }

  assert.equal(cart.get(product.product_key), 5);
  assert.deepEqual(checkoutPayload("sha256:catalogue", cart, "checkout_support_hours"), {
    checkout_request_id: "checkout_support_hours",
    manifest_version: "sha256:catalogue",
    items: [{ product_key: "TEST-SUPPORT-HOURS", quantity: 5 }],
  });
});

test("preview configuration cannot submit checkout", async () => {
  await assert.doesNotReject(async () => {
    assert.equal(
      await startCheckout({ checkoutEnabled: false, apiBaseUrl: "https://api.test.invalid" }, {}, () => {
        throw new Error("preview must not fetch");
      }),
      null,
    );
  });
});

test("preview catalogue is loaded locally without a network request", async () => {
  const previewCatalogue = { version: "sha256:preview", products: [{ product_key: "TEST-TEMPLATE" }] };
  assert.equal(
    await loadCatalogue({ checkoutEnabled: false, apiBaseUrl: null, previewCatalogue }, () => {
      throw new Error("preview catalogue must not fetch");
    }),
    previewCatalogue,
  );
});

test("production checkout posts exactly the trusted browser payload", async () => {
  const payload = checkoutPayload("sha256:catalogue", new Map([["TEST-TEMPLATE", 1]]), "checkout_1");
  let request;
  const checkout = await startCheckout({ checkoutEnabled: true, apiBaseUrl: "https://api.test.invalid" }, payload, async (...args) => {
    request = args;
    return { ok: true, json: async () => ({ checkout_url: "https://checkout.stripe.test/session", session_id: "cs_test_1" }) };
  });

  assert.deepEqual(checkout, { url: "https://checkout.stripe.test/session", sessionId: "cs_test_1" });
  assert.deepEqual(request, [
    "https://api.test.invalid/checkout",
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    },
  ]);
});

test("persisted cart is catalogue-scoped, revalidated, and quantity-clamped", () => {
  const storage = memoryStorage();
  persistCart(storage, catalogue.version, new Map([["TEST-SUPPORT-HOURS", 99]]), 4);
  const restored = loadPersistedCart(storage, catalogue);
  assert.deepEqual([...restored.cart], [["TEST-SUPPORT-HOURS", 5]]);
  assert.equal(restored.revision, 4);

  persistCart(storage, "sha256:old", new Map([["TEST-TEMPLATE", 1]]), 5);
  assert.deepEqual([...loadPersistedCart(storage, catalogue).cart], []);

  storage.setItem("cartray.cart.v1", JSON.stringify({
    catalogueVersion: catalogue.version,
    items: [["UNKNOWN", 1]],
    revision: 6,
  }));
  assert.deepEqual([...loadPersistedCart(storage, catalogue).cart], []);
});

test("only the matching unchanged pending checkout may clear a cart", () => {
  const storage = memoryStorage();
  const cart = new Map([["TEST-TEMPLATE", 1]]);
  persistCart(storage, catalogue.version, cart, 7);
  storePendingCheckout(storage, { sessionId: "cs_test_matching" }, catalogue.version, cart, 7);
  const pending = loadPendingCheckout(storage);

  assert.equal(matchingPendingCheckout("cs_test_matching", pending, cart, 7, catalogue.version), true);
  assert.equal(matchingPendingCheckout("cs_test_foreign", pending, cart, 7, catalogue.version), false);
  assert.equal(matchingPendingCheckout("cs_test_matching", pending, cart, 8, catalogue.version), false);
  assert.equal(matchingPendingCheckout("cs_test_matching", pending, new Map([["TEST-SUPPORT-HOURS", 1]]), 7, catalogue.version), false);

  consumePendingCheckout(storage, "cs_test_foreign");
  assert.deepEqual(loadPendingCheckout(storage), pending);
  consumePendingCheckout(storage, "cs_test_matching");
  assert.equal(loadPendingCheckout(storage), null);
});

test("return orchestration clears only the correlated unchanged cart and retains pending carts", async () => {
  const storage = memoryStorage();
  const cart = new Map([["TEST-TEMPLATE", 1]]);
  persistCart(storage, catalogue.version, cart, 7);
  storePendingCheckout(storage, { sessionId: "cs_test_return" }, catalogue.version, cart, 7);

  const pending = await processCheckoutReturn({
    configuration: {}, sessionId: "cs_test_return", storage, catalogue, cart, revision: 7, poll: async () => "pending",
  });
  assert.deepEqual(pending, { state: "pending", cleared: false });
  assert.deepEqual([...loadPersistedCart(storage, catalogue).cart], [["TEST-TEMPLATE", 1]]);
  assert.equal(successReturnPath("/", pending.state), null);

  const confirmed = await processCheckoutReturn({
    configuration: {}, sessionId: "cs_test_return", storage, catalogue, cart, revision: 7, poll: async () => "confirmed",
  });
  assert.deepEqual(confirmed, { state: "confirmed", cleared: true });
  assert.deepEqual([...cart], []);
  assert.deepEqual([...loadPersistedCart(storage, catalogue).cart], []);
  assert.equal(successReturnPath("/", confirmed.state), "/?checkout=complete");
});

test("a cart changed during checkout creation cannot be cleared by that checkout return", async () => {
  const storage = memoryStorage();
  const cart = new Map([["TEST-TEMPLATE", 1]]);
  persistCart(storage, catalogue.version, cart, 1);
  let resolveCheckout;
  const checkoutPromise = createCheckoutAndStorePending({
    configuration: { checkoutEnabled: true, apiBaseUrl: "https://api.test.invalid" },
    catalogueVersion: catalogue.version,
    cart,
    revision: 1,
    requestId: "checkout_1",
    storage,
    fetchImpl: () => new Promise((resolve) => { resolveCheckout = resolve; }),
  });
  addToCart(cart, catalogue.products[1]);
  persistCart(storage, catalogue.version, cart, 2);
  resolveCheckout({ ok: true, json: async () => ({ checkout_url: "https://checkout.stripe.test/session", session_id: "cs_test_race" }) });
  await checkoutPromise;

  const result = await processCheckoutReturn({
    configuration: {}, sessionId: "cs_test_race", storage, catalogue, cart, revision: 2, poll: async () => "confirmed",
  });
  assert.deepEqual(result, { state: "confirmed", cleared: false });
  assert.deepEqual([...loadPersistedCart(storage, catalogue).cart], [["TEST-SUPPORT-HOURS", 1], ["TEST-TEMPLATE", 1]]);
});

test("return polling treats status as a tiny confirmation signal", async () => {
  const responses = ["pending", "confirmed"];
  const requests = [];
  const fetchImpl = async (url) => {
    requests.push(url);
    return { ok: true, json: async () => ({ state: responses.shift() }) };
  };
  assert.equal(
    await pollCheckoutStatus({ apiBaseUrl: "https://api.test.invalid" }, "cs_test_return", fetchImpl, async () => {}),
    "confirmed",
  );
  assert.deepEqual(requests, [
    "https://api.test.invalid/checkout-status?session_id=cs_test_return",
    "https://api.test.invalid/checkout-status?session_id=cs_test_return",
  ]);
  assert.equal(await checkoutStatus({ apiBaseUrl: "https://api.test.invalid" }, "not-a-session", fetchImpl), null);

  assert.equal(
    await pollCheckoutStatus(
      { apiBaseUrl: "https://api.test.invalid" },
      "cs_test_timeout",
      () => new Promise(() => {}),
      async () => {},
      1,
    ),
    null,
  );

  let now = 0;
  let calls = 0;
  assert.equal(
    await pollCheckoutStatus(
      { apiBaseUrl: "https://api.test.invalid" },
      "cs_test_pending_deadline",
      async () => {
        calls += 1;
        return { ok: true, json: async () => ({ state: "pending" }) };
      },
      async (milliseconds) => { now += milliseconds; },
      5,
      () => now,
    ),
    "pending",
  );
  assert.equal(calls, 1);
});

test("confirmed view has an explicit route back to the normal storefront", async () => {
  const source = await readFile(new URL("../storefront/index.html", import.meta.url), "utf8");
  assert.match(source, /id="continue-shopping" href="\/" hidden/);
  const application = await readFile(new URL("../storefront/app.js", import.meta.url), "utf8");
  assert.match(application, /continueShopping\.hidden = false/);
});
