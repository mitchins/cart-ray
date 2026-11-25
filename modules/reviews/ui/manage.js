(() => {
  "use strict";
  const fragment = new URLSearchParams(location.hash.slice(1));
  const review = fragment.get("review"), token = fragment.get("token");
  const message = document.querySelector("#message"), content = document.querySelector("#content");
  const element = (tag, text) => { const node = document.createElement(tag); if (text !== undefined) node.textContent = text; return node; };
  let loadVersion = 0;
  const contactPath = globalThis.CARTRAY_REVIEWS_UI?.contactPath;
  function settle(state, text, retry = false) {
    message.textContent = text; message.classList.add('cr-feedback');
    content.dataset.crState = state; content.setAttribute('aria-busy', 'false'); content.replaceChildren();
    const actions = element('div'); actions.className = 'cr-actions';
    if (retry) {
      const button = element('button', 'Try again'); button.type = 'button';
      button.className = 'cr-button cr-button--secondary'; button.dataset.crAction='retry';
      button.addEventListener('click', () => { void load(); }); actions.append(button);
    }
    if (typeof contactPath === 'string' && /^\/(?:[A-Za-z0-9_-]+\/)*$/.test(contactPath)) {
      const contact = element('a', 'Contact the store'); contact.href = contactPath;
      contact.className = 'cr-button cr-button--secondary'; actions.append(contact);
    }
    content.append(actions);
  }
  function loading() {
    message.textContent='Loading your review…';
    for (const button of content.querySelectorAll('[data-cr-action=retry]')) button.disabled=true;
    content.dataset.crState = 'loading'; content.setAttribute('aria-busy', 'true');
    message.classList.add('cr-feedback');
  }
  async function post(action, data, key) {
    const headers = {"Content-Type": "application/json"};
    if (key) headers["Idempotency-Key"] = key;
    return fetch(`/reviews/v1/manage/${review}/${action}`, {method:"POST", headers, body:JSON.stringify({management_token:token,...data}), cache:"no-store", referrerPolicy:"no-referrer", signal:AbortSignal.timeout(10000)});
  }
  async function load() {
    const version=++loadVersion; loading();
    if (!/^[a-f0-9]{64}$/.test(review || "") || !/^[a-f0-9]{64}$/.test(token || "")) {
      settle("invalid", "This management link is unavailable. Please contact the store for help."); return;
    }
    let response;
    try { response = await post("read", {}); } catch (_) { if(version!==loadVersion)return; settle("unavailable", "We couldn't reach the store. Please try again.", true); return; }
    if(version!==loadVersion)return;
    if (!response.ok) {
      const failure = response.status === 429
        ? "Too many attempts. Please wait a little and try again."
        : response.status === 503
          ? "The review service is temporarily unavailable. Please try again shortly."
          : "This management link is unavailable. Your review may already be withdrawn.";
      settle("unavailable", failure, response.status === 429 || response.status === 503); return;
    }
    let current;
    try { current = await response.json(); if(version!==loadVersion)return; if (!current || (current.body !== null && typeof current.body !== "string")) throw new Error(); }
    catch (_) { if(version!==loadVersion)return; settle("unavailable", "We couldn't load this review. Please try again.", true); return; }
    content.dataset.crState="ready"; content.setAttribute("aria-busy","false");
    message.textContent = "Changes are checked before they appear publicly. You can also withdraw this review.";
    const form = element("form"); form.className = "cr-form";
    function field(labelText, name, tag, value) {
      const label = element("label", labelText), input = element(tag); label.className="cr-field"; input.name=name;
      if (tag !== "select") input.value=value || ""; label.append(input); form.append(label); return input;
    }
    const rating = field("Rating (optional if you write a comment)", "rating", "select");
    for (const [value,label] of [["","Select a rating"],["5","5 stars"],["4","4 stars"],["3","3 stars"],["2","2 stars"],["1","1 star"]]) {
      const option=element("option",label); option.value=value; rating.append(option);
    }
    rating.classList.add("cr-rating");
    rating.value = current.rating == null ? "" : String(current.rating);
    const body=field("Your experience", "body", "textarea", current.body); body.maxLength=5000;
    const title=field("Title (optional)", "title", "input", current.title); title.maxLength=200;
    const author=field("Display name (optional)", "author", "input", current.author === "Anonymous" ? "" : current.author); author.maxLength=120;
    const submit=element("button","Save changes"); submit.type="submit";
    const withdraw=element("button","Withdraw review"); withdraw.type="button";
    const confirm=element("button","Confirm withdrawal"); confirm.type="button"; confirm.hidden=true;
    submit.className="cr-button cr-button--primary"; submit.dataset.crAction="edit";
    withdraw.className="cr-button cr-button--danger"; withdraw.dataset.crAction="withdraw";
    confirm.className="cr-button cr-button--danger"; confirm.dataset.crAction="confirm-withdraw";
    const actions=element("div"); actions.className="cr-actions"; actions.append(submit,withdraw,confirm);
    form.append(actions); content.replaceChildren(form);
    const fields=[rating,body,title,author], draftKey=`cartray-management-draft:${review}`;
    let saved={}; try { saved=JSON.parse(sessionStorage.getItem(draftKey)||"{}"); } catch (_) { /* optional */ }
    if (saved.revision===current.revision_id && saved.values) for (const input of fields) input.value=saved.values[input.name] || "";
    const values=()=>Object.fromEntries(fields.map(input=>[input.name,input.value]));
    const persist=()=>{ try { sessionStorage.setItem(draftKey, JSON.stringify(saved)); } catch (_) { /* optional */ } };
    form.addEventListener("input",()=>{saved={...saved,revision:current.revision_id,values:values()};persist();});
    async function change(action) {
      const value=values();
      if (action === "edit" && !value.rating && value.body.trim().length<3) { message.textContent="Choose a rating or write a little about your experience."; return; }
      const update={author:value.author,title:value.title,body:value.body,rating:value.rating ? Number(value.rating):null};
      const payload={revision_id:current.revision_id,...(action === "edit" ? {update}: {})};
      const fingerprint=JSON.stringify([action,payload]);
      const key=saved.fingerprint === fingerprint ? saved.requestKey : crypto.randomUUID();
      saved={...saved,revision:current.revision_id,values:value,fingerprint,requestKey:key}; persist();
      content.dataset.crState="submitting";content.setAttribute("aria-busy","true");
      for (const input of [...fields,submit,withdraw,confirm]) input.disabled=true;
      try {
        const result=await post(action,payload,key);
        if (!result.ok) { message.textContent="We couldn't complete that change. Your draft is still here; please try again or contact the store."; return; }
        try { sessionStorage.removeItem(draftKey); } catch (_) { /* optional */ }
        document.dispatchEvent(new CustomEvent("cartray:reviews-changed",{detail:{subjectId:current.subject_id || null}}));
        content.dataset.crState=action === "withdraw" ? "withdrawn" : "submitted";
        if (action === "withdraw") { content.replaceChildren(); message.textContent="Your review has been withdrawn."; }
        else { content.replaceChildren(); message.textContent="Your changes were submitted and are awaiting moderation."; }
        // Removing a long form must not strand keyboard focus or the viewport in a merchant footer.
        message.tabIndex=-1;
        message.focus({preventScroll:true});
        message.scrollIntoView({block:"center",behavior:"instant"});
      } catch (_) { message.textContent="We couldn't reach the store. Your draft is still here; please try again."; }
      finally { content.setAttribute("aria-busy","false");
        if(content.dataset.crState==="submitting")content.dataset.crState="error"; for (const input of [...fields,submit,withdraw,confirm]) input.disabled=false; }
    }
    form.addEventListener("submit", event=>{event.preventDefault();void change("edit");});
    withdraw.addEventListener("click",()=>{confirm.hidden=false;message.textContent="Select Confirm withdrawal to remove this review.";});
    confirm.addEventListener("click",()=>{void change("withdraw");});
  }
  void load();
})();
