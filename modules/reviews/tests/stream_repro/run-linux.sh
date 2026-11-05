#!/usr/bin/env bash
# Run from any directory; requires Docker and network access for pinned npm packages.
set -euo pipefail
root=$(git rev-parse --show-toplevel)
docker run --rm -v "$root:/src:ro" node:24.20.0-bookworm bash -c '
set -eu
mkdir /tmp/repro
cd /tmp/repro
cp /src/package.json /src/package-lock.json .
npm ci --ignore-scripts --silent
cp /src/modules/reviews/tests/stream_repro/* .
sed -i s/127.0.0.1:8789/127.0.0.1:8791/ python.capnp
node_modules/.bin/workerd --version
node_modules/.bin/workerd serve workerd.capnp > /tmp/js-direct.log 2>&1 &
node_modules/.bin/workerd serve python.capnp > /tmp/python-direct.log 2>&1 &
node_modules/.bin/wrangler dev --config wrangler.toml --local --port 8790 --inspector-port 9230 --log-level info > /tmp/js-proxy.log 2>&1 &
node_modules/.bin/wrangler dev --config python.toml --local --port 8792 --inspector-port 9232 --log-level info > /tmp/python-proxy.log 2>&1 &
for entry in js-direct:8789 python-direct:8791 js-proxy:8790 python-proxy:8792; do
  name=${entry%:*}; port=${entry#*:}
  ready=0
  for i in $(seq 1 120); do
    if curl -fsS "http://127.0.0.1:$port/" >/dev/null 2>&1; then ready=1; break; fi
    sleep 1
  done
  echo "=== $name ($port) ==="
  if [ "$ready" = 1 ]; then python3 probe.py "$port" || true; else echo "listener failed to start"; fi
  cat "/tmp/$name.log"
done
'
