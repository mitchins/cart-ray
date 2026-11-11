/** Product media presentation only; catalogue order is authoritative. */
export function galleryImages(product,base='') {
  const primary=product.image_url||product.image;
  const resolve=url=>/^(https?:\/\/|\/)/.test(url)?url:base+url;
  const ordered=Array.isArray(product.images)?product.images:[];
  const candidates=[...(primary?[{url:primary,alt:ordered.find(item=>item?.url===primary)?.alt||product.image_alt||''}]:[]),...ordered];
  const seen=new Set();
  return candidates.filter(item=>item&&typeof item.url==='string'&&item.url.trim()).map(item=>({url:resolve(item.url),alt:typeof item.alt==='string'?item.alt:''})).filter(item=>{if(seen.has(item.url))return false;seen.add(item.url);return true;}).slice(0,8).map((item,index)=>({...item,alt:item.alt||`${product.title} — image ${index+1}`}));
}
export function galleryIndex(index,key,count) {
  if(key==='Home')return 0;
  if(key==='End')return count-1;
  if(key==='ArrowRight'||key==='ArrowDown')return (index+1)%count;
  if(key==='ArrowLeft'||key==='ArrowUp')return (index+count-1)%count;
  return null;
}
export function mountGallery(root,product,base='') {
  if(!root)return;
  const images=galleryImages(product,base),image=root.querySelector('[data-gallery-image]'),fallback=root.querySelector('[data-gallery-fallback]'),controls=root.querySelector('[data-gallery-controls]'),status=root.querySelector('[data-gallery-status]');
  let selected=0;
  const buttons=[];
  const unavailable=()=>{image.hidden=true;fallback.hidden=false;status.textContent=`Image ${selected+1} of ${images.length} unavailable`;};
  image.addEventListener('error',unavailable);
  image.addEventListener('load',()=>{image.hidden=false;fallback.hidden=true;});
  function select(index,focus=false){
    selected=index;image.hidden=false;fallback.hidden=true;image.alt=images[index].alt;image.src=images[index].url;
    buttons.forEach((button,i)=>button.setAttribute('aria-pressed',String(i===index)));
    status.textContent=`Image ${index+1} of ${images.length}: ${images[index].alt}`;
    if(focus)buttons[index]?.focus();
    if(image.complete&&!image.naturalWidth)unavailable();
  }
  if(!images.length){image.hidden=true;fallback.hidden=false;return;}
  controls.replaceChildren();controls.className='gallery-thumbnails';controls.setAttribute('role','group');controls.setAttribute('aria-label','Choose product image');
  images.forEach((item,index)=>{
    const button=document.createElement('button');button.type='button';button.className='gallery-thumbnail';button.setAttribute('aria-label',`Show image ${index+1} of ${images.length}: ${item.alt}`);
    const thumbnail=document.createElement('img');thumbnail.src=item.url;thumbnail.alt='';thumbnail.width=80;thumbnail.height=72;thumbnail.loading='lazy';thumbnail.decoding='async';thumbnail.addEventListener('error',()=>{thumbnail.hidden=true;button.textContent=String(index+1);});button.append(thumbnail);
    button.addEventListener('click',()=>select(index));button.addEventListener('keydown',event=>{const next=galleryIndex(index,event.key,images.length);if(next!==null){event.preventDefault();select(next,true);}});buttons.push(button);controls.append(button);
  });
  controls.hidden=images.length<2;select(0);
}
