import { ConsoleError, createClient } from './client.mjs';
const client = createClient();
const queue = document.querySelector('#queue'), status = document.querySelector('#status'), error = document.querySelector('#error');
const refresh = document.querySelector('#refresh'), more = document.querySelector('#more');
let cursor = null, loading = false;
const seen = new Set();
function message(text = '') { error.textContent = text; error.hidden = !text; }
function friendly(e) { return e instanceof ConsoleError ? e.message : 'The review service is temporarily unavailable. Please try again.'; }
function element(tag, text, cls) { const result = document.createElement(tag); if (text) result.textContent = text; if (cls) result.className = cls; return result; }
function card(item) {
  const article = element('article', '', 'card');
  const title = element('h3', item.title || 'Customer feedback'); article.append(title);
  article.append(element('p', `Product reference: ${item.subject_id}`, 'meta'));
  article.append(element('p', `${item.author || 'Anonymous'} · ${item.rating === null ? 'Unrated' : `${item.rating} out of 5 stars`}`, 'meta'));
  if (item.body) article.append(element('p', item.body));
  const actions = element('div', '', 'actions');
  const approve = element('button', 'Approve and publish', 'primary'); approve.type = 'button';
  const reject = element('button', 'Reject'); reject.type = 'button';
  const confirmation = element('div', '', 'confirmation'); confirmation.hidden = true;
  const prompt = element('p', '');
  const confirmButton = element('button', 'Confirm'); confirmButton.type = 'button';
  const cancelButton = element('button', 'Cancel'); cancelButton.type = 'button';
  confirmation.append(prompt, confirmButton, cancelButton);
  let selected = null;
  cancelButton.addEventListener('click', () => { confirmation.hidden = true; selected = null; });
  for (const [button, action] of [[approve, 'approve'], [reject, 'reject']]) {
    button.setAttribute('aria-label', `${button.textContent}: ${item.title || 'customer feedback'} for ${item.subject_id}`);
    button.addEventListener('click', () => {
      if (loading) return;
      selected = action;
      prompt.textContent = action === 'approve' ? 'Publish this review?' : 'Reject this review?';
      confirmation.hidden = false;
      confirmButton.focus();
    });
  }
  confirmButton.addEventListener('click', async () => {
      if (loading || !selected) return;
      const action = selected;
      loading = true; approve.disabled = reject.disabled = confirmButton.disabled = cancelButton.disabled = refresh.disabled = more.disabled = true; message();
      try {
        await client.decide(item, action);
        article.remove(); seen.delete(item.review_id);
        status.textContent = action === 'approve' ? 'Review approved and published.' : 'Review rejected and kept private.';
        if (!queue.children.length && !cursor) queue.append(element('p', 'No reviews are waiting for moderation.'));
      } catch (e) { message(friendly(e)); }
      finally { loading = false; approve.disabled = reject.disabled = confirmButton.disabled = cancelButton.disabled = refresh.disabled = more.disabled = false; }
  });
  actions.append(approve, reject); article.append(actions, confirmation); return article;
}
async function load(replace) {
  if (loading) return;
  loading = true; refresh.disabled = more.disabled = true; queue.setAttribute('aria-busy', 'true'); message();
  try {
    const result = await client.pending(replace ? null : cursor);
    if (replace) { queue.replaceChildren(); seen.clear(); }
    for (const item of result.pending) if (!seen.has(item.review_id)) { queue.append(card(item)); seen.add(item.review_id); }
    cursor = result.next_after; more.hidden = !cursor;
    if (!queue.children.length && !cursor) queue.append(element('p', 'No reviews are waiting for moderation.'));
    status.textContent = result.pending.length ? `Loaded ${result.pending.length} review${result.pending.length === 1 ? '' : 's'}.` : 'Queue is up to date.';
  } catch (e) { message(friendly(e)); }
  finally { loading = false; refresh.disabled = more.disabled = false; queue.setAttribute('aria-busy', 'false'); }
}
refresh.addEventListener('click', () => load(true));
more.addEventListener('click', () => load(false));
load(true);
