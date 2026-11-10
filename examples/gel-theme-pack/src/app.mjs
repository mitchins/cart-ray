/** Shared presentation only. No theme switching, inline merchant CSS or authority derived from browser inputs. */
const config=window.CARTRAY_THEME;
const h=value=>String(value??'').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;').replaceAll("'",'&#39;');
const $=(s,root=document)=>root.querySelector(s);
const $$=(s,root=document)=>[...root.querySelectorAll(s)];
const icon=(name)=>({bag:'<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" aria-hidden="true"><path d="M5 7h14l1 14H4L5 7ZM9 8V6a3 3 0 0 1 6 0v2"/></svg>',check:'<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true"><path d="m5 12 4 4L19 6"/></svg>',lock:'<svg width="19" height="19" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" aria-hidden="true"><rect x="5" y="10" width="14" height="11" rx="2"/><path d="M8 10V7a4 4 0 0 1 8 0v3"/></svg>'}[name]||'');
let adapter;let toastTimer;let selectedProduct;let busy=false;
const base=config?.base;
const url=path=>base+path;
const image=p=>p.image?.startsWith('/')?p.image:base+p.image;
const amount=(value,currency=adapter?.currency||config.gel.identity.currency)=>new Intl.NumberFormat(config.gel.identity.locale,{style:'currency',currency}).format(value/100);
const price=p=>p.amount_minor===0?'Free':amount(p.amount_minor,p.currency);
function announce(text){$('#toast').textContent=text;clearTimeout(toastTimer);toastTimer=setTimeout(()=>{$('#toast').textContent='';},5000);}
function message(text,error=false){const e=$('#message');if(e){e.textContent=text;e.classList.toggle('error',error);}}
function counts(){ $$('[data-cart-count]').forEach(e=>e.textContent=adapter.count()); }
function focusHeading(){const e=$('#content h2');if(e){e.tabIndex=-1;e.focus();}}
function emptyCart(){return `<section class="empty-state"><h2>A little room for something good.</h2><p>Your cart is empty. Take your time with the collection.</p><a class="button" href="${url('shop/')}">Explore the collection →</a></section>`;}
function renderCart(){
  const lines=adapter.getCart(); counts();
  if(!lines.length){$('#content').innerHTML=emptyCart();return;}
  $('#content').innerHTML=`<div class="cart-layout"><div><ul class="cart-lines">${lines.map(p=>`<li class="cart-line"><img src="${h(image(p))}" alt="" width="106" height="104"><div><h2><a href="${url('products/'+p.slug+'/')}">${h(p.title)}</a></h2><p class="muted small">${h(p.category)} · ${price(p)} each</p><div class="quantity-control" role="group" aria-label="Quantity for ${h(p.title)}"><button type="button" data-quantity="${h(p.product_key)}" data-step="-1" aria-label="Decrease quantity of ${h(p.title)}" ${adapter.checkoutLocked?'disabled':''}>−</button><output aria-label="Quantity">${p.quantity}</output><button type="button" data-quantity="${h(p.product_key)}" data-step="1" aria-label="Increase quantity of ${h(p.title)}" ${p.quantity>=p.max_quantity||adapter.checkoutLocked?'disabled':''}>+</button></div><button class="text-button remove" type="button" data-remove="${h(p.product_key)}" ${adapter.checkoutLocked?'disabled':''}>Remove <span class="sr-only">${h(p.title)}</span></button></div><p class="line-price">${amount(p.amount_minor*p.quantity,p.currency)}</p></li>`).join('')}</ul><a class="text-link" href="${url('shop/')}">← Continue shopping</a></div><aside class="order-summary" aria-labelledby="summary-title"><h2 id="summary-title">Order summary</h2><div class="summary-row"><span>Items (${adapter.count()})</span><span>${amount(adapter.subtotal())}</span></div><p class="muted small">Final shipping and any applicable taxes are shown at secure checkout.</p><div class="summary-row total"><span>Subtotal</span><span>${amount(adapter.subtotal())}</span></div>${adapter.permissionEnabled?`<label class="permission" id="review-invitation-choice"><input type="checkbox" id="review-invitation-opt-in" ${adapter.state.permission?'checked':''} ${adapter.checkoutLocked?'disabled':''}><span>Email me one request to review my purchase.<br><span class="muted">Optional. You can unsubscribe at any time.</span></span></label>`:''}<button type="button" class="button wide" id="checkout" ${!adapter.checkoutEnabled||adapter.checkoutLocked?'disabled':''}>${adapter.preview?'Preview secure checkout':'Continue to secure payment'} →</button><p class="payment-note">${icon('lock')} ${adapter.preview?'Simulation only · nothing will be charged':'Payment handled by Stripe'}</p></aside></div>`;
  $('#review-invitation-opt-in')?.addEventListener('change',e=>adapter.setPermission(e.target.checked));
  $('#checkout').addEventListener('click',async()=>{
    if(busy)return; busy=true;$('#checkout').disabled=true;
    try{const next=await adapter.checkout();location.assign(next);}catch{message('Checkout is currently unavailable. Your cart is still here. Please try again.',true);renderCart();}finally{busy=false;}
  });
}
function checkoutPreview(){
  if(!adapter.preview)throw new Error('Preview-only route.');
  $('#content').innerHTML=adapter.count()?`<section class="panel checkout-handoff"><div class="large-icon">${icon('lock')}</div><h2>The store ends here.<br>Stripe takes care of payment.</h2><p>This preview does not collect card details or create a payment. The connected storefront uses CartRay’s existing Stripe redirect.</p><div class="summary-row total"><span>Preview subtotal</span><span>${amount(adapter.subtotal())}</span></div><p class="muted small">Review invitation: ${adapter.state.permission?'selected':'not selected'}. ${adapter.state.suppressed?'This preview recipient is suppressed.':''}</p><div class="button-row"><button type="button" id="simulate-payment">Simulate successful payment</button><a class="button secondary" href="${url('purchase-return/?state=cancelled')}">Simulate cancellation</a></div></section>`:emptyCart();
  $('#simulate-payment')?.addEventListener('click',()=>location.assign(adapter.simulatePayment()));
}
async function renderReturn(result){
  const state=adapter.preview ? new URLSearchParams(location.search).get('state')||'pending':result?.state||'unavailable';
  const states={
    confirmed:['Thank you. Your order is confirmed.','Your purchase has been confirmed. Keep your confirmation email for your records.','check'],
    pending:['Your order is still being confirmed.','You do not need to pay again. You can safely close this page and check again later.','lock'],
    unavailable:['We could not confirm this checkout.','That does not mean your payment failed. Please check again or contact the store before placing another order.','lock'],
    cancelled:['Your cart is ready when you are.','Checkout was cancelled. Your items are still in your cart.','bag'],
    expired:['This checkout has expired.','Return to your cart to start a new checkout when you are ready.','bag'],
  };
  const data=states[state]||states.unavailable;
  $('.flow-heading h1').textContent=state==='confirmed'?'A good little beginning.':'Your order';
  $('#content').innerHTML=`<section class="panel state-panel"><div class="state-icon">${icon(data[2])}</div><h2>${data[0]}</h2><p>${h(!adapter.preview&&result?.message?result.message:data[1])}</p>${adapter.preview?'<p class="tag">Simulated state · not evidence of a real payment</p>':''}${state==='confirmed'&&adapter.preview&&adapter.state.order?`<div class="summary-row"><span>Preview reference</span><span>${h(adapter.state.order.reference)}</span></div><p class="muted">${adapter.state.order.invitation?'Your preview purchase includes one review invitation.':'No review invitation will be sent in this preview.'}</p>`:''}<div class="button-row">${['pending','unavailable'].includes(state)?`<a class="button" href="${h(location.pathname+location.search)}">Check order status again</a>`:`<a class="button" href="${url(state==='confirmed'?'shop/':'cart/')}">${state==='confirmed'?'Continue shopping':'Return to cart'}</a>`}${adapter.preview&&state==='confirmed'?`<a class="button secondary" href="${url('inbox/')}">View sample email</a>`:''}</div></section>`;
}
function renderInbox(){
  if(!adapter.preview)return;
  if(adapter.state.order&&!adapter.state.order.invitation){$('#content').innerHTML=`<section class="panel"><h2>No invitation this time.</h2><p>The review request was declined, the purchase had no reviewable products, or this recipient was suppressed. A receipt is not a separate review invitation.</p><a class="text-link" href="${url('shop/')}">Continue shopping →</a></section>`;return;}
  $('#content').innerHTML=`<article class="panel email-preview"><div class="email-meta"><strong>From:</strong> ${h(config.gel.identity.name)}<br><strong>Subject:</strong> How was your purchase?<br><span class="muted">Illustrative email · nothing was sent</span></div><div class="email-body"><img src="${base+config.gel.assets.logo}" width="42" height="42" alt=""><p class="eyebrow">ONE SMALL QUESTION</p><h2>How did it go?</h2><p>Thank you for your purchase from ${h(config.gel.identity.name)}. A rating or a few honest words can help the next person choose.</p><a class="button" href="${url('review/')}">Review your purchase →</a><p class="small">Your link gives access to the products in your purchase. Anyone with the link can use it.</p><p class="small">${h(config.gel.identity.name)}<br><a href="mailto:${h(config.gel.identity.contact_email)}">${h(config.gel.identity.contact_email)}</a><br><a href="${url('unsubscribe/')}">Stop review-request emails</a></p></div></article>`;
}
function reviewFields(saved={}){
  return `<fieldset class="rating-field"><legend>Your rating <span class="muted">(optional with a comment)</span></legend><div class="rating-options">${[1,2,3,4,5].map(n=>`<label><input type="radio" name="rating" value="${n}" ${Number(saved.rating)===n?'checked':''}><span>${n} <span aria-hidden="true">★</span><span class="sr-only">${n===1?'star':'stars'}</span></span></label>`).join('')}<label class="none"><input type="radio" name="rating" value="" ${!saved.rating?'checked':''}><span>No rating</span></label></div></fieldset><label class="field">Your experience <span class="muted">(optional with a rating)</span><textarea name="body" maxlength="5000" aria-describedby="experience-hint" placeholder="What would you tell someone choosing this?">${h(saved.body||'')}</textarea><small id="experience-hint">A rating, a comment, or both. Up to 5,000 characters.</small></label><label class="field">Review title <span class="muted">(optional)</span><input name="title" maxlength="200" value="${h(saved.title||'')}" placeholder="A few words to sum it up"></label><label class="field">Display name <span class="muted">(optional)</span><input name="author" maxlength="120" value="${h(saved.author||'')}" autocomplete="nickname" aria-describedby="name-hint"><small id="name-hint">This is shown with your review. Leave blank to use Anonymous.</small></label>`;
}
function unavailableReview(state){
  $('#content').innerHTML=`<section class="panel"><h2>${state==='unavailable'?'The review service is taking a moment.':'This invitation is unavailable.'}</h2><p>${state==='unavailable'?'Your draft is kept in this tab. Please try again shortly.':'The link may have expired, been withdrawn or been replaced. Please contact the store for help.'}</p><div class="button-row"><a class="button" href="${url('review/')}">${state==='unavailable'?'Try again':'Back to review preview'}</a><a class="button secondary" href="${url('help/')}">Contact the store</a></div></section>`;
}
function draftKey(key){return `cartray.theme-demo.draft:${config.gel.identity.key}:${key}`;}
function getDraft(key){try{const saved=JSON.parse(sessionStorage.getItem(draftKey(key))||'{}');return saved && typeof saved==='object' && !Array.isArray(saved)?saved:{};}catch{return {};}}
function formValues(form){const f=new FormData(form);return {rating:f.get('rating')?Number(f.get('rating')):null,body:String(f.get('body')||''),title:String(f.get('title')||''),author:String(f.get('author')||'')};}
function rememberDraft(form,key){form.addEventListener('input',()=>{try{sessionStorage.setItem(draftKey(key),JSON.stringify(formValues(form)));}catch{/* optional */}});}
function renderReview(selected){
  const state=new URLSearchParams(location.search).get('state');
  if(state==='unavailable'||state==='expired'||adapter.state.suppressed){unavailableReview(state==='unavailable'?state:'expired');return;}
  const products=adapter.reviewable();
  if(!products.length){$('#content').innerHTML='<section class="panel"><h2>No reviewable products in this purchase.</h2><p>The free guide does not receive a review invitation.</p></section>';return;}
  selectedProduct=products.find(p=>p.product_key===selected)||products[0];
  const p=selectedProduct;const saved=adapter.state.reviews[p.product_key];
  const done=Boolean(saved);const withdrawn=saved?.status==='withdrawn';
  $('#content').innerHTML=`<div class="review-layout"><aside class="review-products"><h2>Your purchased products</h2><div class="review-tabs" role="group" aria-label="Choose a product to review">${products.map(x=>`<button type="button" class="review-tab" data-review-product="${h(x.product_key)}" aria-pressed="${x.product_key===p.product_key}"><img src="${h(image(x))}" alt="" width="48" height="48"><span>${h(x.title)}<small>${adapter.state.reviews[x.product_key]?.status==='pending'?'Submitted · pending':adapter.state.reviews[x.product_key]?.status==='withdrawn'?'Withdrawn':'Available to review'}</small></span></button>`).join('')}</div><p class="muted small">One purchase. A separate review for each eligible product.</p></aside>${done?`<section class="panel review-summary"><p class="success-label">${icon('check')} ${withdrawn?'Review withdrawn':'Submitted · awaiting moderation'}</p><h2 class="spaced-title">${withdrawn?'Your words, your choice.':'Thank you for sharing.'}</h2><p>${withdrawn?'Your review is withdrawn. Withdrawal does not create a new review entitlement.':'Your review of '+h(p.title)+' is awaiting moderation. It is not published yet.'}</p>${withdrawn?'':`<div class="button-row"><a class="button secondary" href="${url('review/manage.html?product='+encodeURIComponent(p.product_key))}">Correct or withdraw this review</a></div>`}<p class="muted small">The other products in your purchase remain available to review.</p></section>`:`<form class="review-form" id="review-form" novalidate><h2>${h(p.title)}</h2>${reviewFields(getDraft(p.product_key))}<div class="button-row"><button type="submit">Submit review →</button></div><p class="privacy-note">Reviews are checked before publication. This preview saves your example only in this browser; it is not sent to the store.</p></form>`}</div>`;
  const form=$('#review-form');
  if(form){rememberDraft(form,p.product_key);form.addEventListener('submit',e=>{
    e.preventDefault();const value=formValues(form);
    if(!value.rating && value.body.trim().length<3){message('Choose a rating or write at least three characters about your experience.',true);$('textarea',form).setAttribute('aria-invalid','true');$('textarea',form).focus();return;}
    adapter.review(p.product_key,value);try{sessionStorage.removeItem(draftKey(p.product_key));}catch{}
    message('Thank you. Your example review is awaiting moderation.');renderReview(p.product_key);focusHeading();
  });}
}
function renderManage(){
  const key=new URLSearchParams(location.search).get('product')||adapter.reviewable()[0]?.product_key;
  const p=adapter.products.find(x=>x.product_key===key);
  if(!p){unavailableReview('expired');return;}
  const saved=adapter.state.reviews[key]||{rating:5,body:'A lovely little beginning. Easy to get started.',title:'A thoughtful growing project',author:'Alex',status:'pending'};
  if(saved.status==='withdrawn'){$('#content').innerHTML='<section class="panel"><p class="success-label">Review withdrawn</p><h2>Your words, your choice.</h2><p>This example review is no longer shown. Your purchase is unchanged.</p></section>';return;}
  $('#content').innerHTML=`<form class="review-form" id="manage-form" novalidate><h2>${h(p.title)}</h2><p class="muted small">Preview review · changes return to moderation</p>${reviewFields(saved)}<div class="button-row"><button type="submit">Save changes</button><button type="button" class="secondary" id="withdraw-review">Withdraw review</button></div><p class="privacy-note">This is a local example. In the connected store, access and updates are checked using your management capability and current review revision.</p></form>`;
  const form=$('#manage-form');form.addEventListener('submit',e=>{
    e.preventDefault();const value=formValues(form);
    if(!value.rating&&value.body.trim().length<3){message('Choose a rating or write at least three characters.',true);$('textarea',form).focus();return;}
    try{adapter.review(key,value);message('Your changes were submitted and are awaiting moderation.');announce('Changes saved in this preview.');}catch(error){message(error.message,true);}
  });
  const dialog=$('#confirm-dialog');let opener;
  $('#withdraw-review').addEventListener('click',e=>{opener=e.currentTarget;dialog.showModal();$('[data-dialog-cancel]',dialog).focus();});
  $('[data-dialog-cancel]',dialog).onclick=()=>dialog.close();
  $('[data-dialog-confirm]',dialog).onclick=()=>{adapter.state.reviews[key]=saved;adapter.withdraw(key);dialog.close();renderManage();focusHeading();};
  dialog.onclose=()=>{if(opener?.isConnected)opener.focus();};
  dialog.onkeydown=e=>{
    if(e.key!=='Tab')return;
    const buttons=$$('button:not([disabled])',dialog);const first=buttons[0],last=buttons.at(-1);
    if(e.shiftKey&&document.activeElement===first){e.preventDefault();last.focus();}
    else if(!e.shiftKey&&document.activeElement===last){e.preventDefault();first.focus();}
  };
}
function renderUnsubscribe(){
  if(!adapter.preview)return;
  $('#content').innerHTML=adapter.state.suppressed?`<section class="panel state-panel"><p class="success-label">${icon('check')} You are unsubscribed</p><h2 class="spaced-title">We will leave it there.</h2><p>Review-request emails from ${h(config.gel.identity.name)} are stopped for this preview recipient, including on another purchase.</p><a class="text-link" href="${url('shop/')}">Back to the collection →</a></section>`:`<section class="panel state-panel"><h2>Stop review-request emails?</h2><p>Choose Unsubscribe to stop future review requests from ${h(config.gel.identity.name)}. Order and payment messages are not affected.</p><div class="button-row"><button type="button" id="unsubscribe-confirm">Unsubscribe</button><a class="button secondary" href="${url('shop/')}">Keep browsing</a></div><p class="muted small">Opening this page does not unsubscribe you. This is an interactive preview.</p></section>`;
  $('#unsubscribe-confirm')?.addEventListener('click',()=>{adapter.unsubscribe();renderUnsubscribe();focusHeading();});
}
function cardMarkup(p){return `<article class="product-card" data-key="${h(p.product_key)}" data-category="${h(p.category)}"><a class="product-art" href="${url('products/'+p.slug+'/')}" tabindex="-1" aria-hidden="true"><img src="${h(image(p))}" alt="" width="800" height="720" loading="lazy"></a><div class="product-copy"><p class="eyebrow">${h(p.eyebrow||p.category)}</p><h3><a href="${url('products/'+p.slug+'/')}">${h(p.title)}</a></h3><p class="description">${h(p.short_description||'')}</p><div class="product-bottom"><span class="price">${price(p)}<small>${p.amount_minor?h(p.currency):'digital guide'}</small></span><button type="button" class="add-small" data-add="${h(p.product_key)}" ${adapter.checkoutLocked?'disabled':''} aria-label="Add ${h(p.title)} to cart">Add to cart <span aria-hidden="true">+</span></button></div></div></article>`;}
function catalogueControls(){
  if(!$('#products'))return;
  let category='all';let query=(new URLSearchParams(location.search).get('q')||'').slice(0,100);
  if($('#site-search'))$('#site-search').value=query;
  function render(){
    let products=adapter.products.filter(p=>(category==='all'||p.category===category)&&`${p.title} ${p.short_description} ${p.category}`.toLowerCase().includes(query.toLowerCase()));
    const sort=$('#sort')?.value;
    if(sort==='low')products.sort((a,b)=>a.amount_minor-b.amount_minor);if(sort==='high')products.sort((a,b)=>b.amount_minor-a.amount_minor);if(sort==='name')products.sort((a,b)=>a.title.localeCompare(b.title));
    $('#products').innerHTML=products.map(cardMarkup).join('');$('#results-status').textContent=`${products.length} product${products.length===1?'':'s'}${query?' matching “'+query+'”':''}`;
    $('#no-results').hidden=products.length>0;
  }
  $$('[data-category-filter]').forEach(e=>e.addEventListener('click',()=>{category=e.dataset.categoryFilter;$$('[data-category-filter]').forEach(b=>b.setAttribute('aria-pressed',String(b===e)));render();}));
  $('#sort')?.addEventListener('change',render);
  $('.search-form')?.addEventListener('submit',e=>{if(document.body.dataset.page==='shop'){e.preventDefault();query=$('#site-search').value.trim().slice(0,100);render();}});
  $('#reset-filter')?.addEventListener('click',()=>{category='all';query='';if($('#site-search'))$('#site-search').value='';$$('[data-category-filter]').forEach(e=>e.setAttribute('aria-pressed',String(e.dataset.categoryFilter==='all')));render();});
  render();
}
async function nativeReview(page){
  const {presentNativeReviews}=await import('./native-presenter.mjs');
  presentNativeReviews(page);
  $('#content').replaceChildren();
  message(page==='manage'?'Loading your review…':'Loading your purchased products…');
  const s=document.createElement('script');s.src=base+'theme/'+(page==='manage'?'native-manage.js':'native-reviews.js');
  s.onerror=()=>message('The review service is temporarily unavailable. Please try again.',true);
  document.body.append(s);
}
document.addEventListener('keydown',e=>{if(e.key==='Escape')$$('.mobile-nav[open]').forEach(d=>{d.open=false;$('summary',d).focus();});});
async function boot(){
  if(!config)throw new Error('Missing theme build configuration.');
  if(document.body.dataset.pageTitle)document.title=document.body.dataset.pageTitle;
  let page=document.body.dataset.page;
  // Review capabilities use the existing standalone client; a catalogue outage must not block them.
  if(config.preview===false && (page==='review'||page==='manage')){await nativeReview(page);return;}
  const module=await import(config.adapter);adapter=await module.createAdapter(config);counts();
  if(!adapter.preview && new URLSearchParams(location.search).get('checkout')){
    $('#main').innerHTML='<section class="flow-page wrap"><header class="flow-heading"><p class="eyebrow">YOUR PURCHASE</p><h1>Checking your order…</h1></header><p id="message" class="message" role="status" aria-live="polite"></p><div id="content"></div></section>';
    await renderReturn(await adapter.returnState());counts();return;
  }
  if(page==='home'||page==='shop')catalogueControls();
  if(page==='product'&&!adapter.preview){
    const p=adapter.products.find(x=>x.product_key===document.body.dataset.productKey);
    if(p){$('.detail-price').textContent=price(p);$('.detail-copy h1').textContent=p.title;$$('[data-add]').forEach(b=>b.disabled=adapter.checkoutLocked);}
    else{$$('[data-add]').forEach(b=>b.disabled=true);announce('This product is currently unavailable.');}
    if(window.CARTRAY_STOREFRONT?.reviewsEnabled){const s=document.createElement('script');s.type='module';s.src=base+'reviews.mjs';document.body.append(s);}
  }
  if(page==='cart')renderCart();
  if(page==='checkout-preview')checkoutPreview();
  if(page==='purchase-return')await renderReturn(await adapter.returnState());
  if(page==='review'){if(adapter.preview)renderReview();else await nativeReview('review');}
  if(page==='manage'){if(adapter.preview)renderManage();else await nativeReview('manage');}
  if(page==='inbox')renderInbox();
  if(page==='unsubscribe')renderUnsubscribe();
  document.addEventListener('click',e=>{
    const add=e.target.closest('[data-add]');if(add){
      const key=add.dataset.add;const p=adapter.products.find(x=>x.product_key===key);const before=adapter.count();
      if(adapter.add(key)){counts();announce(adapter.count()>before?`${p.title} added to your cart.`:'You have reached the maximum quantity.');}
    }
    const qty=e.target.closest('[data-quantity]');if(qty){const key=qty.dataset.quantity;const line=adapter.getCart().find(x=>x.product_key===key);if(line){const step=Number(qty.dataset.step);adapter.quantity(key,line.quantity+step);renderCart();const next=$$('[data-quantity]').find(x=>x.dataset.quantity===key&&Number(x.dataset.step)===step);(next||$('#checkout')||$('#content a'))?.focus();announce('Cart updated.');}}
    const remove=e.target.closest('[data-remove]');if(remove){adapter.quantity(remove.dataset.remove,0);renderCart();($('#checkout')||$('#content a'))?.focus();announce('Item removed from your cart.');}
    const review=e.target.closest('[data-review-product]');if(review){renderReview(review.dataset.reviewProduct);const chosen=$$('[data-review-product]').find(x=>x.dataset.reviewProduct===review.dataset.reviewProduct);chosen?.focus();}
    if(e.target.closest('[data-reset-demo]')&&adapter.preview){adapter.reset();try{for(const key of Object.keys(sessionStorage))if(key.startsWith(`cartray.theme-demo.draft:${config.gel.identity.key}:`))sessionStorage.removeItem(key);}catch{}location.assign(base);}
  });
}
boot().catch(()=>{
  const content=$('#content');if(content)content.innerHTML='<section class="panel"><h2>The store is temporarily unavailable.</h2><p>Please reload or contact the store. No payment has been started by this page.</p></section>';
  $$('[data-add],#checkout').forEach(b=>b.disabled=true);announce('The store is currently unavailable. Please try again.');
});
