import test from 'node:test';
import assert from 'node:assert/strict';
import { ConsoleError } from '../client.mjs';
import { replyControls } from '../reply-controls.mjs';
import { setupDeliveries } from '../delivery-controls.mjs';
class Element {
  constructor(tag, text = '', cls = '') { this.tag = tag; this.textContent = text; this.className = cls; this.children = []; this.listeners = {}; this.hidden = false; this.value = ''; }
  append(...items) { for (const item of items) { item.parent = this; this.children.push(item); } }
  replaceChildren(...items) { this.children = []; this.append(...items); }
  setAttribute(key, value) { this[key] = value; }
  addEventListener(name, handler) { this.listeners[name] = handler; }
  focus() { globalThis.focused = this; }
  async click() { return this.listeners.click(); }
  async change() { return this.listeners.change(); }
}
const element = (tag, text, cls) => new Element(tag, text, cls);
const id = 'a'.repeat(64), revision = 'b'.repeat(64), replyId = 'c'.repeat(64), replyRevision = 'd'.repeat(64);
const item = { review_id: id, revision_id: revision, subject_id: 'policy', title: 'Helpful' };
const head = { reply_id: replyId, reply_revision_id: replyRevision, parent_revision_id: revision, state: 'published' };
const row = { reply_revision_id: replyRevision, author: 'Store', body: '<img onerror=bad>', created_at: 1 };

test('reply outcome uncertainty freezes the exact request and retries with the same idempotency key', async () => {
  let state = 'empty', loading = false, message = '', calls = [];
  const client = {
    async replyHistory() { return state === 'empty' ? { head: null, records: [], next_after: null } : { head: { ...head, state }, records: [row], next_after: null }; },
    async replyAudit() { return { records: [{ event_id: 'f'.repeat(64), action: 'created', actor: 'staff:x', occurred_at: 1 }], next_after: null }; },
    async reply(_item, action, payload, key) {
      calls.push({ action, payload, key });
      if (calls.length === 1) throw new ConsoleError('Request timed out.');
      state = action === 'withdraw' ? 'withdrawn' : 'published';
      return { reply_id: replyId, reply_revision_id: replyRevision, state };
    },
  };
  const section = replyControls(item, { client, element, isBusy: () => loading, setBusy: (v) => { loading = v; }, message: (v = '') => { message = v; }, status: {} });
  const show = section.children[1], form = section.children[2].children[2];
  await show.click(); assert.equal(form.hidden, false);
  form.children[1].value = 'Store'; form.children[3].value = 'Thank you';
  await form.children[4].click();
  assert.equal(form.children[1].disabled, true); assert.equal(form.children[7].hidden, false); assert.match(message, /Retry the same request/);
  form.children[3].value = 'tampered after timeout';
  await form.children[7].click();
  assert.equal(calls[0].key, calls[1].key); assert.equal(calls[1].payload.body, 'Thank you');
  assert.equal(form.children[4].textContent, 'Save changes');
  await section.children[2].children[3].click();
  assert.match(section.children[2].children[4].children[0].children[0].textContent, /staff:x/);
  assert.equal(globalThis.focused, section.children[2].children[4]);
  await form.children[5].click(); assert.equal(form.children[6].hidden, false);
  await form.children[6].children[1].click();
  assert.equal(calls[2].action, 'withdraw'); assert.equal(calls[2].payload.expected_reply_revision_id, replyRevision);
  assert.equal(form.hidden, true);
});

test('reply editing waits until the current revision is found in paginated history', async () => {
  let loading = false, calls = 0;
  const client = { async replyHistory(_item, cursor) {
    calls++;
    return cursor === null ? { head, records: [{ ...row, reply_revision_id: 'e'.repeat(64), body: 'Older' }], next_after: 'e'.repeat(64) }
      : { head, records: [row], next_after: null };
  } };
  const section = replyControls(item, { client, element, isBusy: () => loading, setBusy: (v) => { loading = v; }, message: () => {}, status: {} });
  const panel = section.children[2], form = panel.children[2];
  await section.children[1].click(); assert.equal(form.hidden, true); assert.equal(panel.children[1].hidden, false);
  await panel.children[1].click(); assert.equal(form.hidden, false); assert.equal(form.children[3].value, '<img onerror=bad>');
  assert.equal(calls, 2);
});

test('delivery investigation is read-only and permission failure does not disable moderation', async () => {
  const names = ['delivery-state', 'delivery-load', 'delivery-more', 'delivery-results', 'delivery-status', 'delivery-error'];
  const nodes = Object.fromEntries(names.map((name) => [name, new Element(name)])); nodes['delivery-state'].value = 'uncertain';
  let calls = 0, denied = true;
  const client = { async deliveries(state) { calls++; if (denied) throw new ConsoleError('unauthorized', 401); return { records: [{ delivery_identity: 'f'.repeat(64), state, prepared_at: 1 }], next_after: null }; } };
  setupDeliveries({ client, document: { querySelector: (selector) => nodes[selector.slice(1)] }, element });
  assert.equal(calls, 0);
  await nodes['delivery-load'].click(); assert.match(nodes['delivery-error'].textContent, /operations permission/);
  denied = false; await nodes['delivery-load'].click();
  assert.match(nodes['delivery-results'].children[0].children[1].textContent, /Delivery reference/);
  assert.equal(nodes['delivery-results'].children[0].children.length, 3);
});

test('reply history preserves edits made while loading and save locks inputs', async () => {
  let loading = false, resolveHistory, resolveSave, msg = '', deferred = false;
  const page = { head: null, records: [], next_after: null };
  const client = {
    replyHistory() { return deferred ? new Promise((resolve) => { resolveHistory = resolve; }) : Promise.resolve(page); },
    reply() { return new Promise((resolve) => { resolveSave = resolve; }); },
  };
  const section = replyControls(item, { client, element, isBusy: () => loading, setBusy: (v) => { loading = v; }, message: (v = '') => { msg = v; }, status: {} });
  const show = section.children[1], form = section.children[2].children[2];
  await show.click(); deferred = true;
  const refresh = show.click(); form.children[1].value = 'Staff'; form.children[3].value = 'Draft while loading'; resolveHistory(page); await refresh;
  assert.equal(form.children[3].value, 'Draft while loading'); assert.match(msg, /draft is unsaved/);
  deferred = false;
  const saving = form.children[4].click(); assert.equal(form.children[1].disabled, true); assert.equal(form.children[3].disabled, true);
  resolveSave({ reply_id: replyId, reply_revision_id: replyRevision, state: 'published' }); await saving;
});

test('subject-name setting preserves input and locks on an uncertain result', async () => {
  const { setupSubjectNames } = await import('../subject-name-controls.mjs');
  const ids = ['subject-name-form', 'subject-name-id', 'subject-name-value', 'subject-name-save', 'subject-name-status', 'subject-name-error'];
  const nodes = Object.fromEntries(ids.map((key) => [key, new Element(key)]));
  nodes['subject-name-id'].value = 'acorn-kit'; nodes['subject-name-value'].value = 'Acorn Kit';
  let calls = 0;
  setupSubjectNames({ client: { async setSubjectName() { calls++; throw new ConsoleError('timeout'); } }, document: { querySelector: (selector) => nodes[selector.slice(1)] } });
  await nodes['subject-name-form'].listeners.submit({ preventDefault() {} });
  assert.match(nodes['subject-name-error'].textContent, /outcome is uncertain/);
  assert.equal(nodes['subject-name-value'].value, 'Acorn Kit');
  assert.equal(nodes['subject-name-save'].disabled, true);
  await nodes['subject-name-form'].listeners.submit({ preventDefault() {} });
  assert.equal(calls, 1);
});

test('subject-name permission denial preserves input and permits a corrected request', async () => {
  const { setupSubjectNames } = await import('../subject-name-controls.mjs');
  const ids = ['subject-name-form', 'subject-name-id', 'subject-name-value', 'subject-name-save', 'subject-name-status', 'subject-name-error'];
  const nodes = Object.fromEntries(ids.map((key) => [key, new Element(key)]));
  nodes['subject-name-id'].value = 'acorn-kit'; nodes['subject-name-value'].value = 'Acorn Kit';
  let allowed = false;
  setupSubjectNames({ client: { async setSubjectName() { if (!allowed) throw new ConsoleError('Permission required.', 401); } }, document: { querySelector: (selector) => nodes[selector.slice(1)] } });
  await nodes['subject-name-form'].listeners.submit({ preventDefault() {} });
  assert.match(nodes['subject-name-error'].textContent, /Permission/); assert.equal(nodes['subject-name-save'].disabled, false);
  allowed = true; await nodes['subject-name-form'].listeners.submit({ preventDefault() {} });
  assert.match(nodes['subject-name-status'].textContent, /saved/); assert.equal(nodes['subject-name-value'].value, 'Acorn Kit');
});
