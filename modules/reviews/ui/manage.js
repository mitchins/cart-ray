(() => {
  "use strict";
  const fragment = new URLSearchParams(location.hash.slice(1));
  const review = fragment.get("review"), token = fragment.get("token");
  const message = document.querySelector("#message"), content = document.querySelector("#content");
  const element = (tag, text) => { const node = document.createElement(tag); if (text !== undefined) node.textContent = text; return node; };
  async function post(action, data, key) {
    const headers = {"Content-Type": "application/json"};
    if (key) headers["Idempotency-Key"] = key;
    return fetch(`/reviews/v1/manage/${review}/${action}`, {method:"POST", headers, body:JSON.stringify({management_token:token,...data}), cache:"no-store", referrerPolicy:"no-referrer"});
  }
  async function load() {
    if (!/^[a-f0-9]{64}$/.test(review || "") || !/^[a-f0-9]{64}$/.test(token || "")) {
      message.textContent = "This management link is unavailable. Please contact the store for help."; return;
    }
    let response;
    try { response = await post("read", {}); } catch (_) { message.textContent = "We couldn't reach the store. Please try again."; return; }
    if (!response.ok) {
      message.textContent = response.status === 429
        ? "Too many attempts. Please wait a little and try again."
        : response.status === 503
          ? "The review service is temporarily unavailable. Please try again shortly."
          : "This management link is unavailable. Your review may already be withdrawn.";
      return;
    }
    const current = await response.json();
    message.textContent = "Changes are checked before they appear publicly. You can also withdraw this review.";
    const form = element("form");
    function field(labelText, name, tag, value) {
      const label = element("label", labelText), input = element(tag); input.name=name;
      if (tag !== "select") input.value=value || ""; label.append(input); form.append(label); return input;
    }
    const rating = field("Rating (optional if you write a comment)", "rating", "select");
    for (const [value,label] of [["","Select a rating"],["5","5 stars"],["4","4 stars"],["3","3 stars"],["2","2 stars"],["1","1 star"]]) {
      const option=element("option",label); option.value=value; rating.append(option);
    }
    rating.value = current.rating == null ? "" : String(current.rating);
    const body=field("Your experience", "body", "textarea", current.body); body.maxLength=5000;
    const title=field("Title (optional)", "title", "input", current.title); title.maxLength=200;
    const author=field("Display name (optional)", "author", "input", current.author === "Anonymous" ? "" : current.author); author.maxLength=120;
    const submit=element("button","Save changes"); submit.type="submit";
    const withdraw=element("button","Withdraw review"); withdraw.type="button";
    const confirm=element("button","Confirm withdrawal"); confirm.type="button"; confirm.hidden=true;
    form.append(submit, withdraw, confirm); content.replaceChildren(form);
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
      for (const input of [...fields,submit,withdraw,confirm]) input.disabled=true;
      try {
        const result=await post(action,payload,key);
        if (!result.ok) { message.textContent="We couldn't complete that change. Your draft is still here; please try again or contact the store."; return; }
        try { sessionStorage.removeItem(draftKey); } catch (_) { /* optional */ }
        if (action === "withdraw") { content.replaceChildren(); message.textContent="Your review has been withdrawn."; }
        else { content.replaceChildren(); message.textContent="Your changes were submitted and are awaiting moderation."; }
      } catch (_) { message.textContent="We couldn't reach the store. Your draft is still here; please try again."; }
      finally { for (const input of [...fields,submit,withdraw,confirm]) input.disabled=false; }
    }
    form.addEventListener("submit", event=>{event.preventDefault();void change("edit");});
    withdraw.addEventListener("click",()=>{confirm.hidden=false;message.textContent="Select Confirm withdrawal to remove this review.";});
    confirm.addEventListener("click",()=>{void change("withdraw");});
  }
  void load();
})();
