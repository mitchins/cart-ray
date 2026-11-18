import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';

test('browsing makes no checkout reads until the shopper adds a product', async () => {
  const source = (await readFile(new URL('../src/connected-browse.mjs', import.meta.url), 'utf8'))
    .replace("import {createAdapter} from './cartray-adapter.mjs';", 'const createAdapter=globalThis.__testAdapter;');
  let listener, contextCalls = 0, adapterCalls = 0, added = 0;
  const button = {dataset:{uatAdd:'1'}, disabled:true, addEventListener:(_, callback) => {listener=callback;}};
  const message = {textContent:''};
  const saved = {window:globalThis.window, document:globalThis.document, fetch:globalThis.fetch,
    adapter:globalThis.__testAdapter};
  try {
    globalThis.window = {CARTRAY_THEME:{contextPath:'/__uat/context',browseOffers:[{sourceId:1,productKey:'P1'}]}};
    globalThis.document = {querySelector: selector => selector === '#uat-cart-message' ? message : null,
      querySelectorAll: () => [button]};
    globalThis.fetch = async () => {contextCalls++; return {ok:true};};
    globalThis.__testAdapter = async () => {adapterCalls++; return {add: key => {added++; return key === 'P1';}};};
    await import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`);
    assert.equal(contextCalls, 0);
    assert.equal(adapterCalls, 0);
    assert.equal(button.disabled, false);
    await listener();
    assert.equal(contextCalls, 1);
    assert.equal(adapterCalls, 1);
    assert.equal(added, 1);
    assert.equal(message.textContent, 'Added to your cart.');
  } finally {
    globalThis.window=saved.window; globalThis.document=saved.document; globalThis.fetch=saved.fetch;
    globalThis.__testAdapter=saved.adapter;
  }
});
