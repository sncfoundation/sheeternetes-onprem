#!/usr/bin/env python3
"""
Sheetmesh bridge — federate an on-prem (Excel/LibreOffice) cluster with a
Google Sheets cluster. On-prem nodes can't be reached inbound, so the bridge
dials OUT: it reads the local apiserver and publishes a snapshot to a rendezvous
endpoint (a Google Sheets Apps Script web app, or another node), and pulls the
peer's services back — giving cross-substrate service discovery.

  python3 bridge.py --local http://localhost:8787 --local-token secret \
                    --peer https://script.google.com/macros/s/XXXX/exec --peer-token secret2

Requires only the standard library (urllib). Sync interval bounded by the peer
(Apps Script triggers ~1/min); trust via a shared token/HMAC.
"""
import argparse, json, urllib.request, urllib.parse

def get(base, token, kind):
    url = base + ("&" if "?" in base else "?") + urllib.parse.urlencode({"token": token, "kind": kind})
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.loads(r.read()).get("items", [])

def post(base, payload):
    req = urllib.request.Request(base, data=json.dumps(payload).encode(),
                                headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode()

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--local", required=True); p.add_argument("--local-token", default="CHANGE_ME_super_secret")
    p.add_argument("--peer", required=True); p.add_argument("--peer-token", default="CHANGE_ME_super_secret")
    p.add_argument("--push", action="store_true", help="also publish local deployments to the peer")
    a = p.parse_args()

    local = get(a.local, a.local_token, "deployments")
    peer = get(a.peer, a.peer_token, "deployments")
    print(f"[bridge] local: {len(local)} deployments · peer: {len(peer)} deployments")

    mesh = {d.get("name"): "local" for d in local}
    for d in peer: mesh.setdefault(d.get("name"), "peer")
    print("[bridge] federated services:")
    for name, home in sorted(mesh.items()):
        print(f"  {name:20s} -> {home}")

    if a.push and local:
        print("[bridge] publishing local deployments to peer…")
        print("  ", post(a.peer, {"token": a.peer_token, "action": "apply", "deployments": local}))

if __name__ == "__main__":
    main()
