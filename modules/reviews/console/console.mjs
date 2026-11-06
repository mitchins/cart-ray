import { ConsoleError, createClient } from './client.mjs';
const client = createClient();
const queue = document.querySelector('#queue'), status = document.querySelector('#status'), error = document.querySelector('#error');
const refresh = document.querySelector('#refresh'), more = document.querySelector('#more'), view = document.querySelector('#view');
const heading = document.querySelector('#queue-title'), description = document.querySelector('#queue-description');
let cursor = null, loading = false, shownMode = 'pending';
const seen = new Set();
function message(text = '') { error.textContent = text; error.hidden = !text; }
function friendly(e) { return e instanceof ConsoleError ? e.message : 'The review service is temporarily unavailable. Please try again.'; }
function element(tag, text, cls) { const result = document.createElement(tag); if (text) result.textContent = text; if (cls) result.className = cls; return result; }
function busy(active) { loading = active; refresh.disabled = more.disabled = view.disabled = active; queue.setAttribute('aria-busy', String(active)); }
function history(item) {
  const section = element('section', '', 'history');
  section.append(element('h4', 'Review history'));
  for (const kind of ['revisions', 'audit']) {
    const group = element('div', '', 'history-group'), results = element('div', '');
    results.setAttribute('tabindex', '-1');
    results.setAttribute('role', 'region');
    results.setAttribute('aria-label', `${kind === 'revisions' ? 'Revision history' : 'Audit history'} for ${item.title || 'customer feedback'}`);
    const button = element('button', kind === 'revisions' ? 'Show revisions' : 'Show audit history'); button.type = 'button';
    button.setAttribute('aria-label', `${button.textContent}: ${item.title || 'customer feedback'} for ${item.subject_name || item.subject_id}`);
    let after = null, started = false;
    button.addEventListener('click', async () => {
      if (loading) return;
      busy(true); button.disabled = true; message();
      let finished = false;
      try {
        const page = await client.history(item.review_id, kind, after);
        if (!started && !page.records.length) results.append(element('p', `No ${kind} found.`));
        for (const record of page.records) {
          const line = element('div', '', 'history-entry');
          if (kind === 'revisions') {
            line.append(element('p', `Revision ${record.revision_id.slice(0, 12)}`, 'meta'));
            if (record.title) line.append(element('p', record.title));
            line.append(element('p', `${record.author || 'Anonymous'} · ${record.rating == null ? 'Unrated' : `${record.rating} out of 5 stars`}`, 'meta'));
            if (record.body) line.append(element('p', record.body));
          } else {
            line.append(element('p', `${record.action} · ${record.actor}`, 'meta'));
            if (Number.isSafeInteger(record.occurred_at) && record.occurred_at >= 0 && record.occurred_at <= 253402300799) line.append(element('p', `Recorded ${new Date(record.occurred_at * 1000).toISOString()}`, 'meta'));
          }
          results.append(line);
        }
        started = true; after = page.next_after; button.hidden = !after; finished = !after;
        if (after) button.textContent = kind === 'revisions' ? 'Load more revisions' : 'Load more audit history';
        status.textContent = `Loaded ${page.records.length} ${kind === 'revisions' ? 'revision' : 'audit'} record${page.records.length === 1 ? '' : 's'}.${finished ? ' No more records.' : ''}`;
      } catch (e) { message(friendly(e)); }
      finally { button.disabled = false; busy(false); (finished ? results : button).focus(); }
    });
    group.append(button, results); section.append(group);
  }
  return section;
}
function card(item, mode) {
  const article = element('article', '', 'card');
  const title = element('h3', item.title || 'Customer feedback'); article.append(title);
  article.append(element('p', item.subject_name ? `Product: ${item.subject_name}` : `Product reference: ${item.subject_id}`, 'meta'));
  article.append(element('p', `${item.author || 'Anonymous'} · ${item.rating === null ? 'Unrated' : `${item.rating} out of 5 stars`}`, 'meta'));
  if (item.body) article.append(element('p', item.body));
  const actions = element('div', '', 'actions');
  const choices = mode === 'pending' ? [['Approve and publish', 'approve'], ['Reject', 'reject']] : [['Withdraw', 'withdraw']];
  const confirmation = element('div', '', 'confirmation'); confirmation.hidden = true;
  const prompt = element('p', ''), confirmButton = element('button', 'Confirm'), cancelButton = element('button', 'Cancel');
  confirmButton.type = cancelButton.type = 'button'; confirmation.append(prompt, confirmButton, cancelButton);
  let selected = null, trigger = null;
  cancelButton.addEventListener('click', () => { confirmation.hidden = true; selected = null; trigger?.focus(); });
  for (const [label, action] of choices) {
    const button = element('button', label, action === 'approve' ? 'primary' : ''); button.type = 'button';
    button.setAttribute('aria-label', `${label}: ${item.title || 'customer feedback'} for ${item.subject_name || item.subject_id}`);
    button.addEventListener('click', () => {
      if (loading) return;
      selected = action; trigger = button;
      prompt.textContent = { approve: 'Publish this review?', reject: 'Reject this review?', withdraw: 'Withdraw this published review?' }[action];
      confirmation.hidden = false; confirmButton.focus();
    });
    actions.append(button);
  }
  confirmButton.addEventListener('click', async () => {
    if (loading || !selected) return;
    const action = selected;
    busy(true); confirmButton.disabled = cancelButton.disabled = true; message();
    try {
      await client.decide(item, action);
      article.remove(); seen.delete(item.review_id);
      status.textContent = { approve: 'Review approved and published.', reject: 'Review rejected and kept private.', withdraw: 'Review withdrawn.' }[action];
      if (!queue.children.length && !cursor) queue.append(element('p', mode === 'pending' ? 'No reviews are waiting for moderation.' : 'No published reviews found.'));
      refresh.disabled = false; refresh.focus();
    } catch (e) { message(friendly(e)); }
    finally { confirmButton.disabled = cancelButton.disabled = false; busy(false); }
  });
  article.append(actions, confirmation, history(item)); return article;
}
async function load(replace) {
  if (loading) return;
  const mode = view.value;
  busy(true); message();
  try {
    const result = await client[mode](replace ? null : cursor);
    if (replace) { queue.replaceChildren(); seen.clear(); }
    const records = result[mode];
    for (const item of records) if (!seen.has(item.review_id)) { queue.append(card(item, mode)); seen.add(item.review_id); }
    cursor = result.next_after; more.hidden = !cursor;
    if (!queue.children.length && !cursor) queue.append(element('p', mode === 'pending' ? 'No reviews are waiting for moderation.' : 'No published reviews found.'));
    heading.textContent = mode === 'pending' ? 'Awaiting review' : 'Published reviews';
    description.textContent = mode === 'pending' ? 'Approve feedback to publish it, or reject it to keep it private.' : 'Review published feedback and withdraw it when needed.';
    shownMode = mode;
    status.textContent = records.length ? `Loaded ${records.length} review${records.length === 1 ? '' : 's'}.` : 'List is up to date.';
    if (!replace && !cursor) queue.focus();
  } catch (e) { view.value = shownMode; message(friendly(e)); }
  finally { busy(false); }
}
function startLoad(replace) { return load(replace).catch((e) => message(friendly(e))); }
refresh.addEventListener('click', () => startLoad(true));
more.addEventListener('click', () => startLoad(false));
view.addEventListener('change', () => startLoad(true));
await startLoad(true);
