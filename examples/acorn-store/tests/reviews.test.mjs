import assert from 'node:assert/strict';
import test from 'node:test';
import { attachReviews, renderContribution, reviewsPage } from '../reviews.mjs';

const summary = { subject_id: 'acorn-growing-kit', snapshot_id: 'native-1', rating: { average: 4, rated_count: 1 }, contribution_count: 2 };
const page = { subject_id: 'acorn-growing-kit', snapshot_id: 'native-1', contributions: [], next_after: null, has_more: false };
function fakeFetch(requests, values = [summary, page]) {
  return async (url, options) => {
    requests.push({ url, options });
    return Response.json(url.includes('/summary') ? values[0] : values[1]);
  };
}
test('loads same-subject matching public revisions without credentials', async () => {
  const requests = [];
  const result = await reviewsPage('acorn-growing-kit', null, null, fakeFetch(requests));
  assert.equal(result.summary.rating.average, 4);
  assert.equal(result.summary.contribution_count, 2);
  assert.equal(requests.length, 2);
  assert.ok(requests.every(({ options }) => options.credentials === 'omit' && options.cache === 'no-store'));
});
test('rejects mismatched revisions and subjects instead of displaying inconsistent data', async () => {
  await assert.rejects(reviewsPage('acorn-growing-kit', null, null, fakeFetch([], [summary, { ...page, snapshot_id: 'native-2' }])));
  await assert.rejects(reviewsPage('acorn-growing-kit', null, null, fakeFetch([], [summary, { ...page, subject_id: 'other' }])));
});
test('pagination binds its cursor to the original revision', async () => {
  const requests = [];
  await reviewsPage('acorn-growing-kit', 'abc123', 'native-1', fakeFetch(requests));
  assert.ok(requests.some(({ url }) => url.includes('after=abc123') && url.includes('snapshot=native-1')));
  await assert.rejects(reviewsPage('acorn-growing-kit', 'abc123', 'native-0', fakeFetch([])));
});
test('rejects malformed subjects, cursors and unavailable service', async () => {
  await assert.rejects(reviewsPage('../admin'));
  await assert.rejects(reviewsPage('acorn-growing-kit', '../bad', 'native-1'));
  await assert.rejects(reviewsPage('acorn-growing-kit', null, null, async () => new Response('down', { status: 503 })));
});

test('renders a populated reply tree in order and bounds the total nodes', () => {
  const doc = { createElement: tag => ({ tag, children: [], append(child) { this.children.push(child); } }) };
  const parent = doc.createElement('main');
  const review = { author: 'Reviewer', body: 'A review', replies: [
    { author: 'Staff', body: 'First reply', replies: [
      { author: 'Reviewer', body: 'Nested reply', replies: [] },
    ] },
    { author: 'Staff', body: 'Second reply', replies: [] },
  ] };
  renderContribution(review, parent, doc, { remaining: 220 });
  const article = parent.children[0];
  const first = article.children[2];
  assert.equal(first.children[0].textContent, 'First reply');
  assert.equal(first.children[2].children[0].textContent, 'Nested reply');
  assert.equal(article.children[3].children[0].textContent, 'Second reply');
  assert.throws(() => renderContribution(review, doc.createElement('main'), doc, { remaining: 3 }));
});

test('disabled public reviews make no request and stale pagination removes displayed content', async () => {
  const element = () => ({children:[], hidden:true, textContent:'', disabled:false,
    append(child) { this.children.push(child); }, replaceChildren() { this.children=[]; },
    addEventListener(_event, callback) { this.click=callback; }});
  const nodes = Object.fromEntries(['reviews','reviews-status','review-list','reviews-more'].map(id => [id,element()]));
  const doc = {querySelector: id => nodes[id.slice(1)], createElement: element, createDocumentFragment: element};
  const originalFetch = globalThis.fetch;
  let calls=0, changed=false;
  globalThis.fetch = async url => {
    calls++;
    const firstPage = {...page, contributions:[{author:'Reviewer',title:'Helpful',rating:4,
      verification:'cartray_verified_acquisition',body:'Review text',replies:[]}],next_after:'next',has_more:true};
    return Response.json(url.includes('/summary') ? summary : changed ? {...firstPage,snapshot_id:'new'} : firstPage);
  };
  try {
    attachReviews({reviewsEnabled:false,productKey:'p',reviewSubjects:{p:'acorn-growing-kit'}},doc);
    assert.equal(calls,0);assert.equal(nodes.reviews.hidden,true);
    attachReviews({reviewsEnabled:true,productKey:'p',reviewSubjects:{p:'acorn-growing-kit'}},doc);
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(calls,2);assert.equal(nodes.reviews.hidden,false);
    assert.equal(nodes['review-list'].children.length,1);
    assert.match(nodes['reviews-status'].textContent,/4 out of 5/);
    changed=true;nodes['reviews-more'].click();
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(nodes['review-list'].children.length,0);
    assert.match(nodes['reviews-status'].textContent,/temporarily unavailable/);
  } finally { globalThis.fetch=originalFetch; }
});

test('rejects oversized public review responses', async () => {
  await assert.rejects(reviewsPage('acorn-growing-kit',null,null,async()=>new Response('x'.repeat(128001))));
});
