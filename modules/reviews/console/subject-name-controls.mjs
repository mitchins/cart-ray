// Subject naming is separate from moderation and requires its own permission.
import { ConsoleError } from './client.mjs';

export function setupSubjectNames({ client, document }) {
  const form = document.querySelector('#subject-name-form');
  const id = document.querySelector('#subject-name-id'), name = document.querySelector('#subject-name-value');
  const save = document.querySelector('#subject-name-save'), status = document.querySelector('#subject-name-status');
  const error = document.querySelector('#subject-name-error');
  let busy = false, uncertain = false;
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    if (busy || uncertain) return;
    busy = true; id.disabled = name.disabled = save.disabled = true;
    status.textContent = ''; error.textContent = ''; error.hidden = true;
    try {
      await client.setSubjectName(id.value, name.value);
      status.textContent = 'Customer-facing name saved. Invitations use the updated name.';
    } catch (failure) {
      const code = failure instanceof ConsoleError ? failure.status : 0;
      uncertain = code === 0 || code >= 500;
      error.textContent = uncertain
        ? 'The save outcome is uncertain. Do not submit again until an administrator verifies the current name.'
        : code === 409 ? 'This product reference is unavailable. Check the reference and try again.'
          : failure instanceof ConsoleError ? failure.message : 'Unable to save the name.';
      error.hidden = false;
    } finally {
      busy = false;
      id.disabled = name.disabled = save.disabled = uncertain;
      (uncertain ? error : save).focus();
    }
  });
}
