/** Optional first-party browser context before the shared connected flow starts. */
try {
  if (window.CARTRAY_THEME.contextPath) {
    const response = await fetch(window.CARTRAY_THEME.contextPath, {cache:'no-store'});
    if (!response.ok) throw new Error('context unavailable');
  }
  if (window.CARTRAY_THEME.returnExtension) await import(window.CARTRAY_THEME.returnExtension);
  await import('./app.mjs');
} catch {
  const content = document.querySelector('#content');
  if (content) content.textContent = 'The store is temporarily unavailable. Please try again.';
}
