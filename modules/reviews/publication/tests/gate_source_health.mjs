// Probe only the authenticated, synthetic source diagnostic until the exact source route is visible to the gate.
const [base] = process.argv.slice(2), token = process.env.PUBLICATION_TEST_TOKEN;
const deadline = Date.now() + 30_000, attempts = [];
while (Date.now() < deadline) {
  try {
    const response = await fetch(new URL('/__test/source-diagnostic', base), { method: 'POST', headers: { 'Authorization': `Bearer ${token}`, 'Content-Type': 'application/json' }, body: '{}', signal: AbortSignal.timeout(8000) });
    const body = await response.json();
    attempts.push({ elapsed_ms: 30_000 - Math.max(0, deadline - Date.now()), status: response.status, result: response.ok ? 'ok' : body.error });
    if (response.status === 200 && body.lookup?.subject_id === 'example-policy') { console.log(JSON.stringify({ ready: true, attempts })); process.exit(0); }
  } catch (error) { attempts.push({ elapsed_ms: 30_000 - Math.max(0, deadline - Date.now()), error: error.name }); }
  await new Promise((resolve) => setTimeout(resolve, Math.min(2000, Math.max(0, deadline - Date.now()))));
}
console.log(JSON.stringify({ ready: false, attempts })); process.exitCode = 1;
