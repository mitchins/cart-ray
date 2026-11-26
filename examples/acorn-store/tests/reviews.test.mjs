import assert from 'node:assert/strict';
import test from 'node:test';
import {readFile} from 'node:fs/promises';
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
  const doc=presentationDocument(), nodes=doc.nodes;
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

function presentationDocument() {
  const element = tag => ({tag, children:[], attributes:{}, hidden:true, disabled:false,
    set textContent(value) { this.text=value;this.children=[]; },
    get textContent() { return (this.text || '') + this.children.map(child=>child.textContent).join(''); },
    setAttribute(name,value) { this.attributes[name]=value; },
    append(child) { this.children.push(child); },
    get childElementCount() { return this.children.filter(child=>child.tag).length; },
    replaceChildren() { this.text='';this.children=[]; },
    addEventListener(_name,callback) { this.click=callback;this.listeners=(this.listeners || 0)+1; },
    removeEventListener() {} });
  const nodes=Object.fromEntries(['reviews','reviews-status','review-list','reviews-more'].map(id=>[id,element('div')]));
  return {nodes, querySelector:id=>nodes[id.slice(1)], createElement:element, createTextNode:text=>({textContent:text}), createDocumentFragment:()=>element('fragment')};
}

const ratedReview={author:'Reviewer',body:'Useful',rating:4,replies:[{author:'Staff',body:'Thanks',rating:5,replies:[]}]};

test('optional rating presentation gets validated data, keeps numeric text, and never decorates replies or unrated contributions',()=>{
  const doc=presentationDocument(), parent=doc.createElement('main'), calls=[];
  renderContribution(ratedReview,parent,doc,{remaining:220},{renderRating:(visual,value,document)=>{
    calls.push(value);assert.equal(document,doc);assert.equal(Object.isFrozen(value),true);
    visual.append(document.createElement('svg'));
  }});
  assert.deepEqual(calls,[{rating:4,maxRating:5}]);
  const author=parent.children[0].children[0];
  assert.equal(author.textContent,'Reviewer · 4 of 5 stars');
  const rating=author.children[0];
  assert.equal(rating.className,'cr-review-rating cr-review-rating--custom');
  assert.equal(rating.children[0].className,'cr-review-rating-text');
  assert.equal(rating.children[1].attributes['aria-hidden'],'true');
  assert.equal(rating.children[1].className,'cr-review-rating-visual');
  for(const rating of [null,0,6,2.5,'5']) renderContribution({...ratedReview,rating},parent,doc,{remaining:220},{
    renderRating:()=>assert.fail('invalid/unrated/reply must not get rating presentation')});
  const ordinary=doc.createElement('main');
  renderContribution(ratedReview,ordinary,doc,{remaining:220});
  assert.equal(ordinary.children[0].children[0].textContent,'Reviewer · 4 of 5 stars');
  assert.equal(ordinary.children[0].children[0].children[0].className,'cr-review-rating');
  assert.equal(ordinary.children[0].children[0].children[0].children.length,1);
});

test('throwing presentation is discarded without losing review text or creating markup from return values',()=>{
  const doc=presentationDocument(), parent=doc.createElement('main');
  renderContribution(ratedReview,parent,doc,{remaining:220},{renderRating:visual=>{
    visual.textContent='partial visual';throw new Error('merchant presentation failed');
  }});
  const author=parent.children[0].children[0];
  assert.equal(author.textContent,'Reviewer · 4 of 5 stars');assert.equal(author.children[0].className,'cr-review-rating');
  assert.equal(author.children[0].children.length,1);
  renderContribution(ratedReview,parent,doc,{remaining:220},{renderRating:()=>'<script>not markup</script>'});
  assert.equal(parent.children[1].children[0].textContent,'Reviewer · 4 of 5 stars');
});

test('summary hooks share the normal snapshot reads, preserve unrated counts, and disappear on invalidation/failure',async()=>{
  const doc=presentationDocument(), calls=[], summaries=[], originalFetch=globalThis.fetch;
  let unrated=false, mismatch=false;
  globalThis.fetch=async(url,options)=>{
    calls.push({url,options});
    return Response.json(url.includes('/summary') ? unrated ? {...summary,rating:{average:null,rated_count:0}} : summary :
      {...page, snapshot_id:mismatch?'changed':summary.snapshot_id,contributions:[ratedReview]});
  };
  try {
    const controller=attachReviews({reviewsEnabled:true,productKey:'p',reviewSubjects:{p:'acorn-growing-kit'}},doc,{
      renderRating:()=>{throw new Error('optional decoration');},
      renderSummary:(visual,value,document)=>{
        assert.equal(Object.isFrozen(value),true);summaries.push(value);visual.append(document.createElement('svg'));
      }});
    await new Promise(resolve=>setImmediate(resolve));
    assert.equal(calls.length,2);
    assert.ok(calls.every(({options})=>options.credentials==='omit' && options.cache==='no-store'));
    assert.deepEqual(summaries,[{average:4,ratedCount:1,contributionCount:2,maxRating:5}]);
    assert.equal(doc.nodes['reviews-status'].textContent,'4 out of 5 · 1 rating · 2 contributions');
    assert.equal(doc.nodes['reviews-status'].children[0].className,'cr-review-summary cr-review-summary--custom');
    assert.equal(doc.nodes['reviews-status'].children[0].children[0].className,'cr-review-summary-text');
    assert.equal(doc.nodes['reviews-status'].children[0].children[1].attributes['aria-hidden'],'true');
    assert.equal(doc.nodes['review-list'].children.length,1);
    unrated=true;controller.refresh();await new Promise(resolve=>setImmediate(resolve));
    assert.equal(calls.length,4);
    assert.deepEqual(summaries[1],{average:null,ratedCount:0,contributionCount:2,maxRating:5});
    assert.equal(doc.nodes['reviews-status'].textContent,'No ratings yet · 2 contributions');
    mismatch=true;controller.refresh();await new Promise(resolve=>setImmediate(resolve));
    assert.equal(calls.length,6);assert.equal(summaries.length,2);
    assert.equal(doc.nodes['reviews-status'].children.length,0);
    assert.equal(doc.nodes['review-list'].children.length,0);
    assert.match(doc.nodes['reviews-status'].textContent,/temporarily unavailable/);
    controller.dispose();
  } finally {globalThis.fetch=originalFetch;}
});

test('empty or throwing summary renderers never mark fallback text as replaced',async()=>{
  const originalFetch=globalThis.fetch;
  globalThis.fetch=fakeFetch([]);
  try {
    for(const renderSummary of [()=>{}, ()=>'<svg/>', (visual,_value,document)=>visual.append(document.createTextNode('')), visual=>{visual.textContent='partial';throw Error('bad visual');}]) {
      const doc=presentationDocument();
      const controller=attachReviews({reviewsEnabled:true,productKey:'p',reviewSubjects:{p:'acorn-growing-kit'}},doc,{renderSummary});
      await new Promise(resolve=>setImmediate(resolve));
      const wrapper=doc.nodes['reviews-status'].children[0];
      assert.equal(wrapper.className,'cr-review-summary');assert.equal(wrapper.children.length,1);
      assert.equal(wrapper.textContent,'4 out of 5 · 1 rating · 2 contributions');
      controller.dispose();
    }
  } finally {globalThis.fetch=originalFetch;}
});


test('automatic mount accepts presentation callbacks without a second reader or pagination listener',async()=>{
  const doc=presentationDocument(), requests=[];
  const original={fetch:globalThis.fetch,window:globalThis.window,document:globalThis.document};
  globalThis.fetch=fakeFetch(requests,[summary,{...page,contributions:[ratedReview]}]);
  globalThis.document=doc;
  globalThis.window={CARTRAY_STOREFRONT:{reviewsEnabled:true,productKey:'p',reviewSubjects:{p:'acorn-growing-kit'},
    reviewPresentation:{renderRating:(visual,_value,document)=>visual.append(document.createElement('svg')),
      renderSummary:(visual,_value,document)=>visual.append(document.createElement('svg'))}}};
  try {
    await import('../../../modules/reviews/ui/public.mjs?automatic-presentation-test');
    await new Promise(resolve=>setImmediate(resolve));
    assert.equal(requests.length,2);
    assert.equal(doc.nodes['review-list'].children.length,1);
    assert.equal(doc.nodes['review-list'].children[0].children.length,1);
    assert.equal(doc.nodes['reviews-more'].listeners,1);
    assert.equal(doc.nodes['reviews-status'].children[0].className,'cr-review-summary cr-review-summary--custom');
    assert.equal(doc.nodes['review-list'].children[0].children[0].children[0].children[0].className,
      'cr-review-rating cr-review-rating--custom');
  } finally {
    globalThis.fetch=original.fetch;
    if(original.window===undefined)delete globalThis.window;else globalThis.window=original.window;
    if(original.document===undefined)delete globalThis.document;else globalThis.document=original.document;
  }
});


test('connected product mount reads document-level presentation without changing the other client bridge',async()=>{
  const doc=presentationDocument(), requests=[];
  const original={fetch:globalThis.fetch,window:globalThis.window,document:globalThis.document};
  globalThis.fetch=fakeFetch(requests,[summary,{...page,contributions:[ratedReview]}]);
  globalThis.window=Object.assign(new EventTarget(),{CARTRAY_STOREFRONT:{reviewsEnabled:true,
    reviewPresentation:{renderRating:(v,_data,d)=>v.append(d.createElement('svg')),
      renderSummary:(v,_data,d)=>v.append(d.createElement('svg'))}}});
  doc.defaultView=globalThis.window;
  const select=doc.querySelector;
  doc.querySelector=id=>id==='#product-data' ? {textContent:JSON.stringify({review_subject:'acorn-growing-kit'})} : select(id);
  globalThis.document=doc;
  try {
    const bridge=await readFile(new URL('../../gel-theme-pack/src/connected-public-reviews.mjs',import.meta.url),'utf8');
    const native=new URL('../../../modules/reviews/ui/public.mjs?connected-presentation-test',import.meta.url).href;
    await import('data:text/javascript,'+encodeURIComponent(bridge.replace("'./reviews.mjs'",JSON.stringify(native))));
    await new Promise(resolve=>setImmediate(resolve));
    assert.equal(requests.length,2);
    assert.equal(doc.nodes['review-list'].children.length,1);
    assert.equal(doc.nodes['reviews-more'].listeners,1);
    assert.equal(doc.nodes['reviews-status'].children[0].className,'cr-review-summary cr-review-summary--custom');
  } finally {
    globalThis.fetch=original.fetch;
    if(original.window===undefined)delete globalThis.window;else globalThis.window=original.window;
    if(original.document===undefined)delete globalThis.document;else globalThis.document=original.document;
  }
});
