"""Create, qualify and delete isolated synthetic Cloudflare resources.

The source Worker replays a captured local R4 public projection; it is not a
hosted deployment of the R4 Python runtime. No production resource is used.
"""
import hashlib
import json
import os
import re
import secrets
import signal
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
MODULE = ROOT / 'modules/reviews/publication'
WRANGLER = str(ROOT / 'node_modules/.bin/wrangler')


def command(args, *, env, cwd, stdin=None, timeout=180):
    result = subprocess.run([WRANGLER, *args], cwd=cwd, env=env, input=stdin, text=True, capture_output=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError(f'wrangler {args[0]} failed; sanitized diagnostics retained in temporary directory')
    return result.stdout if '--json' in args else result.stdout + result.stderr


def main():
    if len(sys.argv) != 2:
        raise SystemExit('usage: hosted_qualification.py <captured-synthetic-r4-projection.json>')
    fixture_path = Path(sys.argv[1]); fixture = json.loads(fixture_path.read_text())
    if fixture['subject']['id'] != 'example-policy' or fixture['summary']['snapshot_id'] != 'one' or fixture['summary']['contribution_count'] != 3 or {item['id'] for item in fixture['pages'][0]['contributions']} != {'r', 'r2', 'u'}:
        raise RuntimeError('unexpected fixture; synthetic R4 data required')
    expected = json.loads((ROOT / 'modules/reviews/tests/fixtures/approved-synthetic.json').read_text())
    expected_rows = {item['id']: (item['author'], item['body'], item['rating']) for item in expected['contributions']}
    def flatten(items):
        return [nested for item in items for nested in [item, *flatten(item.get('replies', []))]]
    actual_rows = {item['id']: (item['author'], item['body'], item['rating']) for item in flatten(fixture['pages'][0]['contributions'])}
    if actual_rows != expected_rows: raise RuntimeError('fixture content does not match the checked-in synthetic source')
    suffix = secrets.token_hex(6); source_name = f'r5-src-{suffix}'; gate_name = f'r5-gate-{suffix}'; database_name = f'r5-db-{suffix}'
    token = secrets.token_hex(32); resources = {'source': False, 'gate': False, 'database': False}
    with tempfile.TemporaryDirectory(prefix='r5-hosted-qualification-') as temporary:
        temp = Path(temporary); env = os.environ.copy()
        identity = command(['whoami'], env=env, cwd=ROOT)
        login = re.search(r'associated with the email ([^\s.]+(?:\.[^\s.]+)+)', identity, re.I)
        if not login: raise RuntimeError('cannot identify signed-in personal account')
        rows = [line for line in identity.splitlines() if login.group(1).lower() in line.lower() and re.search(r'\b[0-9a-f]{32}\b', line)]
        if len(rows) != 1: raise RuntimeError('personal account is ambiguous')
        env['CLOUDFLARE_ACCOUNT_ID'] = re.search(r'\b[0-9a-f]{32}\b', rows[0]).group(0)
        source_config = temp / 'source.toml'; gate_config = temp / 'gate.toml'
        source_url = None; receipt = None; cleanup_errors = []
        try:
            resources['database'] = True
            output = command(['d1', 'create', database_name], env=env, cwd=ROOT)
            found = re.search(r'database_id\s*=\s*["\']([0-9a-f-]{36})["\']', output)
            if not found: raise RuntimeError('could not identify newly created test database')
            database_id = found.group(1)
            shutil.copyfile(MODULE / 'tests/synthetic_source.mjs', temp / 'synthetic_source.mjs')
            (temp / 'synthetic_source_data.mjs').write_text('export default ' + json.dumps(fixture, separators=(',', ':')) + ';\n')
            source_config.write_text(f'name = "{source_name}"\nmain = "synthetic_source.mjs"\ncompatibility_date = "2026-08-31"\nworkers_dev = true\n')
            resources['source'] = True
            output = command(['deploy', '--config', str(source_config)], env=env, cwd=temp)
            found = re.search(r'https://r5-src-[a-z0-9-]+\.[a-z0-9-]+\.workers\.dev', output)
            if not found: raise RuntimeError('source Worker URL unavailable')
            source_url = found.group(0)
            shutil.copyfile(MODULE / 'worker.mjs', temp / 'worker.mjs')
            shutil.copyfile(MODULE / 'projection.mjs', temp / 'projection.mjs')
            gate_config.write_text(f'''name = "{gate_name}"\nmain = "worker.mjs"\ncompatibility_date = "2026-08-31"\nworkers_dev = true\n[vars]\nPUBLICATION_MODE = "test"\nPUBLICATION_ENVIRONMENT = "test"\nPUBLICATION_CANONICAL_ORIGIN = "https://example.invalid"\nREVIEWS_PUBLIC_ORIGIN = "{source_url}"\nREVIEWS_SOURCE_SCOPE = "example-store:test"\n[[d1_databases]]\nbinding = "PUBLICATION_DB"\ndatabase_name = "{database_name}"\ndatabase_id = "{database_id}"\n''')
            command(['d1', 'execute', database_name, '--remote', '--config', str(gate_config), '--file', str(MODULE / 'schema.sql')], env=env, cwd=temp)
            resources['gate'] = True
            output = command(['deploy', '--config', str(gate_config)], env=env, cwd=temp)
            found = re.search(r'https://r5-gate-[a-z0-9-]+\.[a-z0-9-]+\.workers\.dev', output)
            if not found: raise RuntimeError('publication Worker URL unavailable')
            gate_url = found.group(0)
            command(['secret', 'put', 'PUBLICATION_TEST_TOKEN', '--config', str(gate_config)], env=env, cwd=temp, stdin=token)
            started = datetime.now(timezone.utc).isoformat()
            result = subprocess.run(['node', str(MODULE / 'tests/qualify.mjs'), gate_url, str(fixture_path)], cwd=ROOT, env=env | {'PUBLICATION_TEST_TOKEN': token, 'REVIEWS_PUBLIC_ORIGIN': source_url}, capture_output=True, text=True, timeout=120, check=True)
            measurement = json.loads(result.stdout.strip().splitlines()[-1])
            state = command(['d1', 'execute', database_name, '--remote', '--config', str(gate_config), '--json', '--command', "SELECT (SELECT count(*) FROM publication_media_tombstones) media_count, (SELECT count(*) FROM publication_tombstones) lineage_count, (SELECT state FROM publication_gate WHERE singleton=1) gate_state"], env=env, cwd=temp)
            actual = json.loads(state)[0]['results'][0]
            if actual != {'media_count': 1, 'lineage_count': 2, 'gate_state': 'safe'}: raise RuntimeError('hosted tombstone assertion failed')
            receipt = {'date_utc': started, 'source_commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(), 'fixture_sha256': hashlib.sha256(fixture_path.read_bytes()).hexdigest(), 'publication_worker_sha256': hashlib.sha256((MODULE / 'worker.mjs').read_bytes()).hexdigest(), 'projection_module_sha256': hashlib.sha256((MODULE / 'projection.mjs').read_bytes()).hexdigest(), 'source_kind': 'synthetic replay of local R4 public projection', 'database_assertions': actual, 'measurements': measurement}
        finally:
            for key, args in [('gate', ['delete', gate_name, '--force']), ('source', ['delete', source_name, '--force']), ('database', ['d1', 'delete', database_name, '--skip-confirmation'])]:
                if resources[key]:
                    try: command(args, env=env, cwd=temp)
                    except Exception: cleanup_errors.append(f'{key}: {dict(source=source_name, gate=gate_name, database=database_name)[key]}')
            if cleanup_errors: raise RuntimeError('disposable resource cleanup failed: ' + ', '.join(cleanup_errors))
        if receipt:
            receipt['cleanup'] = 'all disposable resources deleted'
            output = ROOT / 'modules/reviews/publication/hosted-receipt.json'
            output.write_text(json.dumps(receipt, indent=2) + '\n')
            print(json.dumps(receipt, sort_keys=True))


if __name__ == '__main__':
    def interrupt(_signum, _frame):
        raise KeyboardInterrupt()
    signal.signal(signal.SIGTERM, interrupt)
    main()
