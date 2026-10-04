#!/usr/bin/env bash
# A guided tour of the on-prem cluster. Assumes the apiserver is up (make up) and at
# least one kubelet is running (make node).
set -euo pipefail
here="$(cd "$(dirname "$0")/.." && pwd)"
sk="$here/skctl"
say(){ printf '\n\033[1;32m>>> %s\033[0m\n' "$*"; }
say "Applying lab/hello-web.json (web x2, hello x1)"
"$sk" apply "$here/lab/hello-web.json"
say "Deployments"; "$sk" get deployments
say "Give the kubelet a few seconds to place & run pods..."; sleep 12
say "Pods (should be Running across your nodes)"; "$sk" get pods
say "Nodes"; "$sk" get nodes
say "Scaling web to 4"; "$sk" scale web 4; sleep 12; "$sk" get pods

say "SheetGate: one gateway on :8080, host/path routes into the cluster (lab/sheetgate.json)"
"$sk" apply "$here/lab/sheetgate.json"
"$sk" get routes
say "Waiting for the gateway to be Programmed..."
for _ in $(seq 1 30); do "$sk" get gateways | grep -q Programmed && break; sleep 3; done
"$sk" get gateways
say "curl -H 'Host: hello.localhost' http://localhost:8080/"
curl -s -H 'Host: hello.localhost' http://localhost:8080/ || true
say "curl http://localhost:8080/hello   (path route, prefix rewritten to /)"
curl -s http://localhost:8080/hello || true
say "Done. The spreadsheet is the cluster — and now it has a front door. It reconciles."
