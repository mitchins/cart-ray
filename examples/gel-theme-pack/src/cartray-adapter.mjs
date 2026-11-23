/** Connected presenter. Delegates payment/persistence/return decisions to the existing CartRay client. */
import {
  loadCatalogue, loadPersistedCart, persistCart, addToCart, cartSnapshot,
  createCheckoutAndStorePending, processCheckoutReturn, pollCheckoutDetails,
  confirmedCheckoutView, expiredCheckoutView, unconfirmedCheckoutView, cancelledCheckoutView,
} from './core-storefront.js';

export async function createAdapter(config) {
  if (config.preview !== false) throw new Error('Connected adapter requires a connected build.');
  const source=window.CARTRAY_STOREFRONT;
  if (!source) throw new Error('CartRay storefront configuration is missing.');
  const catalogue=await loadCatalogue(source);
  const currencies=new Set(catalogue.products.map(product=>product.currency));
  if(currencies.size!==1)throw new Error('The catalogue must use one checkout currency.');
  let storage; try {storage=window.localStorage;} catch {storage=null;}
  const restored=loadPersistedCart(storage,catalogue);
  const cart=restored.cart; let revision=restored.revision;
  const attemptKey='cartray.checkout-attempt.v1';
  let attempt=null;
  try { attempt=JSON.parse(storage?.getItem(attemptKey)||'null'); } catch { attempt=null; }
  const validAttempt = value => value && typeof value.requestId==='string' && /^checkout_[a-f0-9]{32}$/.test(value.requestId)
    && typeof value.catalogueVersion==='string' && Number.isSafeInteger(value.revision) && Array.isArray(value.items)
    && typeof value.permission==='boolean';
  if (!validAttempt(attempt) || attempt.catalogueVersion!==catalogue.version) {
    attempt=null;
    try {storage?.removeItem(attemptKey);} catch { /* storage is optional */ }
  }
  const local=new Map(config.catalogue.products.map(p=>[p.product_key,p]));
  // Theme content contributes presentation only, never a price, product identity or review entitlement.
  const products=catalogue.products.map(p=>{
    const presentation=local.get(p.product_key) || {};
    return {...presentation,...p,images:Array.isArray(p.images)?p.images:[],image:p.image_url || presentation.image,image_alt:p.image_alt || presentation.image_alt || '',
      slug:presentation.slug,path:source.productPaths?.[p.product_key],category:presentation.category || 'Products',
      review_subject:source.reviewSubjects?.[p.product_key] || null};
  });
  let returnResult=null;
  const params=new URLSearchParams(location.search);
  const adapter={
    preview:false, products, currency:[...currencies][0], state:{permission:attempt ? attempt.permission : source.reviewInvitationDefaultChecked===true},
    permissionEnabled:source.reviewInvitationsEnabled===true,
    permissionWording:source.reviewInvitationWording,
    checkoutEnabled:source.checkoutEnabled===true,
    checkoutLocked:params.get('checkout')==='complete',
    attemptPending:Boolean(attempt),
    getCart(){return products.filter(p=>cart.has(p.product_key)).map(p=>({...p,quantity:cart.get(p.product_key)}));},
    count(){return this.getCart().reduce((n,p)=>n+p.quantity,0);},
    subtotal(){return this.getCart().reduce((n,p)=>n+p.quantity*p.amount_minor,0);},
    quantity(key,q){
      if(this.checkoutLocked || this.attemptPending)return false;
      const p=products.find(x=>x.product_key===key);
      if(!p || !Number.isSafeInteger(q) || q<0)return false;
      if(q===0)cart.delete(key);else cart.set(key,Math.min(q,p.max_quantity));
      revision++;persistCart(storage,catalogue.version,cart,revision);return true;
    },
    add(key){
      const p=products.find(x=>x.product_key===key); if(!p || this.checkoutLocked || this.attemptPending)return false;
      addToCart(cart,p);revision++;persistCart(storage,catalogue.version,cart,revision);return true;
    },
    setPermission(value){if(!this.attemptPending)this.state.permission=value===true;},
    async checkout(){
      if(this.checkoutLocked || !this.checkoutEnabled || !this.count())throw new Error('Checkout is not available.');
      const snapshot=cartSnapshot(cart);
      const permission=this.permissionEnabled?this.state.permission:false;
      if(attempt && (attempt.catalogueVersion!==catalogue.version || attempt.revision!==revision ||
          JSON.stringify(attempt.items)!==JSON.stringify(snapshot) || attempt.permission!==permission))
        throw new Error('A previous checkout may have started. Contact the store before starting another.');
      if(!attempt){
        attempt={requestId:`checkout_${crypto.randomUUID().replaceAll('-','')}`,catalogueVersion:catalogue.version,
          revision,items:snapshot,permission};
        try {storage?.setItem(attemptKey,JSON.stringify(attempt));} catch { /* retain in memory */ }
      }
      this.attemptPending=true;this.checkoutLocked=true;
      try{
        const result=await createCheckoutAndStorePending({configuration:source,catalogueVersion:catalogue.version,cart,revision,
          requestId:attempt.requestId,storage,
          reviewInvitationOptIn:this.permissionEnabled?attempt.permission:undefined});
        try {storage?.removeItem(attemptKey);} catch { /* return may still be checked */ }
        attempt=null;this.attemptPending=false;
        return result.url;
      }catch(error){
        // A 400 is a definitive validation rejection. Network failures, 409
        // in-progress responses and server errors retain the idempotency key.
        if(error.checkoutStatus===400){
          attempt=null;this.attemptPending=false;
          try {storage?.removeItem(attemptKey);} catch { /* storage is optional */ }
        }
        this.checkoutLocked=false;throw error;
      }
    },
    async returnState(){
      if(returnResult)return returnResult;
      if(params.get('checkout')==='cancelled'){
        this.checkoutLocked=false;history.replaceState(null,'',location.pathname);
        returnResult={state:'cancelled',...cancelledCheckoutView()};return returnResult;
      }
      if(params.get('checkout')!=='complete')return null;
      const result=await processCheckoutReturn({configuration:source,sessionId:params.get('session_id'),storage,catalogue,cart,revision,poll:pollCheckoutDetails});
      const view=result.state==='confirmed'?confirmedCheckoutView(result.cleared,result.receipt,products):result.state==='expired'?expiredCheckoutView():unconfirmedCheckoutView(result.state);
      this.checkoutLocked=view.checkoutLocked;
      if(result.cleared)revision=0;
      if(result.state==='confirmed'||result.state==='expired')history.replaceState(null,'',location.pathname);
      returnResult={state:result.state||'unavailable',...view};return returnResult;
    },
  };
  return adapter;
}
