#!/usr/bin/env python3
"""
wasmlet — a sheet-native runtime. Where the kubelet resolves a `sicf:` image and runs it
through Docker, wasmlet resolves a WASM module that lives IN a spreadsheet cell and runs it
with a WASI runtime — no Docker daemon, no registry, no OCI. Just the bytes from the sheet.

  wasmlet.py --store <sheet-id> --name hello:v1
    -> pulls the module out of a cell, verifies its sha256, runs it with `wasmtime`

The store is a Google Sheet with a `Wasm` tab: name | digest | bytes | data_b64
(a whole small module fits in one cell — 50k base64 chars ~ a 37 KB module). This is the
runtime half of "everything in the sheet": SICF stores the image in cells, wasmlet executes
straight from them. Requires: google-api-python-client, google-auth, and `wasmtime` on PATH.
"""
import argparse, base64, hashlib, json, os, subprocess, sys, tempfile
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

WASM_TAB = "Wasm"

def _svc():
    path = os.environ.get("SHEETSOP_CREDS") or os.path.expanduser("~/.sheetsop/creds.json")
    c = Credentials.from_authorized_user_info(json.load(open(path)))
    if not c.valid:
        c.refresh(Request())
    return build("sheets", "v4", credentials=c, cache_discovery=False).spreadsheets()

def pull(store, name):
    ss = _svc()
    rows = ss.values().get(spreadsheetId=store, range=f"{WASM_TAB}!A1:D1000").execute().get("values", [])
    if not rows:
        sys.exit(f"[wasmlet] no {WASM_TAB} tab / empty store")
    head, *data = rows
    for r in data:
        rec = dict(zip(head, r))
        if rec.get("name") == name:
            raw = base64.b64decode(rec["data_b64"])
            got = "sha256:" + hashlib.sha256(raw).hexdigest()
            if got != rec.get("digest"):
                sys.exit(f"[wasmlet] digest MISMATCH for {name}: {got} != {rec.get('digest')}")
            return raw, got
    sys.exit(f"[wasmlet] module {name!r} not found in store")

def run(store, name, args):
    raw, digest = pull(store, name)
    print(f"[wasmlet] pulled {name} from a spreadsheet cell ({len(raw)} bytes), sha256 OK")
    with tempfile.NamedTemporaryFile(suffix=".wasm", delete=False) as f:
        f.write(raw); path = f.name
    print("[wasmlet] running via wasmtime (no Docker):")
    try:
        subprocess.run(["wasmtime", "run", path, *args], check=True)
    finally:
        os.unlink(path)

def main():
    ap = argparse.ArgumentParser(description="wasmlet — run a WASM module straight out of a spreadsheet cell")
    ap.add_argument("--store", required=True, help="spreadsheet id holding the Wasm tab")
    ap.add_argument("--name", required=True, help="module name, e.g. hello:v1")
    ap.add_argument("args", nargs="*", help="args passed to the module")
    a = ap.parse_args()
    run(a.store, a.name, a.args)

if __name__ == "__main__":
    main()
