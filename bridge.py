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

  # live-migrate a deployment across substrates (make-before-break, zero downtime)
  python3 bridge.py migrate web --from local --to peer \
                            --local http://localhost:8787 --local-token secret \
                            --peer https://script.google.com/macros/s/XXXX/exec --peer-token secret2

Requires only the standard library (urllib). Trust via a shared token/HMAC.
"""
import argparse, json, time, urllib.request, urllib.parse

def get(base, token, kind):
    url = base + ("&" if "?" in base else "?") + urllib.parse.urlencode({"token": token, "kind": kind})
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.loads(r.read()).get("items", [])

def post(base, payload):
    req = urllib.request.Request(base, data=json.dumps(payload).encode(),
                                headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode()

# ---------------------------------------------------------------- operations

def federated_view(local, peer):
    """Merge two clusters' deployments into one name->home service map."""
    mesh = {d.get("name"): "local" for d in local}
    for d in peer: mesh.setdefault(d.get("name"), "peer")
    return mesh

def migrate(deploy, src, dst, *, get_fn=get, post_fn=post, wait=120, poll=3,
            skip_wait=False, sleep_fn=time.sleep, now_fn=time.monotonic, log=print):
    """Live-migrate one deployment from src cluster to dst cluster.

    src/dst are {"base":..,"token":..}. Order is make-before-break:
      1) copy the spec and APPLY it on dst (new replicas come up there);
      2) WAIT until dst reports the deployment's pods Running (up to `wait`s);
      3) only then DELETE it on src (drain the old home).
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
    return {"ok": True, "deploy": deploy, "replicas": replicas}

# ---------------------------------------------------------------------- CLI

def _clusters(a):
    return ({"base": a.local, "token": a.local_token},
            {"base": a.peer,  "token": a.peer_token})

def _endpoints(a):
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument("--local", required=True); p.add_argument("--local-token", default="CHANGE_ME_super_secret")
    p.add_argument("--peer", required=True);  p.add_argument("--peer-token", default="CHANGE_ME_super_secret")
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

    a = ap.parse_args()
    if not a.cmd:
        ap.print_help(); return
    local, peer = _clusters(a)

    if a.cmd == "status":
        ld = get(local["base"], local["token"], "deployments")
        pd = get(peer["base"], peer["token"], "deployments")
        print(f"[bridge] local: {len(ld)} deployments · peer: {len(pd)} deployments")
        print("[bridge] federated services:")
        for name, home in sorted(federated_view(ld, pd).items()):
            print(f"  {name:20s} -> {home}")
        if a.push and ld:
            print("[bridge] publishing local deployments to peer…")
            print("  ", post(peer["base"], {"token": peer["token"], "action": "apply", "deployments": ld}))

    elif a.cmd == "migrate":
        src = local if a.src == "local" else peer
        dst = local if a.dst == "local" else peer
        if a.src == a.dst:
            print("[migrate] --from and --to must differ"); return
        res = migrate(a.deploy, src, dst, wait=a.wait, poll=a.poll, skip_wait=a.skip_wait)
        print("[migrate] " + ("done: " if res["ok"] else "FAILED: ") + json.dumps(res))

if __name__ == "__main__":
    main()
