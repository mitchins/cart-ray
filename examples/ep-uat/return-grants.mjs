/** Local UAT grant display; the server checks browser ownership and confirmed payment. */
const key = 'cartray.uat.confirmed-session.v1';
let session = new URLSearchParams(location.search).get('session_id');
if (!session && !new URLSearchParams(location.search).has('checkout') && document.body.dataset.page === 'purchase-return') {
  try {
    const saved = sessionStorage.getItem(key);
    if (/^cs_[A-Za-z0-9_]+$/.test(saved || '')) {
      session = saved;
      history.replaceState(null, '', location.pathname + '?checkout=complete&session_id=' + encodeURIComponent(saved));
    }
  } catch { /* storage is optional; the email remains available */ }
}
let rendered = false;
document.addEventListener('cartray:checkout-return', async event => {
  if (event.detail?.state === 'cancelled' || event.detail?.state === 'expired') {
    try { sessionStorage.removeItem(key); } catch { /* storage is optional */ }
  }
  if (rendered || event.detail?.state !== 'confirmed' || !/^cs_[A-Za-z0-9_]+$/.test(session || '')) return;
  rendered = true;
  try { sessionStorage.setItem(key, session); } catch { /* storage is optional */ }
  try {
    const response = await fetch('/__uat/current-grants?session_id=' + encodeURIComponent(session), {cache:'no-store'});
    if (!response.ok) throw new Error('unavailable');
    const data = await response.json();
    const section = document.createElement('section');section.className='panel';
    const title = document.createElement('h2');title.textContent='Your test downloads';section.append(title);
    for (const grant of data.grants || []) {
      if (!Number.isSafeInteger(grant.source_id)) continue;
      const row=document.createElement('p');
      const link=document.createElement('a');link.href='/__uat/download?source_id='+grant.source_id;
      link.textContent='Download dummy test file' + (grant.title ? ' for ' + grant.title : '');row.append(link);section.append(row);
      for (const value of grant.followups || []) {
        const url=new URL(value);
        if(url.protocol!=='https:'||url.hostname!=='www.dropbox.com'||!/^\/t\/[A-Za-z0-9]+$/.test(url.pathname)) continue;
        const line=document.createElement('p'),a=document.createElement('a');a.href=url.href;
        a.textContent='Open supplied dummy Dropbox follow-up';line.append(a);section.append(line);
      }
    }
    document.querySelector('#content')?.append(section);
  } catch {
    const p=document.createElement('p');p.setAttribute('role','status');
    p.textContent='Your test downloads are temporarily unavailable. Please contact the store.';
    document.querySelector('#content')?.append(p);
  }
});
