import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import { checkoutPayload, loadCatalogue, startCheckout } from "../storefront/app.js";

async function builtConfiguration() {
  const source = await readFile(new URL("../storefront-dist/storefront-config.js", import.meta.url), "utf8");
  return JSON.parse(source.replace("window.CARTRAY_STOREFRONT = ", "").trim().replace(/;$/, ""));
}

test("generated storefront configuration is safe for its declared build mode", async () => {
  const configuration = await builtConfiguration();
  if (process.env.CARTRAY_STOREFRONT_EXPECTED_MODE === "preview") {
    assert.equal(configuration.checkoutEnabled, false);
    assert.equal(configuration.apiBaseUrl, null);
    assert.equal(configuration.previewCatalogue.version, "sha256:preview-fixtures-v1");
    assert.deepEqual(Object.keys(configuration.previewCatalogue.products[0]).sort(), [
      "amount_minor",
      "currency",
      "max_quantity",
      "product_key",
      "title",
    ]);
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
  const url = await startCheckout({ checkoutEnabled: true, apiBaseUrl: "https://api.test.invalid" }, payload, async (...args) => {
    request = args;
    return { ok: true, json: async () => ({ checkout_url: "https://checkout.stripe.test/session" }) };
  });

  assert.equal(url, "https://checkout.stripe.test/session");
  assert.deepEqual(request, [
    "https://api.test.invalid/checkout",
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    },
  ]);
});
