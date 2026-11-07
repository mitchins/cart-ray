import { ConsoleError } from './client.mjs';

// Separate permission and read-only API: no resend or delivery outcome mutation.
export function setupDeliveries({ client, document, element }) {
  const filter = document.querySelector('#delivery-state'), load = document.querySelector('#delivery-load');
  const more = document.querySelector('#delivery-more'), output = document.querySelector('#delivery-results');
  const status = document.querySelector('#delivery-status'), error = document.querySelector('#delivery-error');
  let loading = false, cursor = null, loadedState = null;
  const seen = new Set();
  function setError(text = '') { error.textContent = text; error.hidden = !text; }
  async function fetchPage(replace) {
    if (loading || (!replace && (!cursor || filter.value !== loadedState))) return;
    const selected = filter.value; loading = true; filter.disabled = load.disabled = more.disabled = true; setError();
    output.setAttribute('aria-busy', 'true');
    try {
      const page = await client.deliveries(selected, replace ? null : cursor);
      if (replace) { output.replaceChildren(); seen.clear(); }
      for (const row of page.records) {
        if (seen.has(row.delivery_identity)) continue;
        seen.add(row.delivery_identity);
        const card = element('article', '', 'card');
        card.append(element('h3', `${row.state} delivery`));
        card.append(element('p', `Delivery reference: ${row.delivery_identity}`, 'meta'));
        card.append(element('p', `Prepared ${new Date(row.prepared_at * 1000).toISOString()}`, 'meta'));
        card.append(element('p', `Purchase reference: ${row.acquisition_id}`, 'meta'));
        const inspect = element('button', 'View provider evidence');
        const evidence = element('div', '', 'meta');
        inspect.addEventListener('click', async () => {
          inspect.disabled = true;
          try {
            const result = await client.deliveryEvidence(row.delivery_identity);
            evidence.replaceChildren();
            evidence.append(element('p', result.provider ? `Provider accepted: ${result.provider.provider_email_id}` : 'No provider acceptance identity recorded.'));
            for (const event of result.activity) evidence.append(element('p', `${event.event_type}: ${event.event_at}`));
            for (const event of result.discoveries) evidence.append(element('p', `Unconfirmed identity ${event.provider_email_id}: ${event.event_type} ${event.event_at}`));
            if (result.truncated) evidence.append(element('p', 'Additional evidence exists; contact an operator.'));
          } catch { evidence.textContent = 'Provider evidence is unavailable. Do not resend without reconciliation.'; }
          finally { inspect.disabled = false; }
        });
        card.append(inspect, evidence);
        output.append(card);
      }
      cursor = page.next_after; loadedState = selected; more.hidden = !cursor;
      if (!output.children.length && !cursor) output.append(element('p', 'No deliveries found in this state.'));
      status.textContent = `Loaded ${page.records.length} ${selected} deliver${page.records.length === 1 ? 'y' : 'ies'}.${cursor ? '' : ' No more records.'}`;
      if (!replace && !cursor) output.focus();
    } catch (reason) {
      const text = reason instanceof ConsoleError && (reason.status === 401 || reason.status === 403)
        ? 'Your staff session or delivery operations permission is unavailable.'
        : reason instanceof ConsoleError ? reason.message : 'Delivery investigation is temporarily unavailable.';
      setError(text);
    } finally { loading = false; filter.disabled = load.disabled = more.disabled = false; output.setAttribute('aria-busy', 'false'); }
  }
  filter.addEventListener('change', () => { more.hidden = true; status.textContent = loadedState ? `Showing ${loadedState} results. Load the selected state to update.` : 'Load the selected state.'; });
  load.addEventListener('click', () => fetchPage(true));
  more.addEventListener('click', () => fetchPage(false));
}
