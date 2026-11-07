// Test-only reply controls. Requests remain bound to their original idempotency key and payload.
import { ConsoleError } from './client.mjs';

export function replyControls(item, { client, element, isBusy, setBusy, message, status }) {
  const section = element('section', '', 'history');
  section.append(element('h4', 'Staff reply'));
  const show = element('button', 'Manage reply'); show.type = 'button';
  show.setAttribute('aria-label', `Manage reply: ${item.title || 'customer feedback'} for ${item.subject_name || item.subject_id}`);
  const output = element('div', ''); output.setAttribute('role', 'region'); output.setAttribute('tabindex', '-1');
  output.setAttribute('aria-label', `Staff reply history for ${item.title || 'customer feedback'}`);
  const panel = element('div', ''), pageButton = element('button', 'Load more reply history'); pageButton.type = 'button'; pageButton.hidden = true;
  const authorLabel = element('label', 'Public display name'); authorLabel.htmlFor = `reply-author-${item.review_id}`;
  const author = element('input', ''); author.id = authorLabel.htmlFor; author.maxLength = 120;
  const bodyLabel = element('label', 'Reply'); bodyLabel.htmlFor = `reply-body-${item.review_id}`;
  const body = element('textarea', ''); body.id = bodyLabel.htmlFor; body.maxLength = 5000; body.rows = 4;
  const save = element('button', 'Publish reply', 'primary'); save.type = 'button';
  const withdraw = element('button', 'Withdraw reply'); withdraw.type = 'button';
  const confirmation = element('div', '', 'confirmation'); confirmation.hidden = true;
  const confirmWithdraw = element('button', 'Confirm withdrawal'); confirmWithdraw.type = 'button';
  const cancel = element('button', 'Cancel'); cancel.type = 'button';
  confirmation.append(element('p', 'Withdraw this reply?'), confirmWithdraw, cancel);
  const retry = element('button', 'Retry same request'); retry.type = 'button'; retry.hidden = true;
  const form = element('div', '', 'reply-form'); form.hidden = true;
  form.append(authorLabel, author, bodyLabel, body, save, withdraw, confirmation, retry);
  const audit = element('button', 'Show reply audit'); audit.type = 'button';
  const auditOutput = element('div', ''); auditOutput.setAttribute('role', 'region'); auditOutput.setAttribute('tabindex', '-1');
  auditOutput.setAttribute('aria-label', `Reply audit history for ${item.title || 'customer feedback'}`);
  panel.append(output, pageButton, form, audit, auditOutput); section.append(show, panel);
  let head = null, cursor = null, current = null, attempt = null, headIdentity = null;
  let auditCursor = null, auditStarted = false;
  const headKey = (value) => value ? `${value.reply_id}:${value.reply_revision_id}:${value.parent_revision_id}:${value.state}` : 'none';
  function resetForm() { form.hidden = true; author.value = ''; body.value = ''; current = null; }
  function freeze(frozen) { author.disabled = body.disabled = save.disabled = withdraw.disabled = frozen; retry.hidden = !frozen; }
  async function loadHistory(reset) {
    const page = await client.replyHistory(item, reset ? null : cursor);
    if (!reset && headKey(page.head) !== headIdentity) throw new ConsoleError('The reply changed. Reload the review list before continuing.', 409);
    if (reset) { output.replaceChildren(); resetForm(); head = page.head; headIdentity = headKey(head); }
    for (const record of page.records) {
      const line = element('div', '', 'history-entry');
      line.append(element('p', `Reply revision ${record.reply_revision_id.slice(0, 12)} · ${record.author}`, 'meta'));
      line.append(element('p', record.body)); output.append(line);
      if (head && record.reply_revision_id === head.reply_revision_id) current = record;
    }
    cursor = page.next_after; pageButton.hidden = !cursor;
    if (head === null) {
      output.append(element('p', 'No reply has been published.'));
      form.hidden = false; withdraw.hidden = true; save.textContent = 'Publish reply';
    } else if (head.state === 'withdrawn') {
      output.append(element('p', 'This reply was withdrawn. Creating a replacement is not currently supported.'));
      form.hidden = true;
    } else if (current) {
      if (form.hidden) { author.value = current.author; body.value = current.body; }
      form.hidden = false;
      withdraw.hidden = false; save.textContent = 'Save changes';
    } else {
      form.hidden = true;
      output.append(element('p', cursor ? 'Load more history to reach the current reply.' : 'Current reply revision was not found. Refresh the review list.'));
    }
    status.textContent = `Loaded ${page.records.length} reply revision${page.records.length === 1 ? '' : 's'}.${cursor ? '' : ' No more records.'}`;
    return !cursor;
  }
  async function load(reset) {
    if (isBusy()) return;
    setBusy(true); show.disabled = pageButton.disabled = true; message();
    let finished = false;
    try { finished = await loadHistory(reset); }
    catch (error) { message(error instanceof ConsoleError ? error.message : 'The reply service is temporarily unavailable.'); }
    finally { show.disabled = pageButton.disabled = false; setBusy(false); (finished ? output : pageButton.hidden ? show : pageButton).focus(); }
  }
  show.addEventListener('click', () => {
    if (attempt) return;
    if (!form.hidden && (author.value !== (current?.author || '') || body.value !== (current?.body || ''))) {
      message('Your reply draft is unsaved. Submit it or restore the original text before refreshing.');
      return;
    }
    return load(true);
  });
  pageButton.addEventListener('click', () => { if (!attempt) return load(false); });
  async function sendAttempt() {
    if (isBusy() || !attempt) return;
    setBusy(true); retry.disabled = true; message();
    let conflict = false;
    try {
      await client.reply(item, attempt.action, attempt.payload, attempt.key);
      attempt = null; freeze(false); confirmation.hidden = true; form.hidden = true;
      audit.hidden = false; audit.textContent = 'Show reply audit'; auditStarted = false; auditCursor = null; auditOutput.replaceChildren();
      status.textContent = 'Reply saved. Refreshing reply history.';
      try { await loadHistory(true); }
      catch { form.hidden = true; message('Reply saved, but history could not be refreshed. Use Manage reply to refresh.'); }
    } catch (error) {
      if (error instanceof ConsoleError && error.status === 409) {
        attempt = null; conflict = true; retry.hidden = true; save.disabled = withdraw.disabled = true;
        author.disabled = body.disabled = false;
        message('The review or reply changed. Copy any unsaved draft, then refresh the review list.');
      } else {
        freeze(true);
        message(error instanceof ConsoleError ? `${error.message} Retry the same request or refresh the page to inspect its outcome.` : 'Reply outcome is uncertain. Retry the same request or refresh the page to inspect its outcome.');
      }
    } finally { retry.disabled = false; setBusy(false); (conflict ? body : attempt ? retry : show).focus(); }
  }
  save.addEventListener('click', () => {
    if (isBusy() || attempt) return;
    if (!author.value.trim() || author.value.length > 120 || body.value.trim().length < 3 || body.value.length > 5000) { message('Enter a display name and a reply of at least three characters.'); return; }
    attempt = { action: 'upsert', payload: Object.freeze({ expected_reply_revision_id: head?.reply_revision_id ?? null, author: author.value, body: body.value }), key: crypto.randomUUID() };
    return sendAttempt();
  });
  withdraw.addEventListener('click', () => { if (!isBusy() && !attempt) { confirmation.hidden = false; confirmWithdraw.focus(); } });
  cancel.addEventListener('click', () => { confirmation.hidden = true; withdraw.focus(); });
  confirmWithdraw.addEventListener('click', () => {
    if (isBusy() || attempt || !head || head.state !== 'published') return;
    attempt = { action: 'withdraw', payload: Object.freeze({ expected_reply_revision_id: head.reply_revision_id }), key: crypto.randomUUID() };
    return sendAttempt();
  });
  retry.addEventListener('click', () => sendAttempt());
  audit.addEventListener('click', async () => {
    if (isBusy()) return;
    setBusy(true); audit.disabled = true; message();
    let finished = false;
    try {
      const page = await client.replyAudit(item, auditCursor);
      if (!auditStarted && !page.records.length) auditOutput.append(element('p', 'No reply audit events found.'));
      for (const event of page.records) {
        const line = element('div', '', 'history-entry');
        line.append(element('p', `${event.action} · ${event.actor}`, 'meta'));
        line.append(element('p', `Recorded ${new Date(event.occurred_at * 1000).toISOString()}`, 'meta'));
        auditOutput.append(line);
      }
      auditStarted = true; auditCursor = page.next_after; finished = !auditCursor;
      audit.hidden = finished;
      if (auditCursor) audit.textContent = 'Load more reply audit';
      status.textContent = `Loaded ${page.records.length} reply audit event${page.records.length === 1 ? '' : 's'}.${finished ? ' No more records.' : ''}`;
    } catch (error) { message(error instanceof ConsoleError ? error.message : 'Reply audit is temporarily unavailable.'); }
    finally { audit.disabled = false; setBusy(false); (finished ? auditOutput : audit).focus(); }
  });
  return section;
}
