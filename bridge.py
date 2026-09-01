#!/usr/bin/env python3
"""
Sheetmesh bridge — federate an on-prem (Excel/LibreOffice) cluster with a
Google Sheets cluster. On-prem nodes can't be reached inbound, so the bridge
dials OUT: it reads the local apiserver and publishes a snapshot to a rendezvous
endpoint (a Google Sheets Apps Script web app, or another node), and pulls the
peer's services back — giving cross-substrate service discovery and migration.

  # federated service view (+ --push to publish local deployments to the peer)
  python3 bridge.py status  --local http://localhost:8787 --local-token secret \
                            --peer https://script.google.com/macros/s/XXXX/exec --peer-token secret2

  # live-migrate a deployment across substrates (make-before-break, --rollback-window auto-reverts)
  python3 bridge.py migrate web --from local --to peer --rollback-window 30 \
                            --local http://localhost:8787 --local-token secret \
                            --peer https://script.google.com/macros/s/XXXX/exec --peer-token secret2

  # keep both substrates federated (daemon): union-reconcile deployments every 60s
  python3 bridge.py sync --interval 60 \
                            --local http://localhost:8787 --local-token secret \
                            --peer https://script.google.com/macros/s/XXXX/exec --peer-token secret2

Requires only the standard library (urllib). Trust: a shared token, plus optional HMAC
signing — pass --local-signing-key / --peer-signing-key to sign POSTs so a peer running
with SIGNING_KEY set accepts them (tamper- and replay-resistant).
"""
import argparse, hashlib, hmac, json, time, urllib.request, urllib.parse, urllib.error

def sign(key, ts, body):
    """HMAC-SHA256 over 'timestamp.body' — must match apiserver.sign byte-for-byte."""
    if isinstance(body, str): body = body.encode()
    return hmac.new(key.encode(), str(ts).encode() + b"." + body, hashlib.sha256).hexdigest()

def get(base, token, kind):
    url = base + ("&" if "?" in base else "?") + urllib.parse.urlencode({"token": token, "kind": kind})
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.loads(r.read()).get("items", [])

def post(base, payload, key="", now_fn=lambda: int(time.time())):
    body = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json"}
    if key:                                            # sign the request (anti-tamper/replay)
        ts = str(now_fn())
        headers["X-SNCF-Timestamp"] = ts
        headers["X-SNCF-Signature"] = sign(key, ts, body)
    req = urllib.request.Request(base, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode()

# ---------------------------------------------------------------- operations

def federated_view(local, peer):
    """Merge two clusters' deployments into one name->home service map."""
    mesh = {d.get("name"): "local" for d in local}
    for d in peer: mesh.setdefault(d.get("name"), "peer")
    return mesh

def plan_sync(local, peer):
    """Pure: return (to_local, to_peer) — the deployments each side is missing from
    the other. Union reconciliation; existing deployments are left untouched (the
    owning side stays authoritative, so this never fights a rename or a scale)."""
    ln = {d.get("name") for d in local}
    pn = {d.get("name") for d in peer}
    to_local = [d for d in peer if d.get("name") not in ln]
    to_peer  = [d for d in local if d.get("name") not in pn]
    return to_local, to_peer

def sync_once(local, peer, *, get_fn=get, post_fn=post, log=print):
    """One reconciliation pass: push each side's missing deployments to the other."""
    ld = get_fn(local["base"], local["token"], "deployments")
    pd = get_fn(peer["base"], peer["token"], "deployments")
    to_local, to_peer = plan_sync(ld, pd)
    if to_local:
        post_fn(local["base"], {"token": local["token"], "action": "apply", "deployments": to_local})
    if to_peer:
        post_fn(peer["base"], {"token": peer["token"], "action": "apply", "deployments": to_peer})
    log(f"[sync] +{len(to_local)} -> local, +{len(to_peer)} -> peer")
    return {"to_local": [d.get("name") for d in to_local],
            "to_peer": [d.get("name") for d in to_peer]}

def migrate(deploy, src, dst, *, get_fn=get, post_fn=post, wait=120, poll=3,
            skip_wait=False, rollback_window=0, sleep_fn=time.sleep,
            now_fn=time.monotonic, log=print):
    """Live-migrate one deployment from src cluster to dst cluster.

    src/dst are {"base":..,"token":..}. Order is make-before-break:
      1) copy the spec and APPLY it on dst (new replicas come up there);
      2) WAIT until dst reports the deployment's pods Running (up to `wait`s);
      3) only then DELETE it on src (drain the old home);
      4) if rollback_window>0, WATCH dst for that long — should it degrade below the
         replica count, restore the spec on src and remove it from dst (auto rollback).
    If dst never becomes Ready, src is left untouched — no downtime, no data path
    ripped out from under traffic. Pure/injectable so it is unit-testable offline.
    """
    specs = [d for d in get_fn(src["base"], src["token"], "deployments") if d.get("name") == deploy]
    if not specs:
        return {"ok": False, "error": f"deployment '{deploy}' not found on source"}
    spec = specs[0]
    try: replicas = int(float(spec.get("replicas") or 0))
    except (TypeError, ValueError): replicas = 0

    log(f"[migrate] {deploy} (x{replicas}): applying on target…")
    post_fn(dst["base"], {"token": dst["token"], "action": "apply", "deployments": [spec]})

    if not skip_wait and replicas > 0:
        log(f"[migrate] waiting for {deploy} to become Ready on target (≤{wait}s)…")
        deadline = now_fn() + wait
        while now_fn() < deadline:
            pods = get_fn(dst["base"], dst["token"], "pods")
            ready = [p for p in pods if p.get("deployment") == deploy and p.get("phase") == "Running"]
            if len(ready) >= replicas:
                break
            sleep_fn(poll)
        else:
            return {"ok": False, "error": f"timeout: '{deploy}' not Ready on target within {wait}s; "
                                          f"source left intact (no downtime)"}

    log(f"[migrate] target Ready — draining {deploy} from source")
    post_fn(src["base"], {"token": src["token"], "action": "delete", "name": deploy})

    if rollback_window > 0 and replicas > 0 and not skip_wait:
        log(f"[migrate] watching target for {rollback_window}s (rollback armed)…")
        deadline = now_fn() + rollback_window
        while now_fn() < deadline:
            sleep_fn(poll)
            pods = get_fn(dst["base"], dst["token"], "pods")
            ready = [p for p in pods if p.get("deployment") == deploy and p.get("phase") == "Running"]
            if len(ready) < replicas:
                log(f"[migrate] target degraded ({len(ready)}/{replicas}) — rolling back to source")
                post_fn(src["base"], {"token": src["token"], "action": "apply", "deployments": [spec]})
                post_fn(dst["base"], {"token": dst["token"], "action": "delete", "name": deploy})
                return {"ok": False, "rolled_back": True, "deploy": deploy,
                        "error": f"target degraded to {len(ready)}/{replicas}; restored on source"}

    return {"ok": True, "deploy": deploy, "replicas": replicas}

# ---------------------------------------------------------------------- CLI

def _clusters(a):
    return ({"base": a.local, "token": a.local_token, "key": a.local_signing_key},
            {"base": a.peer,  "token": a.peer_token,  "key": a.peer_signing_key})

def _signed_post(clusters):
    """A post() that signs by looking up each cluster's HMAC key by base URL."""
    keys = {c["base"]: c.get("key", "") for c in clusters}
    def _p(base, payload): return post(base, payload, key=keys.get(base, ""))
    return _p

def _endpoints(a):
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument("--local", required=True); p.add_argument("--local-token", default="CHANGE_ME_super_secret")
    p.add_argument("--peer", required=True);  p.add_argument("--peer-token", default="CHANGE_ME_super_secret")
    p.add_argument("--local-signing-key", default=""); p.add_argument("--peer-signing-key", default="")
    return p

def main():
    common = _endpoints(None)
    ap = argparse.ArgumentParser(description="Sheetmesh cross-substrate bridge")
    sub = ap.add_subparsers(dest="cmd")

    s = sub.add_parser("status", parents=[common], help="federated service view")
    s.add_argument("--push", action="store_true", help="publish local deployments to the peer")

    m = sub.add_parser("migrate", parents=[common], help="live-migrate a deployment across substrates")
    m.add_argument("deploy")
    m.add_argument("--from", dest="src", choices=["local", "peer"], default="local")
    m.add_argument("--to", dest="dst", choices=["local", "peer"], default="peer")
    m.add_argument("--wait", type=int, default=120); m.add_argument("--poll", type=int, default=3)
    m.add_argument("--skip-wait", action="store_true", help="don't wait for target readiness (demo w/o kubelets)")
    m.add_argument("--rollback-window", type=int, default=0,
                   help="after cutover, watch the target this many seconds and auto-roll-back if it degrades")

    y = sub.add_parser("sync", parents=[common], help="two-way federation sync (union reconcile)")
    y.add_argument("--interval", type=int, default=0, help="loop every N seconds (0 = one pass)")

    a = ap.parse_args()
    if not a.cmd:
        ap.print_help(); return
    local, peer = _clusters(a)
    spost = _signed_post([local, peer])

    try:
        if a.cmd == "status":
            ld = get(local["base"], local["token"], "deployments")
            pd = get(peer["base"], peer["token"], "deployments")
            print(f"[bridge] local: {len(ld)} deployments · peer: {len(pd)} deployments")
            print("[bridge] federated services:")
            for name, home in sorted(federated_view(ld, pd).items()):
                print(f"  {name:20s} -> {home}")
            if a.push and ld:
                print("[bridge] publishing local deployments to peer…")
                print("  ", spost(peer["base"], {"token": peer["token"], "action": "apply", "deployments": ld}))

        elif a.cmd == "migrate":
            src = local if a.src == "local" else peer
            dst = local if a.dst == "local" else peer
            if a.src == a.dst:
                print("[migrate] --from and --to must differ"); return
            res = migrate(a.deploy, src, dst, post_fn=spost, wait=a.wait, poll=a.poll,
                          skip_wait=a.skip_wait, rollback_window=a.rollback_window)
            print("[migrate] " + ("done: " if res["ok"] else "FAILED: ") + json.dumps(res))

        elif a.cmd == "sync":
            while True:
                sync_once(local, peer, post_fn=spost)
                if not a.interval:
                    break
                time.sleep(a.interval)
    except urllib.error.HTTPError as e:
        detail = "signature/auth rejected — check tokens and --*-signing-key" if e.code == 401 else e.reason
        print(f"[bridge] request failed: HTTP {e.code} — {detail}")
    except urllib.error.URLError as e:
        print(f"[bridge] cannot reach a cluster: {e.reason}")

if __name__ == "__main__":
    main()
