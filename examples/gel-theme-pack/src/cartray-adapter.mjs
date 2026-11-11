/** Connected presenter. Delegates payment/persistence/return decisions to the existing CartRay client. */
import {
  loadCatalogue, loadPersistedCart, persistCart, addToCart,
  createCheckoutAndStorePending, processCheckoutReturn, pollCheckoutStatus,
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
  const local=new Map(config.catalogue.products.map(p=>[p.product_key,p]));
  // Theme content contributes presentation only, never a price, product identity or review entitlement.
  const products=catalogue.products.map(p=>{
    const presentation=local.get(p.product_key) || {};
    return {...presentation,...p,images:Array.isArray(p.images)?p.images:[],image:p.image_url || presentation.image,image_alt:p.image_alt || presentation.image_alt || '',
      slug:presentation.slug,category:presentation.category || 'Products',
      review_subject:source.reviewSubjects?.[p.product_key] || null};
  });
  let returnResult=null;
  const params=new URLSearchParams(location.search);
  const adapter={
    preview:false, products, currency:[...currencies][0], state:{permission:source.reviewInvitationDefaultChecked===true},
    permissionEnabled:source.reviewInvitationsEnabled===true,
    checkoutEnabled:source.checkoutEnabled===true,
    checkoutLocked:params.get('checkout')==='complete',
    getCart(){return products.filter(p=>cart.has(p.product_key)).map(p=>({...p,quantity:cart.get(p.product_key)}));},
    count(){return this.getCart().reduce((n,p)=>n+p.quantity,0);},
    subtotal(){return this.getCart().reduce((n,p)=>n+p.quantity*p.amount_minor,0);},
    quantity(key,q){
      if(this.checkoutLocked)return false;
      const p=products.find(x=>x.product_key===key);
      if(!p || !Number.isSafeInteger(q) || q<0)return false;
      if(q===0)cart.delete(key);else cart.set(key,Math.min(q,p.max_quantity));
      revision++;persistCart(storage,catalogue.version,cart,revision);return true;
    },
    add(key){
      const p=products.find(x=>x.product_key===key); if(!p || this.checkoutLocked)return false;
      addToCart(cart,p);revision++;persistCart(storage,catalogue.version,cart,revision);return true;
    },
    setPermission(value){this.state.permission=value===true;},
    async checkout(){
      if(this.checkoutLocked || !this.checkoutEnabled || !this.count())throw new Error('Checkout is not available.');
      this.checkoutLocked=true;
      try{
        const result=await createCheckoutAndStorePending({configuration:source,catalogueVersion:catalogue.version,cart,revision,
          requestId:`checkout_${crypto.randomUUID().replaceAll('-','')}`,storage,
          reviewInvitationOptIn:this.permissionEnabled?this.state.permission:undefined});
        return result.url;
      }catch(error){this.checkoutLocked=false;throw error;}
    },
    async returnState(){
      if(returnResult)return returnResult;
      if(params.get('checkout')==='cancelled'){
        this.checkoutLocked=false;history.replaceState(null,'',location.pathname);
        returnResult={state:'cancelled',...cancelledCheckoutView()};return returnResult;
      }
      if(params.get('checkout')!=='complete')return null;
      const result=await processCheckoutReturn({configuration:source,sessionId:params.get('session_id'),storage,catalogue,cart,revision,poll:pollCheckoutStatus});
      const view=result.state==='confirmed'?confirmedCheckoutView(result.cleared):result.state==='expired'?expiredCheckoutView():unconfirmedCheckoutView(result.state);
      this.checkoutLocked=view.checkoutLocked;
      if(result.cleared)revision=0;
      if(result.state==='confirmed'||result.state==='expired')history.replaceState(null,'',location.pathname);
      returnResult={state:result.state||'unavailable',...view};return returnResult;
    },
  };
  return adapter;
}
