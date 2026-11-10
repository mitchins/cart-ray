/** DOM-only presentation for native review clients. Requests, drafts and retry keys stay native. */
export function presentNativeReviews(page, doc=document) {
  const content=doc.querySelector('#content');
  let selected=0;
  const observer=new MutationObserver(update);
  function update() {
    observer.disconnect();
    const forms=[...content.querySelectorAll(':scope > form')];
    for(const form of forms) {
      form.classList.add('review-form');
      for(const label of form.querySelectorAll(':scope > label'))label.classList.add('field');
      const select=form.querySelector('select[name="rating"]');
      if(select && !select.dataset.starPresenter) {
        select.dataset.starPresenter='true';
        // The hidden select remains the native client's source of truth, including
        // restored drafts and its in-flight disabled state.
        select.parentElement.hidden=true;
        const fieldset=doc.createElement('fieldset');fieldset.className='rating-field';
        const legend=doc.createElement('legend');legend.textContent='Your rating (optional with a comment)';fieldset.append(legend);
        const options=doc.createElement('div');options.className='rating-options';fieldset.append(options);
        for(const value of ['1','2','3','4','5','']) {
          const label=doc.createElement('label');if(!value)label.className='none';
          const radio=doc.createElement('input');radio.type='radio';radio.name='rating-stars';radio.value=value;
          radio.setAttribute('aria-label',value?`${value} ${value==='1'?'star':'stars'}`:'No rating');
          const text=doc.createElement('span');text.textContent=value?`${value} ★`:'No rating';text.setAttribute('aria-hidden','true');
          radio.addEventListener('input',()=>{select.value=value;select.dispatchEvent(new Event('input',{bubbles:true}));});
          label.append(radio,text);options.append(label);
        }
        select.parentElement.before(fieldset);
      }
      if(select)for(const radio of form.querySelectorAll('[name="rating-stars"]')) {
        radio.checked=radio.value===select.value;radio.disabled=select.disabled;
      }
    }
    if(page==='manage')for(const form of forms) {
      const buttons=[...form.querySelectorAll(':scope > button')];
      const withdraw=buttons.find(button=>button.textContent==='Withdraw review');
      const confirm=buttons.find(button=>button.textContent==='Confirm withdrawal');
      const dialog=doc.querySelector('#confirm-dialog');
      if(withdraw && confirm && dialog && !withdraw.dataset.dialogPresenter) {
        withdraw.dataset.dialogPresenter='true';
        withdraw.addEventListener('click',()=>{
          confirm.hidden=true;dialog.showModal();dialog.querySelector('[data-dialog-cancel]').focus();
        });
        dialog.querySelector('[data-dialog-cancel]').onclick=()=>dialog.close();
        dialog.querySelector('[data-dialog-confirm]').onclick=()=>{dialog.close();confirm.click();};
        dialog.onclose=()=>{if(withdraw.isConnected)withdraw.focus();};
      }
    }
    if(page==='review' && forms.length>1) {
      let chooser=content.querySelector('[data-product-chooser]');
      if(!chooser) {
        chooser=doc.createElement('div');chooser.dataset.productChooser='';chooser.className='review-tabs';
        chooser.setAttribute('role','group');chooser.setAttribute('aria-label','Choose a purchased product to review');
        content.querySelector('p')?.after(chooser);
        forms.forEach((form,index)=>{
          const button=doc.createElement('button');button.type='button';button.className='review-tab';
          button.textContent=form.querySelector('h2')?.textContent||`Product ${index+1}`;
          button.addEventListener('click',()=>{selected=index;update();});chooser.append(button);
        });
      }
      selected=Math.min(selected,forms.length-1);
      forms.forEach((form,index)=>{form.hidden=index!==selected;});
      [...chooser.children].forEach((button,index)=>button.setAttribute('aria-pressed',String(index===selected)));
    }
    observer.observe(content,{childList:true,subtree:true,attributes:true,attributeFilter:['disabled']});
  }
  update();
  return ()=>observer.disconnect();
}
