/* Same-origin standalone invitation form. The capability stays in the fragment. */
(() => {
  "use strict";
  const token = new URLSearchParams(location.hash.slice(1)).get("invitation");
  const featured = (new URLSearchParams(location.hash.slice(1)).get("featured") || "").split(",");
  const message = document.querySelector("#message");
  const content = document.querySelector("#content");
  content.setAttribute("aria-label", "Your review choices");
  const unavailable = "This invitation is unavailable. It may have expired or already been replaced. Please contact the store for help.";
  const element = (tag, text) => {
    const node = document.createElement(tag);
    if (text !== undefined) node.textContent = text;
    return node;
  };
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
    message.textContent='Loading your invitation…';
    for (const button of content.querySelectorAll('[data-cr-action=retry]')) button.disabled=true;
    content.dataset.crState = 'loading'; content.setAttribute('aria-busy', 'true');
    message.classList.add('cr-feedback');
  }
  async function post(path, data, key) {
    const headers = {"Content-Type": "application/json"};
    if (key) headers["Idempotency-Key"] = key;
    return fetch(path, {method: "POST", headers, body: JSON.stringify(data), cache: "no-store", referrerPolicy: "no-referrer", signal:AbortSignal.timeout(10000)});
  }
  function formFor(subject, draftScope) {
    const form = element("form"); form.className = "cr-form";
    const legend = element("h2", subject.public_name);
    form.dataset.crSubject=subject.subject_id; form.append(legend);
    if (subject.progress !== "available") {
      form.append(element("p", "Thank you. Your review has been submitted."));
      if (subject.review_id && subject.management_token) {
        const link = element("a", "Correct or withdraw this review");
        link.className="cr-button cr-button--secondary";
        link.href = `./manage.html#review=${encodeURIComponent(subject.review_id)}&token=${encodeURIComponent(subject.management_token)}`;
        form.append(link);
      }
      return form;
    }
    const draftKey = `cartray-review-draft:${draftScope}:${subject.subject_id}`;
    let saved = {};
    try { saved = JSON.parse(sessionStorage.getItem(draftKey) || "{}"); } catch (_) { /* storage is optional */ }
    const add = (labelText, name, tag = "input") => {
      const label = element("label", labelText); label.className = "cr-field";
      const input = element(tag);
      input.name = name;
      input.value = saved[name] || "";
      label.append(input);
      form.append(label);
      return input;
    };
    const rating = add("Rating (optional if you write a comment) ", "rating", "select");
    for (const [value, label] of [["", "Select a rating"], ["5", "5 stars"], ["4", "4 stars"], ["3", "3 stars"], ["2", "2 stars"], ["1", "1 star"]]) {
      const option = element("option", label); option.value = value; rating.append(option);
    }
    rating.classList.add("cr-rating");
    rating.value = saved.rating || "";
    const body = add("Your experience (optional if you give a rating)", "body", "textarea");
    body.maxLength = 5000;
    const title = add("Title (optional)", "title"); title.maxLength = 200;
    const author = add("Display name (optional)", "author"); author.maxLength = 120;
    const button = element("button", "Submit review"); button.type = "submit"; button.className="cr-button cr-button--primary"; button.dataset.crAction="submit";
    const actions=element("div"); actions.className="cr-actions"; actions.append(button); form.append(actions);
    const fields = [rating, body, title, author];
    function values() { return Object.fromEntries(fields.map(field => [field.name, field.value])); }
    function persist(value) { try { sessionStorage.setItem(draftKey, JSON.stringify(value)); } catch (_) { /* still usable */ } }
    form.addEventListener("input", () => persist({...saved, ...values()}));
    form.addEventListener("submit", async event => {
      event.preventDefault();
      const value = values();
      if (!value.rating && value.body.trim().length < 3) {
        message.textContent = "Please choose a rating or write a little about your experience.";
        return;
      }
      const payload = {subject_id: subject.subject_id, invitation_token: token, ...value, rating: value.rating ? Number(value.rating) : null};
      // Never persist the bearer capability, including inside the retry record.
      const fingerprint = JSON.stringify({subject_id: subject.subject_id, ...value});
      const key = saved.fingerprint === fingerprint ? saved.requestKey : crypto.randomUUID();
      saved = {...value, fingerprint, requestKey: key}; persist(saved);
      content.dataset.crState="submitting"; content.setAttribute("aria-busy","true");
      button.disabled = true;
      fields.forEach(field => { field.disabled = true; });
      try {
        const response = await post("/reviews/v1/contributions", payload, key);
        if (response.ok) {
          try { sessionStorage.removeItem(draftKey); } catch (_) { /* no action needed */ }
          document.dispatchEvent(new CustomEvent("cartray:reviews-changed",{detail:{subjectId:subject.subject_id}}));
          await load("Thank you. Your review was submitted and is awaiting moderation.");
        } else {
          content.dataset.crState="error";
          message.textContent = response.status === 429
            ? "Too many attempts. Your draft is still here; please wait a little and try again."
            : response.status === 409
              ? "This review could not be submitted. It may already have been submitted or the invitation may be unavailable. Your draft is still here; please contact the store if you need help."
              : "We couldn't submit your review. Your draft is still here; please try again or contact the store.";
        }
      } catch (_) {
        message.textContent = "We couldn't reach the store. Your draft is still here; please try again.";
      } finally { content.setAttribute("aria-busy","false");
        if(content.dataset.crState==="submitting")content.dataset.crState="error"; button.disabled = false; fields.forEach(field => { field.disabled = false; }); }
    });
    return form;
  }
  async function load(confirmation) {
    const version=++loadVersion; loading();
    if (!token || !/^[a-f0-9]{64}$/.test(token)) { settle("invalid", unavailable); return; }
    try {
      const response = await post("/reviews/v1/invitation", {invitation_token: token});
      if (version!==loadVersion)return;
      if (!response.ok) {
        const failure = response.status === 429
          ? "Too many attempts. Please wait a little and try again."
          : response.status === 503
            ? "The review service is temporarily unavailable. Please try again shortly."
            : unavailable;
        settle("unavailable", failure, response.status === 429 || response.status === 503);
        if (response.status === 404 || response.status === 409) {
          const recover = element("button", "Request a replacement invitation");
          recover.type = "button"; recover.className="cr-button cr-button--secondary";
          recover.addEventListener("click", async () => {
            recover.disabled = true;
            try {
              const request = await post("/reviews/v1/invitation-recovery", {invitation_token: token});
              message.textContent = request.ok
                ? "If this invitation is eligible, your request has been recorded. Please contact the store if you need further help."
                : "We couldn't record your request. Please try again or contact the store.";
            } catch (_) { message.textContent = "We couldn't reach the store. Please try again."; }
            finally { recover.disabled = false; }
          });
          content.querySelector(".cr-actions").prepend(recover);
        }
        return;
      }
      const data = await response.json(); if(version!==loadVersion)return;
      if (!Array.isArray(data.subjects) || !data.subjects.length) throw new Error("invalid invitation");
      data.subjects.sort((a,b) => Number(featured.includes(b.subject_id)) - Number(featured.includes(a.subject_id)));
      content.replaceChildren(element("p", `Your invitation from ${data.store_name}`), ...data.subjects.map(subject => formFor(subject, data.draft_scope)));
      content.dataset.crState="ready"; content.setAttribute("aria-busy","false");
      message.textContent = confirmation || "Choose what to review. Reviews are checked before they appear publicly.";
    } catch (_) { if(version!==loadVersion)return; settle("unavailable", "We couldn't reach the store. Please try again shortly.", true); }
  }
  load();
})();
