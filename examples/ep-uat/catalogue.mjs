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
    const originalPrice=document.querySelector('#variant-price').textContent;
    function setVariant(){
      const selected=product.variations.find(v=>v.key===select.value);
      const offline=document.body.dataset.offlineParity==='true';
      document.querySelector('#variant-status').textContent=selected?'Selected option: '+Object.entries(selected.attributes).map(([name,value])=>name.replace(/^attribute_/,'').replaceAll('_',' ')+': '+value).join(', ')+(offline?'.':'. Checkout unavailable.'):'';
      document.querySelector('#variant-price').textContent=!selected?originalPrice:selected.source_price?(offline?'':'Source price: ')+selected.source_price+' '+product.currency+(offline?'':' (not checkout-authorised)'):'Price unavailable';
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

