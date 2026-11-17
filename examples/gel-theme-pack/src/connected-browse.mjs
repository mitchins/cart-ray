/** Connect catalogue add buttons to CartRay's authoritative cart, without changing browse listings. */
import {createAdapter} from './cartray-adapter.mjs';
const config = window.CARTRAY_THEME;
const message = document.querySelector('#uat-cart-message');
try {
  if (config.contextPath) {
    const context = await fetch(config.contextPath, {cache:'no-store'});
    if (!context.ok) throw new Error('context unavailable');
  }
  const adapter = await createAdapter(config);
  const mappings = new Map(config.browseOffers.map(item => [String(item.sourceId), item.productKey]));
  for (const button of document.querySelectorAll('[data-uat-add]')) {
    const key = mappings.get(button.dataset.uatAdd);
    if (!key || !adapter.products.some(product => product.product_key === key)) continue;
    button.disabled = false;
    button.addEventListener('click', () => {
      const added = adapter.add(key);
      if (message) message.textContent = added ? 'Added to your cart.' : 'This item cannot be added right now.';
    });
  }
} catch {
  if (message) message.textContent = 'Checkout is unavailable. You can continue browsing.';
}
