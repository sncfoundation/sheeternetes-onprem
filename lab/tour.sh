#!/usr/bin/env bash
# A 30-second guided tour of the on-prem cluster. Assumes the apiserver is up
# (make up) and at least one kubelet is running (make node).
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
say "Done. The spreadsheet is the cluster. It reconciles."
