import test from 'node:test';
import assert from 'node:assert/strict';
class Element {
  constructor(tag) { this.tag = tag; this.children = []; this.listeners = {}; this.textContent = ''; this.hidden = false; }
  append(...items) { for (const item of items) { item.parent = this; this.children.push(item); } }
  replaceChildren(...items) { this.children = []; this.append(...items); }
  remove() { this.parent.children = this.parent.children.filter((child) => child !== this); }
  setAttribute(key, value) { this[key] = value; }
  focus() { globalThis.lastFocused = this; }
  addEventListener(name, handler) { this.listeners[name] = handler; }
  async click() { return this.listeners.click(); }
  async change() { return this.listeners.change(); }
}
const id = 'a'.repeat(64), revision = 'b'.repeat(64);
const flush = () => new Promise((resolve) => setTimeout(resolve, 0));
test('review text is rendered as text, decisions use revisions, and a failed refresh preserves content', async () => {
  const nodes = Object.fromEntries(['queue', 'status', 'error', 'refresh', 'more', 'view', 'queue-title', 'queue-description', 'delivery-state', 'delivery-load', 'delivery-more', 'delivery-results', 'delivery-status', 'delivery-error'].map((name) => [name, new Element(name)]));
  nodes.view.value = 'pending';
  globalThis.document = { querySelector: (s) => nodes[s.slice(1)], createElement: (name) => new Element(name) };
  let phase = 'queue', sent;
  globalThis.fetch = async (url, options) => {
    if (phase === 'failure') return new Response('{}', { status: 503 });
    if (options.method === 'POST') { sent = [url, options]; return Response.json({ review_id: id, status: url.endsWith('/withdraw') ? 'withdrawn' : 'published' }); }
    if (url.endsWith('/published')) return Response.json({ published: [{ review_id: id, revision_id: revision, subject_id: 'product', subject_name: 'Example product', title: 'Helpful', author: 'Customer', rating: 4, body: 'Good' }], next_after: null });
    if (url.endsWith('/revisions')) return Response.json({ review_id: id, revisions: [{ revision_id: revision, title: '<img>', author: 'Customer', body: 'Original', rating: 4 }], next_after: null });
    if (url.endsWith('/audit')) return Response.json({ review_id: id, audit: [{ event_id: 'c'.repeat(64), revision_id: revision, action: 'approve', actor: 'staff:example', occurred_at: 1700000000 }], next_after: null });
    return Response.json({ pending: [{ review_id: id, revision_id: revision, subject_id: 'product', title: '<img src=x>', author: '<script>', rating: null, body: '<img onerror=bad>' }], next_after: null });
  };
  try {
    await import('../console.mjs'); await flush();
    const card = nodes.queue.children[0]; assert.equal(card.children[0].textContent, '<img src=x>');
    assert.equal(card.children[3].textContent, '<img onerror=bad>');
    phase = 'failure'; await nodes.refresh.click(); assert.equal(nodes.queue.children[0], card); assert.match(nodes.error.textContent, /temporarily/);
    nodes.view.value = 'published'; await nodes.view.change();
    assert.equal(nodes.view.value, 'pending'); assert.equal(nodes.queue.children[0], card);
    phase = 'queue'; await card.children[4].children[0].click();
    assert.equal(card.children[5].hidden, false);
    await card.children[5].children[2].click();
    assert.equal(globalThis.lastFocused, card.children[4].children[0]);
    await card.children[4].children[0].click();
    await card.children[5].children[1].click();
    assert.equal(sent[0], `/reviews-admin/v1/contributions/${id}/approve`);
    assert.deepEqual(JSON.parse(sent[1].body), { revision_id: revision });
    assert.match(nodes.status.textContent, /approved/); assert.match(nodes.queue.children[0].textContent, /No reviews/);
    assert.equal(globalThis.lastFocused, nodes.refresh);
    nodes.view.value = 'published'; await nodes.view.change();
    const published = nodes.queue.children[0]; assert.equal(published.children[1].textContent, 'Product: Example product');
    await published.children[6].children[1].children[0].click();
    const revisionResults = published.children[6].children[1].children[1];
    assert.equal(revisionResults.children[0].children[1].textContent, '<img>');
    assert.equal(globalThis.lastFocused, revisionResults);
    assert.equal(revisionResults.role, 'region'); assert.match(revisionResults['aria-label'], /Revision history/);
    assert.match(nodes.status.textContent, /No more records/);
    await published.children[6].children[2].children[0].click();
    const auditResults = published.children[6].children[2].children[1];
    assert.match(auditResults.children[0].children[0].textContent, /staff:example/);
    assert.equal(globalThis.lastFocused, auditResults);
    await published.children[4].children[0].click(); await published.children[5].children[1].click();
    assert.match(sent[0], /\/withdraw$/); assert.deepEqual(JSON.parse(sent[1].body), { revision_id: revision });
    assert.match(nodes.status.textContent, /withdrawn/);
  } finally { delete globalThis.document; delete globalThis.fetch; delete globalThis.lastFocused; }
});
test('finishing queue pagination preserves content and moves focus into the labeled queue', async () => {
  const nodes = Object.fromEntries(['queue', 'status', 'error', 'refresh', 'more', 'view', 'queue-title', 'queue-description', 'delivery-state', 'delivery-load', 'delivery-more', 'delivery-results', 'delivery-status', 'delivery-error'].map((name) => [name, new Element(name)]));
  nodes.view.value = 'pending';
  globalThis.document = { querySelector: (s) => nodes[s.slice(1)], createElement: (name) => new Element(name) };
  globalThis.fetch = async (url) => url.includes('?after=')
    ? Response.json({ pending: [], next_after: null })
    : Response.json({ pending: [{ review_id: id, revision_id: revision, subject_id: 'product', title: null, author: '', rating: null, body: 'Text' }], next_after: id });
  try {
    await import('../console.mjs?pagination-test');
    assert.equal(nodes.more.hidden, false);
    const card = nodes.queue.children[0];
    await nodes.more.click();
    assert.equal(nodes.queue.children[0], card); assert.equal(nodes.more.hidden, true);
    assert.equal(globalThis.lastFocused, nodes.queue);
  } finally { delete globalThis.document; delete globalThis.fetch; delete globalThis.lastFocused; }
});
