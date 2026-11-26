// Public reads only; deployments must route /reviews/v1/ to the scoped Reviews service.
const ID = /^[a-z0-9-]{1,128}$/;
const MAX_BYTES = 128_000;
const validSubject = value => typeof value==='string' && ID.test(value);

async function readJson(url, fetchImpl, signal) {
  const response = await fetchImpl(url, { signal, cache: 'no-store', redirect: 'error', credentials: 'omit' });
  if (!response.ok || !response.body) {
    const error = new Error('Reviews unavailable'); error.status = response.status;
    error.retryAfter = response.headers?.get('Retry-After'); throw error;
  }
  const reader = response.body.getReader();
  const chunks = []; let total = 0;
  try {
    while (true) {
      const part = await reader.read(); if (part.done) break;
      total += part.value.byteLength;
      if (total > MAX_BYTES) throw new Error('Reviews response too large');
      chunks.push(part.value);
    }
  } catch (error) {
    void reader.cancel().catch(() => {});
    throw error;
  } finally { reader.releaseLock(); }
  const bytes = new Uint8Array(total); let offset = 0;
  for (const part of chunks) { bytes.set(part, offset); offset += part.byteLength; }
  return JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(bytes));
}

function validatedSummary(summary, subject) {
  if (summary?.subject_id !== subject || typeof summary.snapshot_id !== 'string' ||
      !summary.snapshot_id.length || summary.snapshot_id.length > 200 ||
      !Number.isSafeInteger(summary.contribution_count) || summary.contribution_count < 0 ||
      !Number.isSafeInteger(summary.rating?.rated_count) || summary.rating.rated_count < 0 ||
      summary.rating.rated_count > summary.contribution_count ||
      (summary.rating.rated_count === 0 ? summary.rating.average !== null :
        typeof summary.rating.average !== 'number' || !Number.isFinite(summary.rating.average) ||
        summary.rating.average < 1 || summary.rating.average > 5)) throw new Error('Invalid review summary');
  return summary;
}

export async function reviewsSummary(subject, fetchImpl = fetch, {signal} = {}) {
  if (!validSubject(subject)) throw new Error('Invalid review selection');
  const controller = new AbortController();
  const stop = () => controller.abort(); signal?.addEventListener('abort',stop,{once:true});
  if (signal?.aborted) controller.abort();
  const timer = setTimeout(stop,5000);
  try {
    return validatedSummary(await readJson(`/reviews/v1/subjects/${encodeURIComponent(subject)}/summary`,
      fetchImpl,controller.signal),subject);
  } finally { clearTimeout(timer); signal?.removeEventListener('abort',stop); controller.abort(); }
}

/** One reader per document. Visibility and rendered-state generation belong to the host. */
export function createSummaryReader({fetchImpl = fetch, maxAgeMs = 30000, now = Date.now} = {}) {
  if (!Number.isFinite(maxAgeMs) || maxAgeMs < 0 || maxAgeMs > 60000) throw new Error('Invalid summary freshness');
  const entries = new Map(), queue = []; let active=0, disposed=false, cooldown=0, starts=[];
  function unavailable(retryAt) { const error=new Error('Reviews temporarily unavailable'); error.retryAt=retryAt; return error; }
  function pump() {
    while (active < 2 && queue.length) {
      const job=queue.shift(); if (entries.get(job.subject)!==job) continue;
      const time=now(); starts=starts.filter(value=>value>time-60000);
      if (disposed || time<cooldown || starts.length>=30) {
        cooldown=Math.max(cooldown, starts.length>=30 ? starts[0]+60000 : 0);
        entries.delete(job.subject); job.reject(unavailable(cooldown)); continue;
      }
      active++; starts.push(time);
      reviewsSummary(job.subject,fetchImpl,{signal:job.controller.signal}).then(summary=>{
        if (entries.get(job.subject)!==job) throw new Error('Reviews invalidated');
        job.value=structuredClone(summary); job.expires=now()+maxAgeMs; job.settled=true; job.resolve();
      }).catch(error=>{
        if (error.status===429) {
          const seconds=Number(error.retryAfter);
          const date=Date.parse(error.retryAfter || '');
          const retry=error.retryAfter && Number.isFinite(seconds) && seconds>=0 ? now()+seconds*1000 : date;
          cooldown=Math.max(cooldown,Number.isFinite(retry) && retry>now() ? retry : now()+60000);
          error.retryAt=cooldown;
        }
        if (entries.get(job.subject)===job) entries.delete(job.subject);
        job.reject(error);
      }).finally(()=>{active--;pump();});
    }
  }
  function invalidate(subject) {
    for (const [key,job] of entries) if (subject===undefined || subject===key) {
      entries.delete(key);
      const index=queue.indexOf(job);
      if(index>=0){queue.splice(index,1);}
      if (!job.settled) { job.controller.abort(); job.reject(new Error('Reviews invalidated')); }
    }
  }
  return {
    isStale() { return !entries.size || [...entries.values()].some(job=>job.settled && now()>=job.expires); },
    read(subject,{signal}={}) {
      if (disposed || !validSubject(subject) || signal?.aborted) return Promise.reject(new Error('Reviews unavailable'));
      let job=entries.get(subject);
      if (job?.settled && now()>=job.expires) { entries.delete(subject); job=null; }
      if (!job) {
        if (now()<cooldown) return Promise.reject(unavailable(cooldown));
        if (entries.size>=128) {
          const old=[...entries].find(([,entry])=>entry.settled && !entry.users);
          if (old) entries.delete(old[0]); else return Promise.reject(unavailable(now()+1000));
        }
        job={subject,controller:new AbortController(),settled:false};
        job.promise=new Promise((resolve,reject)=>{job.resolve=resolve;job.reject=reject;});
        entries.set(subject,job); queue.push(job); pump();
      }
      // Aborting one card must not cancel another card's shared subject request.
      job.users=(job.users || 0)+1;
      return new Promise((resolve,reject)=>{
        let finished=false;
        const finish=()=>{
          if(finished){return;}
          finished=true;job.users--;signal?.removeEventListener('abort',abort);
          if(!job.users && !job.settled && entries.get(subject)===job){
            entries.delete(subject);const index=queue.indexOf(job);if(index>=0)queue.splice(index,1);
            job.controller.abort();job.reject(new DOMException('Aborted','AbortError'));
          }
        };
        const abort=()=>{reject(new DOMException('Aborted','AbortError'));finish();};
        signal?.addEventListener('abort',abort,{once:true});
        job.promise.then(()=>{
          if(entries.get(subject)!==job)reject(new Error('Reviews invalidated'));
          else if(!signal?.aborted)resolve(structuredClone(job.value));
          finish();
        },error=>{reject(error);finish();});
      });
    },
    invalidate,
    dispose() { disposed=true;invalidate();queue.length=0; },
  };
}

export function observeSummaryInvalidation(reader, callback, {target=window, doc=document}={}) {
  const changed=event=>{
    const subjectId=validSubject(event.detail?.subjectId) ? event.detail.subjectId : undefined;
    reader.invalidate(subjectId);callback({subjectId,reason:'change'});
  };
  const navigation=()=>{reader.invalidate();callback({reason:'navigation'});};
  const restored=()=>{if(doc.visibilityState!=='hidden' && reader.isStale()){
    reader.invalidate();callback({reason:'restoration'});
  }};
  const pageshow=event=>{if(event.persisted)navigation();};
  target.addEventListener('focus',restored);target.addEventListener('pageshow',pageshow);
  target.addEventListener('popstate',navigation);doc.addEventListener('visibilitychange',restored);
  doc.addEventListener('cartray:reviews-changed',changed);
  return ()=>{target.removeEventListener('focus',restored);target.removeEventListener('pageshow',pageshow);
    target.removeEventListener('popstate',navigation);doc.removeEventListener('visibilitychange',restored);
    doc.removeEventListener('cartray:reviews-changed',changed);};
}

export async function reviewsPage(subject, cursor = null, snapshot = null, fetchImpl = fetch, {signal} = {}) {
  if (!validSubject(subject) || (cursor !== null && (!/^[A-Za-z0-9_-]{1,130}$/.test(cursor) || typeof snapshot !== 'string'))) throw new Error('Invalid review selection');
  const query = new URLSearchParams({ limit: '10' });
  if (cursor !== null) { query.set('after', cursor); query.set('snapshot', snapshot); }
  const controller = new AbortController();
  const stop=()=>controller.abort(); signal?.addEventListener("abort",stop,{once:true});
  if(signal?.aborted)controller.abort();
  const timer = setTimeout(stop, 5000);
  try {
    const base = `/reviews/v1/subjects/${encodeURIComponent(subject)}`;
    const [summary, page] = await Promise.all([
      readJson(`${base}/summary`, fetchImpl, controller.signal),
      readJson(`${base}/contributions?${query}`, fetchImpl, controller.signal),
    ]);
    if (summary.subject_id !== subject || page.subject_id !== subject || typeof summary.snapshot_id !== 'string' || page.snapshot_id !== summary.snapshot_id ||
        (snapshot !== null && snapshot !== summary.snapshot_id) || !Array.isArray(page.contributions) || page.contributions.length > 10 ||
        typeof page.has_more !== 'boolean' || (page.has_more && !/^[A-Za-z0-9_-]{1,130}$/.test(page.next_after || '')) ||
        !summary.rating || !Number.isSafeInteger(summary.rating.rated_count) || !Number.isSafeInteger(summary.contribution_count) ||
        !(summary.rating.average === null || (typeof summary.rating.average === 'number' && summary.rating.average >= 1 && summary.rating.average <= 5))) throw new Error('Reviews changed');
    validatedSummary(summary,subject);
    return { summary, page };
  } finally { clearTimeout(timer); signal?.removeEventListener("abort",stop); controller.abort(); }
}

export function reviewSummaryText(summary) {
  validatedSummary(summary,summary?.subject_id);
  const count=summary.contribution_count, rating=summary.rating;
  const contributions=`${count} ${count===1?'contribution':'contributions'}`;
  const label=rating.rated_count===1?'rating':'ratings';
  if (!rating.rated_count)return `No ratings yet · ${contributions}`;
  return `${rating.average} out of 5 · ${rating.rated_count} ${label} · ${contributions}`;
}

function line(doc, tag, text, parent, className="") { const node = doc.createElement(tag); node.className=className; node.textContent = text; parent.append(node); return node; }
function decorate(parent, renderer, values, doc, className, customClass) {
  if (typeof renderer !== 'function') return;
  const visual = doc.createElement('span');
  visual.className = className;
  visual.setAttribute('aria-hidden', 'true');
  // Merchant presentation is optional; failure must retain the numeric fallback.
  try { renderer(visual, Object.freeze(values), doc); } catch { return; }
  if (!visual.hasChildNodes()) return;
  parent.append(visual);
  parent.className += ' ' + customClass;
}

export function renderContribution(item, parent, doc, budget, {renderRating} = {}) {
  // The public API caps historical contributions at 200 and returns at most
  // one native staff reply per result. An explicit stack handles deep trees.
  const pending = [{ item, parent, root: true }];
  while (pending.length) {
    const next = pending.pop(), value = next.item;
    if (!value || typeof value !== 'object' || typeof value.body !== 'string' || typeof value.author !== 'string' || !Array.isArray(value.replies) || --budget.remaining < 0 || value.replies.length + pending.length > budget.remaining) throw new Error('Invalid contribution');
    const node = doc.createElement(next.root ? 'article' : 'blockquote');
    node.className = next.root ? 'cr-review' : 'cr-review-reply';
    if (next.root) {
      if (typeof value.title === 'string' && value.title) line(doc, 'h3', value.title, node);
      const stars=Number.isInteger(value.rating) && value.rating>=1 && value.rating<=5 ? ` · ${value.rating} of 5 stars` : '';
      const author = line(doc, 'p', value.author || 'Anonymous', node, 'cr-review-author');
      if (stars) {
        const rating = line(doc, 'span', '', author, 'cr-review-rating');
        line(doc, 'span', stars, rating, 'cr-review-rating-text');
        decorate(rating, renderRating, {rating:value.rating, maxRating:5}, doc,
          'cr-review-rating-visual', 'cr-review-rating--custom');
      }
      if (value.verification === 'cartray_verified_acquisition') line(doc, 'p', 'Verified purchase', node, 'cr-review-verification');
      line(doc, 'p', value.body, node, 'cr-review-body');
    } else {
      line(doc, 'p', value.body, node, 'cr-review-reply-body'); line(doc, 'footer', value.author || 'Store', node, 'cr-review-reply-author');
    }
    next.parent.append(node);
    for (let i = value.replies.length - 1; i >= 0; i--) pending.push({ item: value.replies[i], parent: node, root: false });
  }
}

export function attachReviews(config, doc = document, {renderRating, renderSummary} = config?.reviewPresentation ?? {}) {
  const subject = config?.reviewSubjects?.[config.productKey];
  if (!config?.reviewsEnabled || !subject || !validSubject(subject)) return;
  const section = doc.querySelector('#reviews'), status = doc.querySelector('#reviews-status');
  const list = doc.querySelector('#review-list'), more = doc.querySelector('#reviews-more');
  if (!section || !status || !list || !more) return;
  section.hidden = false;
  more.className=`${more.className || ''} cr-button cr-button--secondary`.trim();
  function announce(state, summary=null) {
    if (typeof section.dispatchEvent==='function' && typeof CustomEvent!=='undefined')
      section.dispatchEvent(new CustomEvent('cartray:reviews-snapshot',{bubbles:true,detail:{subjectId:subject,state,summary:summary?structuredClone(summary):null}}));
  }
  let cursor = null, snapshot = null, busy = false, generation=0, requestController=null, lastRead=0;
  async function load() {
    if (busy) return;
    const selected=generation; requestController=new AbortController();
    busy = true; more.disabled = true; status.textContent = 'Loading reviews…';
    if(cursor===null)announce('loading');
    try {
      const result = await reviewsPage(subject, cursor, snapshot, fetch, {signal:requestController.signal});
      if(selected!==generation){return;}
      lastRead=Date.now();
      const fragment = doc.createDocumentFragment();
      const budget = { remaining: 220 }; // 200 historical + 10 native reviews + their replies.
      for (const item of result.page.contributions) renderContribution(item, fragment, doc, budget, {renderRating});
      list.append(fragment);
      announce('ready',result.summary);
      status.textContent = '';
      const summary = line(doc, 'span', '', status, 'cr-review-summary');
      line(doc, 'span', reviewSummaryText(result.summary), summary, 'cr-review-summary-text');
      decorate(summary, renderSummary, {average:result.summary.rating.average,
        ratedCount:result.summary.rating.rated_count, contributionCount:result.summary.contribution_count,
        maxRating:5}, doc, 'cr-review-summary-visual', 'cr-review-summary--custom');
      cursor = result.page.next_after; snapshot = result.summary.snapshot_id;
      more.hidden = !result.page.has_more;
    } catch {
      if(selected!==generation)return;
      // A stale snapshot invalidates all previously displayed text, not just the new page.
      list.replaceChildren(); cursor = null; snapshot = null; more.hidden = true; announce('unavailable');
      status.textContent = 'Reviews are temporarily unavailable. Please refresh to try again.';
    } finally { if(selected===generation){busy = false; more.disabled = false;} }
  }
  const click=()=>{void load();}; more.addEventListener('click',click);
  const refresh=()=>{generation++;requestController?.abort();busy=false;cursor=null;snapshot=null;list.replaceChildren();void load();};
  const changed=event=>{if(!event.detail?.subjectId || event.detail.subjectId===subject)refresh();};
  const restore=()=>{if(!busy && Date.now()-lastRead>=30000)refresh();};
  const pageshow=event=>{if(event.persisted)refresh();};
  doc.addEventListener?.('cartray:reviews-changed',changed);
  doc.defaultView?.addEventListener('focus',restore);doc.defaultView?.addEventListener('pageshow',pageshow);
  void load();
  return {refresh,dispose(){generation++;requestController?.abort();more.removeEventListener?.('click',click);
    doc.removeEventListener?.('cartray:reviews-changed',changed);doc.defaultView?.removeEventListener('focus',restore);doc.defaultView?.removeEventListener('pageshow',pageshow);}};
}

if (typeof document !== 'undefined') attachReviews(window.CARTRAY_STOREFRONT);
