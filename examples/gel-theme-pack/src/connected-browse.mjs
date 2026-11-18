/** Initialise checkout data only when a visitor chooses to add an item. */
import {createAdapter} from './cartray-adapter.mjs';
const config = window.CARTRAY_THEME;
const message = document.querySelector('#cartray-cart-message') || document.querySelector('#uat-cart-message');
const mappings = new Map(config.browseOffers.map(item => [String(item.sourceId), item.productKey]));
let loading;
async function adapterForAction() {
  if (!loading) loading = (async () => {
    if (config.contextPath) {
      const context = await fetch(config.contextPath, {cache:'no-store'});
      if (!context.ok) throw new Error('context unavailable');
    }
    return createAdapter(config);
  })().catch(error => { loading = null; throw error; });
  return loading;
}
for (const button of document.querySelectorAll('[data-cartray-add], [data-uat-add]')) {
  const key = button.dataset.cartrayAdd || mappings.get(button.dataset.uatAdd);
  if (!key) continue;
  button.disabled = false;
  button.addEventListener('click', async () => {
    button.disabled = true;
    try {
      const adapter = await adapterForAction();
      const added = adapter.add(key);
      if (message) message.textContent = added ? 'Added to your cart.' : adapter.attemptPending
        ? 'A checkout is already in progress. Visit your cart to retry it.' : 'This item cannot be added right now.';
    } catch {
      if (message) message.textContent = 'Checkout is unavailable. You can continue browsing.';
    } finally { button.disabled = false; }
  });
}
