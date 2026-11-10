/** Browser-only preview. Never copied into a connected CartRay build. */
export function cleanCart(raw, products) {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return {};
  const cart = {};
  for (const p of products) {
    const q = raw[p.product_key];
    if (Number.isSafeInteger(q) && q > 0) cart[p.product_key] = Math.min(q, p.max_quantity);
  }
  return cart;
}
export function newDemoStore(configuration, storage) {
  if (configuration.preview !== true) throw new Error('Demo adapter requires an explicit preview build.');
  const key = `cartray.theme-demo.v1:${configuration.gel.identity.key}`;
  let saved = {};
  try { saved = JSON.parse(storage?.getItem(key) || '{}'); } catch { /* optional storage */ }
  if (!saved || typeof saved !== 'object' || Array.isArray(saved)) saved = {};
  const products = configuration.catalogue.products;
  const state = {
    cart: cleanCart(saved.cart, products),
    permission: typeof saved.permission === 'boolean' ? saved.permission : configuration.catalogue.review_invitation_default_checked,
    suppressed: saved.suppressed === true,
    reviews: saved.reviews && typeof saved.reviews === 'object' && !Array.isArray(saved.reviews) ? saved.reviews : {},
    order: saved.order && Array.isArray(saved.order.items) ? saved.order : null,
  };
  function save() { try { storage?.setItem(key, JSON.stringify(state)); } catch { /* functional in memory */ } }
  return {
    preview: true, products, state, checkoutLocked: false,
    permissionEnabled: true, checkoutEnabled: true,
    getCart() { return products.filter(p => state.cart[p.product_key]).map(p => ({...p, quantity: state.cart[p.product_key]})); },
    count() { return this.getCart().reduce((n,p) => n+p.quantity, 0); },
    subtotal() { return this.getCart().reduce((n,p) => n+p.quantity*p.amount_minor, 0); },
    quantity(key, q) {
      const p = products.find(x=>x.product_key===key);
      if (!p || !Number.isSafeInteger(q) || q < 0 || this.checkoutLocked) return false;
      if (q===0) delete state.cart[key]; else state.cart[key]=Math.min(q,p.max_quantity);
      save(); return true;
    },
    add(key) { return this.quantity(key,(state.cart[key]||0)+1); },
    setPermission(value) { state.permission=value===true; save(); },
    async checkout() {
      if (!this.count()) throw new Error('Add something to your cart first.');
      return configuration.base+'checkout-preview/';
    },
    simulatePayment() {
      if (!this.count()) throw new Error('This preview cart is empty.');
      state.order={reference:'PREVIEW-001',items:this.getCart(),permission:state.permission,invitation:state.permission && !state.suppressed && this.getCart().some(p=>p.review_subject)};
      state.cart={}; save();
      return configuration.base+'purchase-return/?state=confirmed';
    },
    reviewable() {
      return (state.order?.items || products).filter(p=>p.review_subject);
    },
    review(key, value) {
      if (!this.reviewable().some(p=>p.product_key===key) || state.suppressed || state.reviews[key]?.status==='withdrawn') throw new Error('This preview invitation is unavailable.');
      if (!value.rating && value.body.trim().length<3) throw new Error('Choose a rating or write at least three characters about your experience.');
      state.reviews[key]={...value,status:'pending'}; save();
    },
    withdraw(key) { if (state.reviews[key]) {state.reviews[key].status='withdrawn'; save();} },
    unsubscribe() { state.suppressed=true; save(); },
    reset() { try {storage?.removeItem(key);} catch { /* optional */ } },
    async returnState() { return null; },
  };
}
export async function createAdapter(config) {
  let storage; try {storage=window.localStorage;} catch {storage=null;}
  return newDemoStore(config,storage);
}
