import {mountGallery} from './gallery.mjs';
const products=document.querySelector('#products');
if(products){
  const original=[...products.querySelectorAll('.product-card')];
  const search=document.querySelector('#search'),category=document.querySelector('#category'),sort=document.querySelector('#sort');
  function update(){
    const query=search.value.trim().toLowerCase(),choice=category.value;
    let visible=0;
    for(const card of original){
      const show=card.dataset.search.includes(query)&&(!choice||card.dataset.categoryIds.split(' ').includes(choice));
      card.hidden=!show;if(show)visible++;
    }
    const cards=[...original];
    if(sort.value==='name')cards.sort((a,b)=>a.dataset.name.localeCompare(b.dataset.name));
    if(sort.value==='low')cards.sort((a,b)=>{
      const number=x=>x.dataset.price.trim()===''?Infinity:Number(x.dataset.price);
      return number(a)-number(b);
    });
    for(const card of cards)products.append(card);
    document.querySelector('#results-status').textContent=`${visible} products`;
  }
  search.addEventListener('input',update);category.addEventListener('change',update);sort.addEventListener('change',update);
}
const script=document.querySelector('#product-data');
if(script){
  const product=JSON.parse(script.textContent);let root=document.querySelector('[data-product-gallery]');
  if(root)mountGallery(root,product,'/');
  const select=document.querySelector('[data-variant]');
  if(select){
    const storageKey=`ep-uat-variant:${location.pathname}`;
    const defaults=product.default_attributes||{};
    const defaultOption=Object.keys(defaults).length?product.variations.find(v=>Object.entries(defaults).every(([k,value])=>v.attributes[`attribute_${k}`]===value)):null;
    try{select.value=sessionStorage.getItem(storageKey)||defaultOption?.key||'';}catch{select.value=defaultOption?.key||'';}
    function setVariant(){
      const selected=product.variations.find(v=>v.key===select.value);
      document.querySelector('#variant-status').textContent=selected?'Selected option: '+Object.entries(selected.attributes).map(([name,value])=>name.replace(/^attribute_/,'').replaceAll('_',' ')+': '+value).join(', ')+'. Checkout unavailable.':'';
      document.querySelector('#variant-price').textContent=selected&&selected.source_price?'Source price: '+selected.source_price+' '+product.currency+' (not checkout-authorised)':'Price unavailable';
      document.querySelector('#variant-description').textContent=selected?.description||'';
      if(root){
        const items=selected?.images?.length?selected.images.map(i=>({url:'/'+i.path,alt:i.alt})):product.images;
        const fresh=root.cloneNode(true);root.replaceWith(fresh);root=fresh;
        mountGallery(root,{title:product.title,image_url:items[0]?.url||'',images:items},'/');
      }
      try{sessionStorage.setItem(storageKey,select.value);}catch{}
    }
    select.addEventListener('change',setVariant);setVariant();
  }
}

// This cart is enabled only by the loopback, test-mode UAT server. It never
// treats catalogue display data as payment authority.
const testProducts = JSON.parse(document.querySelector('#uat-test-products')?.textContent || '[]');
// Previously issued test Checkouts still return here using their original
// session-scoped request identity, even after the connected cart is installed.
if (testProducts.length && (!window.CARTRAY_CONNECTED_CATALOGUE || document.querySelector('#uat-return'))) {
  const known = new Map(testProducts.map(p => [String(p.source_id), p]));
  const key = 'ep-uat-test-cart-v1';
  const pendingKey = 'ep-uat-test-request-v1';
  const confirmedKey = 'ep-uat-test-confirmed-v1';
  const readCart = () => { try { return JSON.parse(sessionStorage.getItem(key) || '{}'); } catch { return {}; } };
  const saveCart = cart => sessionStorage.setItem(key, JSON.stringify(cart));
  const pending = () => sessionStorage.getItem(pendingKey);
  const status = document.querySelector('#uat-cart-message');
  const cartRoot = document.querySelector('#uat-cart');
  const returnRoot = document.querySelector('#uat-return');
  function renderCart(message = '') {
    if (!cartRoot) return;
    cartRoot.replaceChildren();
    const cart = readCart();
    const entries = Object.entries(cart).filter(([id, qty]) => known.has(id) && Number.isInteger(qty) && qty > 0 && qty <= 10);
    if (message) { const notice = document.createElement('p'); notice.setAttribute('role', 'status'); notice.textContent = message; cartRoot.append(notice); }
    if (!entries.length) { const p = document.createElement('p'); p.textContent = 'Your test cart is empty.'; cartRoot.append(p); return; }
    for (const [id, qty] of entries) {
      const row = document.createElement('p');
      row.append(document.createTextNode(`${known.get(id).title} — AUD ${(known.get(id).amount_minor / 100).toFixed(2)} × `));
      const label = document.createElement('label'); label.textContent = 'Quantity ';
      const input = document.createElement('input'); input.type = 'number'; input.min = '0'; input.max = '10'; input.value = qty; input.disabled = Boolean(pending());
      input.addEventListener('change', () => { const value = Number(input.value); if (!Number.isInteger(value) || value < 0 || value > 10) return renderCart('Choose a quantity from 0 to 10.'); if (value) cart[id] = value; else delete cart[id]; saveCart(cart); renderCart(); });
      label.append(input); row.append(label); cartRoot.append(row);
    }
    const button = document.createElement('button'); button.type = 'button'; button.textContent = pending() ? 'Retry test checkout' : 'Continue to Stripe test checkout';
    button.addEventListener('click', async () => {
      button.disabled = true;
      try {
        const requestId = pending() || crypto.randomUUID(); sessionStorage.setItem(pendingKey, requestId);
        const response = await fetch('/__uat/checkout', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({request_id:requestId, items:entries.map(([source_id,quantity]) => ({source_id:Number(source_id), quantity}))})});
        if (!response.ok) throw new Error('unavailable');
        const data = await response.json(); const url = new URL(data.url);
        if (url.protocol !== 'https:' || url.hostname !== 'checkout.stripe.com') throw new Error('invalid redirect');
        location.assign(url.href);
      } catch { renderCart('Checkout is temporarily unavailable. Your cart is saved; retry to resume the same request.'); }
    });
    cartRoot.append(button);
    if (pending()) { const check = document.createElement('a'); check.href = '/uat-return/'; check.textContent = 'Check payment status or expired checkout'; cartRoot.append(document.createTextNode(' '), check); }
  }
  function renderReturn(data) {
    returnRoot.replaceChildren();
    const grants = Array.isArray(data.grants) ? data.grants : [];
    const heading = document.createElement('p'); heading.setAttribute('role','status');
    heading.textContent = grants.length ? 'Payment confirmed for this test order. Your dummy downloads are ready.' : data.state === 'expired' ? 'This test checkout expired without payment.' : 'Payment confirmation is not yet available. Please retry shortly.'; returnRoot.append(heading);
    if (grants.length && pending()) { sessionStorage.setItem(confirmedKey, pending()); sessionStorage.removeItem(key); sessionStorage.removeItem(pendingKey); }
    for (const grant of grants) {
      if (!Number.isSafeInteger(grant.source_id) || !known.has(String(grant.source_id))) continue;
      const section = document.createElement('section'); const name = document.createElement('h2'); name.textContent = known.get(String(grant.source_id)).title; section.append(name);
      const download = document.createElement('a'); download.href = '/__uat/download?source_id=' + grant.source_id; download.textContent = 'Download dummy test file'; section.append(download);
      for (const item of grant.followups || []) { try { const url = new URL(item); if (url.protocol !== 'https:' || !['www.dropbox.com','dropbox.com'].includes(url.hostname)) continue; const p = document.createElement('p'); const a = document.createElement('a'); a.href = url.href; a.textContent = 'Open supplied dummy Dropbox follow-up'; p.append(a); section.append(p); } catch {} }
      returnRoot.append(section);
    }
    if (data.state === 'expired') { const reset = document.createElement('button'); reset.type = 'button'; reset.textContent = 'Start a new test checkout'; reset.addEventListener('click', () => { sessionStorage.removeItem(pendingKey); location.assign('/cart/'); }); returnRoot.append(reset); }
    const retry = document.createElement('button'); retry.type = 'button'; retry.textContent = 'Refresh confirmation'; retry.addEventListener('click', reconcile); returnRoot.append(retry);
  }
  async function reconcile() {
    const requestId = pending() || sessionStorage.getItem(confirmedKey);
    if (!requestId) { returnRoot.textContent = 'There is no test checkout awaiting confirmation in this browser.'; return; }
    try { const response = await fetch('/__uat/reconcile', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({request_id:requestId})}); if (!response.ok) throw new Error('unavailable'); renderReturn(await response.json()); }
    catch { returnRoot.textContent = 'Confirmation is temporarily unavailable. Refresh this page to retry.'; }
  }
  fetch('/__uat/context', {cache:'no-store'}).then(response => {
    if (!response.ok) throw new Error('not available');
    document.querySelectorAll('[data-uat-add]').forEach(button => { button.disabled = false; button.addEventListener('click', () => {
      if (pending()) { if (status) status.textContent = 'A checkout is in progress. Retry it from your cart.'; return; }
      const id = button.dataset.uatAdd; if (!known.has(id)) return; const cart = readCart(); cart[id] = Math.min(10, (Number(cart[id]) || 0) + 1); saveCart(cart);
      if (status) status.textContent = 'Added to your test cart.'; else button.textContent = 'Added to test cart';
    }); });
    renderCart();
    if (returnRoot) { history.replaceState(null, '', location.pathname); reconcile(); }
  }).catch(() => { if (cartRoot) cartRoot.textContent = 'Test checkout is unavailable. The catalogue remains available to browse.'; if (returnRoot) returnRoot.textContent = 'Test confirmation is unavailable.'; });
}
