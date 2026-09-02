#!/usr/bin/env bash
#
# Sheeternetes kubelet — runs on any Docker host and turns it into a "node".
# It heartbeats the Apps Script apiserver, receives the pods it should run,
# and converges local Docker state to match. Requires: bash, curl, jq, docker.
#
# Usage:
#   WEBAPP_URL=https://script.google.com/macros/s/XXXX/exec \
#   TOKEN=CHANGE_ME_super_secret \
#   NODE_NAME=node-a CPU_TOTAL=4000 MEM_TOTAL=8192 \
#   ./kubelet.sh
#
set -euo pipefail

# On-prem: load WEBAPP_URL/TOKEN from a local .skctl.env if present (points at the
# local apiserver, e.g. http://localhost:8787) so `make` targets stay one-liners.
here="$(cd "$(dirname "$0")" && pwd)"
[ -f "$here/.skctl.env" ] && . "$here/.skctl.env"

WEBAPP_URL="${WEBAPP_URL:?set WEBAPP_URL (local apiserver URL, e.g. http://localhost:8787)}"
TOKEN="${TOKEN:-CHANGE_ME_super_secret}"
NODE_NAME="${NODE_NAME:-$(hostname)}"
# Linux: hostname -I; macOS: ipconfig; fall back to loopback if both are empty.
NODE_IP="${NODE_IP:-$(hostname -I 2>/dev/null | awk '{print $1}')}"
NODE_IP="${NODE_IP:-$(ipconfig getifaddr en0 2>/dev/null || true)}"
NODE_IP="${NODE_IP:-127.0.0.1}"
CPU_TOTAL="${CPU_TOTAL:-$(( $(nproc 2>/dev/null || echo 2) * 1000 ))}"   # millicores
MEM_TOTAL="${MEM_TOTAL:-2048}"                                            # MiB
INTERVAL="${INTERVAL:-10}"                                                # seconds

echo "[kubelet] node=$NODE_NAME ip=$NODE_IP cpu=${CPU_TOTAL}m mem=${MEM_TOTAL}Mi -> $WEBAPP_URL"

# Sheetlium: a shared Docker network so pods can reach each other, and each
# deployment name becomes a round-robin DNS alias == a Service (single-daemon;
# real multi-host would need an overlay network).
SK_NET="${SK_NET:-sheeternetes}"
docker network inspect "$SK_NET" >/dev/null 2>&1 || docker network create "$SK_NET" >/dev/null 2>&1 || true
SK_SECRET_DIR="${SK_SECRET_DIR:-${TMPDIR:-/tmp}/sk-secrets}"; mkdir -p "$SK_SECRET_DIR"

# name of the docker container backing a pod
cname() { echo "sk_$1"; }

while true; do
  # 1) report the pods we currently run (only ours, by node label)
  running_json="$(
    docker ps --filter "label=sheeternetes.node=$NODE_NAME" \
      --format '{{.Label "sheeternetes.pod"}}|{{.ID}}' \
    | jq -R -s 'split("\n") | map(select(length>0) | split("|"))
                | map({name: .[0], phase: "Running", container_id: .[1]})'
  )"
  [ -z "$running_json" ] && running_json='[]'

  # 2) heartbeat + fetch desired state
  resp="$(curl -fsSL -m 30 -X POST "$WEBAPP_URL" \
    -H 'Content-Type: application/json' \
    -d "$(jq -n \
          --arg t "$TOKEN" --arg n "$NODE_NAME" --arg ip "$NODE_IP" \
          --argjson cpu "$CPU_TOTAL" --argjson mem "$MEM_TOTAL" \
          --argjson pods "$running_json" \
          '{token:$t, node:$n, ip:$ip, cpu_total:$cpu, mem_total:$mem, pods:$pods}')" \
    || echo '{"pods":[]}')"

  desired="$(echo "$resp" | jq -c '.pods // []')"

  # 3) converge: start Running that are missing, remove Terminating.
  # Read the pod stream on FD 3 so docker commands in the body can't drain it.
  while read -r pod <&3; do
    name="$(echo "$pod"  | jq -r '.name')"
    want="$(echo "$pod"  | jq -r '.desired')"
    cn="$(cname "$name")"

    if [ "$want" = "Running" ]; then
      if ! docker ps -q -f "name=^${cn}$" | grep -q .; then
        image="$(echo "$pod" | jq -r '.image')"
        cmd="$(echo "$pod"   | jq -r '.command // ""')"
        cpu="$(echo "$pod"   | jq -r '.cpu_req // 100')"
        mem="$(echo "$pod"   | jq -r '.mem_req // 64')"
        deploy="$(echo "$pod" | jq -r '.deployment // empty')"
        # SICF native: an image stored IN the spreadsheet. Resolve it to a local
        # docker image (fetch layers from the apiserver, verify digests, docker load).
        case "$image" in
          sicf:*) image="$(python3 "$here/sicf.py" "$WEBAPP_URL" "$TOKEN" "$image")" \
                    || { echo "[kubelet] FAILED to resolve $image"; continue; } ;;
        esac
        # millicores -> docker's fractional --cpus, locale-independent (no awk)
        cpus="$((cpu / 1000)).$(printf '%03d' "$((cpu % 1000))")"
        # Sheetlium: join the shared net; alias = deployment name (the Service)
        net_args=(--network "$SK_NET")
        [ -n "$deploy" ] && net_args+=(--network-alias "$deploy")
        # env: comma-separated K=V pairs from the Deployment
        env_args=()
        env_spec="$(echo "$pod" | jq -r '.env // ""')"
        if [ -n "$env_spec" ] && [ "$env_spec" != "null" ]; then
          oldIFS="$IFS"; IFS=','
          for kv in $env_spec; do [ -n "$kv" ] && env_args+=(-e "$kv"); done
          IFS="$oldIFS"
        fi
        # secret_files: "secretName:/path,..." -> fetch Secret data, write to a node file, mount ro
        sec_args=()
        sec_spec="$(echo "$pod" | jq -r '.secret_files // ""')"
        if [ -n "$sec_spec" ] && [ "$sec_spec" != "null" ]; then
          secrets_json="$(curl -fsSL -m 30 "$WEBAPP_URL?token=$TOKEN&kind=secrets" 2>/dev/null || echo '{"items":[]}')"
          oldIFS="$IFS"; IFS=','
          for m in $sec_spec; do
            sname="${m%%:*}"; spath="${m#*:}"
            [ -z "$sname" ] || [ "$sname" = "$spath" ] && continue
            data="$(echo "$secrets_json" | jq -r --arg n "$sname" '.items[]|select(.name==$n)|.data')"
            [ -z "$data" ] || [ "$data" = "null" ] && { echo "[kubelet] secret '$sname' not found"; continue; }
            sfile="$SK_SECRET_DIR/${name}.${sname}"
            printf '%s' "$data" | base64 -d > "$sfile" 2>/dev/null || echo "[kubelet] bad base64 for secret $sname"
            sec_args+=(-v "$sfile:$spath:ro")
          done
          IFS="$oldIFS"
        fi
        echo "[kubelet] run $name ($image)"
        docker rm -f "$cn" >/dev/null 2>&1 || true
        # command (if any) is run through a shell so quoting/loops survive
        if [ -n "$cmd" ] && [ "$cmd" != "null" ]; then
          docker run -d --name "$cn" \
            --label sheeternetes=1 --label "sheeternetes.pod=$name" \
            --label "sheeternetes.node=$NODE_NAME" \
            "${net_args[@]}" ${env_args[@]+"${env_args[@]}"} ${sec_args[@]+"${sec_args[@]}"} \
            --cpus "$cpus" --memory "${mem}m" \
            "$image" sh -c "$cmd" >/dev/null || echo "[kubelet] FAILED to start $name"
        else
          docker run -d --name "$cn" \
            --label sheeternetes=1 --label "sheeternetes.pod=$name" \
            --label "sheeternetes.node=$NODE_NAME" \
            "${net_args[@]}" ${env_args[@]+"${env_args[@]}"} ${sec_args[@]+"${sec_args[@]}"} \
            --cpus "$cpus" --memory "${mem}m" \
            "$image" >/dev/null || echo "[kubelet] FAILED to start $name"
        fi
      fi
    elif [ "$want" = "Terminating" ]; then
      if docker ps -aq -f "name=^${cn}$" | grep -q .; then
        echo "[kubelet] stop $name"
        docker rm -f "$cn" >/dev/null 2>&1 || true
      fi
    fi
  done 3< <(echo "$desired" | jq -c '.[]')

  # 4) garbage-collect pods no longer desired at all (only ours)
  desired_names="$(echo "$desired" | jq -r '.[].name')"
  while read -r have <&3; do
    [ -z "$have" ] && continue
    if ! echo "$desired_names" | grep -qx "$have"; then
      echo "[kubelet] gc $have"
      docker rm -f "$(cname "$have")" >/dev/null 2>&1 || true
    fi
  done 3< <(docker ps --filter "label=sheeternetes.node=$NODE_NAME" --format '{{.Label "sheeternetes.pod"}}')

  sleep "$INTERVAL"
done
