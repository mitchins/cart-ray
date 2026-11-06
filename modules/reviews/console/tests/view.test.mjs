import test from 'node:test';
import assert from 'node:assert/strict';
class Element {
  constructor(tag) { this.tag = tag; this.children = []; this.listeners = {}; this.textContent = ''; this.hidden = false; }
  append(...items) { for (const item of items) { item.parent = this; this.children.push(item); } }
  replaceChildren(...items) { this.children = []; this.append(...items); }
  remove() { this.parent.children = this.parent.children.filter((child) => child !== this); }
  setAttribute(key, value) { this[key] = value; }
  focus() {}
  addEventListener(name, handler) { this.listeners[name] = handler; }
  async click() { return this.listeners.click(); }
}
const id = 'a'.repeat(64), revision = 'b'.repeat(64);
const flush = () => new Promise((resolve) => setTimeout(resolve, 0));
test('review text is rendered as text, decisions use revisions, and a failed refresh preserves content', async () => {
  const nodes = Object.fromEntries(['queue', 'status', 'error', 'refresh', 'more'].map((name) => [name, new Element(name)]));
  globalThis.document = { querySelector: (s) => nodes[s.slice(1)], createElement: (name) => new Element(name) };
  let phase = 'queue', sent;
  globalThis.fetch = async (url, options) => {
    if (phase === 'failure') return new Response('{}', { status: 503 });
    if (options.method === 'POST') { sent = [url, options]; return Response.json({ review_id: id, status: 'published' }); }
    return Response.json({ pending: [{ review_id: id, revision_id: revision, subject_id: 'product', title: '<img src=x>', author: '<script>', rating: null, body: '<img onerror=bad>' }], next_after: null });
  };
  try {
    await import('../console.mjs'); await flush();
    const card = nodes.queue.children[0]; assert.equal(card.children[0].textContent, '<img src=x>');
    assert.equal(card.children[3].textContent, '<img onerror=bad>');
    phase = 'failure'; await nodes.refresh.click(); assert.equal(nodes.queue.children[0], card); assert.match(nodes.error.textContent, /temporarily/);
    phase = 'queue'; await card.children[4].children[0].click();
    assert.equal(card.children[5].hidden, false);
    await card.children[5].children[1].click();
    assert.equal(sent[0], `/reviews-admin/v1/contributions/${id}/approve`);
    assert.deepEqual(JSON.parse(sent[1].body), { revision_id: revision });
    assert.match(nodes.status.textContent, /approved/); assert.match(nodes.queue.children[0].textContent, /No reviews/);
  } finally { delete globalThis.document; delete globalThis.fetch; }
});
