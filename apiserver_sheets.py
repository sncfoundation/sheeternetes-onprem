#!/usr/bin/env python3
"""
Sheeternetes apiserver — Google Sheets backend.

Same HTTP verb contract as apiserver.py (so kubelet.sh / skctl point at it unchanged),
but the control plane is a real, shareable **Google Sheet** instead of a local .xlsx.
Reuses the pure scheduler from apiserver.py; only the storage layer differs.

  CLUSTER_SHEET=<spreadsheet-id> SHEETSOP_CREDS=creds.json TOKEN=secret \
    python3 apiserver_sheets.py                      # serve on :8787

The cluster's structure (Deployments/Nodes/Pods/Events/Images/Layers/Secrets) lives in
the tabs of that Google Sheet — open it read-only and watch the cluster reconcile live.
Requires: google-api-python-client, google-auth. Execution still happens on nodes.
"""
import json, os, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

import apiserver as core   # reuse TABS, schedule(), parsers, _int/_truthy

SHEET_ID = os.environ["CLUSTER_SHEET"]
TOKEN = os.environ.get("TOKEN", "CHANGE_ME_super_secret")
PORT = int(os.environ.get("PORT", "8787"))
NODE_TTL = int(os.environ.get("NODE_TTL", "30"))
KIND2TAB = {"pods": "Pods", "nodes": "Nodes", "deployments": "Deployments", "events": "Events",
            "images": "Images", "layers": "Layers", "secrets": "Secrets"}

def _svc():
    info = json.load(open(os.environ.get("SHEETSOP_CREDS", os.path.expanduser("~/.sheetsop/creds.json"))))
    c = Credentials.from_authorized_user_info(info)
    if not c.valid:
        c.refresh(Request())
    return build("sheets", "v4", credentials=c, cache_discovery=False).spreadsheets()

SS = _svc()

# ------------------------------------------------------------------ sheet I/O

def _ensure_tabs():
    meta = SS.get(spreadsheetId=SHEET_ID).execute()
    have = {s["properties"]["title"] for s in meta["sheets"]}
    reqs = []
    for tab in core.TABS:
        if tab not in have:
            reqs.append({"addSheet": {"properties": {"title": tab}}})
    if reqs:
        SS.batchUpdate(spreadsheetId=SHEET_ID, body={"requests": reqs}).execute()
    for tab, cols in core.TABS.items():
        cur = _rows(tab)
        if not cur:
            SS.values().update(spreadsheetId=SHEET_ID, range=f"{tab}!A1",
                valueInputOption="RAW", body={"values": [cols]}).execute()

def _rows(tab):
    return SS.values().get(spreadsheetId=SHEET_ID, range=f"{tab}!A1:Z2000").execute().get("values", [])

def records(tab):
    rows = _rows(tab)
    if not rows:
        return []
    head = rows[0]
    out = []
    for r in rows[1:]:
        if not r or not r[0]:
            continue
        out.append({head[i]: (r[i] if i < len(r) else "") for i in range(len(head))})
    return out

def _write_tab(tab, dicts):
    cols = core.TABS[tab]
    body = [cols] + [[d.get(c, "") for c in cols] for d in dicts]
    SS.values().clear(spreadsheetId=SHEET_ID, range=f"{tab}!A1:Z2000").execute()
    SS.values().update(spreadsheetId=SHEET_ID, range=f"{tab}!A1", valueInputOption="RAW",
        body={"values": body}).execute()

# --------------------------------------------------------------- node loading

def _load_nodes(now):
    out = []
    for r in records("Nodes"):
        hb = core._int(r.get("last_heartbeat"), 0)
        out.append({"name": r["name"], "cpu_total": core._int(r.get("cpu_total"), 1000),
                    "mem_total": core._int(r.get("mem_total"), 512),
                    "fresh": now - hb < NODE_TTL, "schedulable": core._truthy(r.get("schedulable")),
                    "labels": core.parse_kv(r.get("labels")), "taints": core.parse_taints(r.get("taints"))})
    return out

def _load_deployments():
    out = []
    for d in records("Deployments"):
        d = dict(d)
        d["node_selector"] = core.parse_kv(d.get("node_selector"))
        d["tolerations"] = core.parse_tolerations(d.get("tolerations"))
        out.append(d)
    return out

# ------------------------------------------------------------------ heartbeat

def heartbeat(node, ip, cpu_total, mem_total, reported):
    now = int(time.time())
    nodes_raw = records("Nodes")
    found = False
    for r in nodes_raw:
        if r["name"] == node:
            r.update({"ip": ip, "cpu_total": cpu_total, "mem_total": mem_total, "last_heartbeat": now})
            found = True
    if not found:
        nodes_raw.append({"name": node, "ip": ip, "cpu_total": cpu_total, "cpu_used": 0,
                          "mem_total": mem_total, "status": "Ready", "last_heartbeat": now, "schedulable": True})

    nodes = []
    for r in nodes_raw:
        hb = core._int(r.get("last_heartbeat"), 0)
        nodes.append({"name": r["name"], "cpu_total": core._int(r.get("cpu_total"), 1000),
                      "mem_total": core._int(r.get("mem_total"), 512),
                      "fresh": now - hb < NODE_TTL, "schedulable": core._truthy(r.get("schedulable")),
                      "labels": core.parse_kv(r.get("labels")), "taints": core.parse_taints(r.get("taints"))})
    existing = {p["name"]: p for p in records("Pods")}
    rep = {p.get("name"): p for p in (reported or [])}
    desired, alloc = core.schedule(_load_deployments(), nodes, existing)

    # Pods tab
    pods = []
    for pname, d in desired.items():
        live = rep.get(pname) or {}
        phase = "Unschedulable" if not d["node"] else (live.get("phase") or ("Running" if pname in rep else "Pending"))
        cid = live.get("container_id") or (existing.get(pname) or {}).get("container_id") or ""
        pods.append({"name": pname, "deployment": d["deployment"], "node": d["node"], "phase": phase, "container_id": cid})
    _write_tab("Pods", pods)

    # Node status + cpu_used
    for r in nodes_raw:
        n = next((x for x in nodes if x["name"] == r["name"]), None)
        if not n:
            continue
        r["cpu_used"] = alloc.get(r["name"], {}).get("cpu", 0)
        r["status"] = "NotReady" if not n["fresh"] else ("SchedulingDisabled" if not n["schedulable"] else "Ready")
    _write_tab("Nodes", nodes_raw)

    out = [{"name": p, "desired": "Running", "image": d["image"], "command": d["command"],
            "cpu_req": d["cpu_req"], "mem_req": d["mem_req"], "deployment": d["deployment"],
            "env": d.get("env", ""), "secret_files": d.get("secret_files", "")}
           for p, d in desired.items() if d["node"] == node]
    for pname in rep:
        d = desired.get(pname)
        if not d or d["node"] != node:
            out.append({"name": pname, "desired": "Terminating"})
    return {"pods": out}

# ------------------------------------------------------------------ mutations

def apply_deps(deps):
    cur = records("Deployments")
    by = {d["name"]: d for d in cur}
    for d in deps:
        by[d["name"]] = {**by.get(d["name"], {}), **d}
    _write_tab("Deployments", list(by.values()))
    return [d.get("name") for d in deps]

def scale(name, replicas):
    cur = records("Deployments")
    for d in cur:
        if d["name"] == name:
            d["replicas"] = replicas; _write_tab("Deployments", cur); return "scaled"
    return "not found"

def delete(name):
    cur = [d for d in records("Deployments") if d["name"] != name]
    _write_tab("Deployments", cur); return "deleted"

# ----------------------------------------------------------------------- HTTP

class H(BaseHTTPRequestHandler):
    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
    def do_GET(self):
        q = parse_qs(urlparse(self.path).query)
        if q.get("token", [""])[0] != TOKEN: return self._json({"error": "unauthorized"}, 401)
        tab = KIND2TAB.get(q.get("kind", ["pods"])[0])
        if not tab: return self._json({"error": "unknown kind"}, 400)
        self._json({"items": records(tab)})
    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        try: body = json.loads(self.rfile.read(n) or b"{}")
        except Exception: return self._json({"error": "bad json"}, 400)
        if body.get("token") != TOKEN: return self._json({"error": "unauthorized"}, 401)
        a = body.get("action")
        if a is None and body.get("node"):
            return self._json(heartbeat(body.get("node"), body.get("ip", ""),
                core._int(body.get("cpu_total"), 1000), core._int(body.get("mem_total"), 512), body.get("pods", [])))
        if a == "apply":     self._json({"applied": apply_deps(body.get("deployments", []))})
        elif a == "scale":   self._json({"result": scale(body.get("name"), body.get("replicas"))})
        elif a == "delete":  self._json({"result": delete(body.get("name"))})
        else: self._json({"error": "unknown action"}, 400)
    def log_message(self, *a): pass

if __name__ == "__main__":
    _ensure_tabs()
    print(f"[apiserver-sheets] cluster = https://docs.google.com/spreadsheets/d/{SHEET_ID}  on :{PORT}")
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
