// A newly deployed workers.dev route was observed returning an edge error before becoming ready.
// Poll for the exact synthetic build only, with a fixed 30-second diagnostic deadline.
const [url, build] = process.argv.slice(2);
const deadline = Date.now() + 30_000, attempts = [];
while (Date.now() < deadline) {
  try {
    const response = await fetch(url, { signal: AbortSignal.timeout(5000) });
    const body = await response.text();
    let parsed = null;
    try { parsed = JSON.parse(body); } catch { /* edge responses need not be JSON */ }
    attempts.push({ elapsed_ms: 30_000 - Math.max(0, deadline - Date.now()), status: response.status, marker: response.headers.get('x-synthetic-source') });
    if (response.status === 200 && response.headers.get('x-synthetic-source') === 'r5-fixture' && parsed?.service === 'r5-synthetic-source' && parsed.build === build) {
      console.log(JSON.stringify({ ready: true, build, attempts })); process.exit(0);
    }
  } catch (error) { attempts.push({ elapsed_ms: 30_000 - Math.max(0, deadline - Date.now()), error: error.name }); }
  await new Promise((resolve) => setTimeout(resolve, Math.min(2000, Math.max(0, deadline - Date.now()))));
}
console.log(JSON.stringify({ ready: false, build, attempts })); process.exitCode = 1;
