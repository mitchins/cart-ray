/* Same-origin standalone invitation form. The capability stays in the fragment. */
(() => {
  "use strict";
  const token = new URLSearchParams(location.hash.slice(1)).get("invitation");
  const message = document.querySelector("#message");
  const content = document.querySelector("#content");
  const unavailable = "This invitation is unavailable. It may have expired or already been replaced. Please contact the store for help.";
  const element = (tag, text) => {
    const node = document.createElement(tag);
    if (text !== undefined) node.textContent = text;
    return node;
  };
  async function post(path, data, key) {
    const headers = {"Content-Type": "application/json"};
    if (key) headers["Idempotency-Key"] = key;
    return fetch(path, {method: "POST", headers, body: JSON.stringify(data), cache: "no-store", referrerPolicy: "no-referrer"});
  }
  function formFor(subject, draftScope) {
    const form = element("form");
    const legend = element("h2", subject.public_name);
    form.append(legend);
    if (subject.progress !== "available") {
      form.append(element("p", "Thank you. Your review has been submitted."));
      if (subject.review_id && subject.management_token) {
        const link = element("a", "Correct or withdraw this review");
        link.href = `./manage.html#review=${encodeURIComponent(subject.review_id)}&token=${encodeURIComponent(subject.management_token)}`;
        form.append(link);
      }
      return form;
    }
    const draftKey = `cartray-review-draft:${draftScope}:${subject.subject_id}`;
    let saved = {};
    try { saved = JSON.parse(sessionStorage.getItem(draftKey) || "{}"); } catch (_) { /* storage is optional */ }
    const add = (labelText, name, tag = "input") => {
      const label = element("label", labelText);
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
    rating.value = saved.rating || "";
    const body = add("Your experience (optional if you give a rating)", "body", "textarea");
    body.maxLength = 5000;
    const title = add("Title (optional)", "title"); title.maxLength = 200;
    const author = add("Display name (optional)", "author"); author.maxLength = 120;
    const button = element("button", "Submit review"); button.type = "submit"; form.append(button);
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
      button.disabled = true;
      fields.forEach(field => { field.disabled = true; });
      try {
        const response = await post("/reviews/v1/contributions", payload, key);
        if (response.ok) {
          try { sessionStorage.removeItem(draftKey); } catch (_) { /* no action needed */ }
          await load("Thank you. Your review was submitted and is awaiting moderation.");
        } else {
          message.textContent = response.status === 429
            ? "Too many attempts. Your draft is still here; please wait a little and try again."
            : response.status === 409
              ? "This review could not be submitted. It may already have been submitted or the invitation may be unavailable. Your draft is still here; please contact the store if you need help."
              : "We couldn't submit your review. Your draft is still here; please try again or contact the store.";
        }
      } catch (_) {
        message.textContent = "We couldn't reach the store. Your draft is still here; please try again.";
      } finally { button.disabled = false; fields.forEach(field => { field.disabled = false; }); }
    });
    return form;
  }
  async function load(confirmation) {
    if (!token || !/^[a-f0-9]{64}$/.test(token)) { message.textContent = unavailable; return; }
    try {
      const response = await post("/reviews/v1/invitation", {invitation_token: token});
      if (!response.ok) {
        message.textContent = response.status === 429
          ? "Too many attempts. Please wait a little and try again."
          : response.status === 503
            ? "The review service is temporarily unavailable. Please try again shortly."
            : unavailable;
        if (response.status === 404 || response.status === 409) {
          const recover = element("button", "Request a replacement invitation");
          recover.type = "button";
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
          content.replaceChildren(recover);
        }
        return;
      }
      const data = await response.json();
      content.replaceChildren(element("p", `Your invitation from ${data.store_name}`), ...data.subjects.map(subject => formFor(subject, data.draft_scope)));
      message.textContent = confirmation || "Choose a product to review. Reviews are checked before they appear publicly.";
    } catch (_) { message.textContent = "We couldn't reach the store. Please try again shortly."; }
  }
  load();
})();
