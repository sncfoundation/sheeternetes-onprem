#!/usr/bin/env python3
"""
Sheeternetes on-prem apiserver — a LOCAL, offline control plane over a desktop
spreadsheet (Excel .xlsx or LibreOffice .ods). Same verb contract as the Google
Apps Script apiserver, so kubelet.sh / skctl point at it unchanged. Air-gap-friendly:
no internet required.

  WORKBOOK=cluster.xlsx TOKEN=secret python3 apiserver.py            # serve on :8787
  GET  /?token=..&kind=pods|nodes|deployments|events   -> {"items":[...]}
  POST /  {"token":..,"action":"apply|scale|delete", ...}

Requires: openpyxl  (pip install openpyxl). The spreadsheet is the store; this
process is the apiserver. Nodes on other machines reach it over the LAN, or you
sync via a shared file — see bridge.py for hybrid federation with Google Sheets.
"""
import json, os, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

WORKBOOK = os.environ.get("WORKBOOK", "cluster.xlsx")
TOKEN = os.environ.get("TOKEN", "CHANGE_ME_super_secret")
PORT = int(os.environ.get("PORT", "8787"))
NODE_TTL = int(os.environ.get("NODE_TTL", "30"))   # seconds before a silent node is NotReady
TABS = {
    "Deployments": ["name", "image", "replicas", "cpu_req", "mem_req", "command"],
    "Nodes": ["name", "ip", "cpu_total", "cpu_used", "mem_total", "status", "last_heartbeat"],
    "Pods": ["name", "deployment", "node", "phase", "container_id"],
    "Events": ["ts", "kind", "object", "message"],
}

def _wb():
    import openpyxl
    if os.path.exists(WORKBOOK):
        return openpyxl.load_workbook(WORKBOOK)
    wb = openpyxl.Workbook(); wb.remove(wb.active)
    for tab, headers in TABS.items():
        ws = wb.create_sheet(tab); ws.append(headers)
    wb.save(WORKBOOK); return wb

def read_tab(kind):
    wb = _wb()
    tab = {"pods": "Pods", "nodes": "Nodes", "deployments": "Deployments", "events": "Events"}.get(kind)
    if not tab: return None
    ws = wb[tab]; rows = list(ws.iter_rows(values_only=True))
    if not rows: return []
    headers = [str(h) for h in rows[0]]
    return [dict(zip(headers, r)) for r in rows[1:] if r and r[0] not in (None, "")]

def upsert_deployment(dep):
    import openpyxl
    wb = _wb(); ws = wb["Deployments"]; headers = TABS["Deployments"]
    name = dep.get("name")
    for row in ws.iter_rows(min_row=2):
        if row[0].value == name:
            for i, h in enumerate(headers): row[i].value = dep.get(h, row[i].value)
            wb.save(WORKBOOK); return "updated"
    ws.append([dep.get(h, "") for h in headers]); wb.save(WORKBOOK); return "created"

def scale(name, replicas):
    wb = _wb(); ws = wb["Deployments"]
    for row in ws.iter_rows(min_row=2):
        if row[0].value == name: row[2].value = replicas; wb.save(WORKBOOK); return "scaled"
    return "not found"

def delete(name):
    wb = _wb(); ws = wb["Deployments"]
    for i, row in enumerate(ws.iter_rows(min_row=2), start=2):
        if row[0].value == name: ws.delete_rows(i, 1); wb.save(WORKBOOK); return "deleted"
    return "not found"

def _dicts(ws):
    rows = list(ws.iter_rows(values_only=True))
    if not rows: return []
    headers = [str(h) for h in rows[0]]
    return [dict(zip(headers, r)) for r in rows[1:] if r and r[0] not in (None, "")]

def _int(v, default=0):
    try: return int(float(v))
    except (TypeError, ValueError): return default

def heartbeat(node, ip, cpu_total, mem_total, reported):
    """Node reports in -> we upsert it, run the scheduler over all Deployments,
    rewrite Pods, and return this node's desired pod set (the same shape the
    Apps Script apiserver returns, so kubelet.sh is byte-for-byte identical)."""
    wb = _wb(); now = int(time.time())
    ns = wb["Nodes"]; nheaders = TABS["Nodes"]

    # 1) upsert the reporting node (preserve cpu_used column), mark it Ready.
    found = False
    for row in ns.iter_rows(min_row=2):
        if row[0].value == node:
            patch = {"ip": ip, "cpu_total": cpu_total, "mem_total": mem_total,
                     "status": "Ready", "last_heartbeat": now}
            for i, h in enumerate(nheaders):
                if h in patch: row[i].value = patch[h]
            found = True; break
    if not found:
        ns.append([node, ip, cpu_total, 0, mem_total, "Ready", now])

    # 2) age out silent nodes; the schedulable set is the fresh ones.
    ready = []
    for row in ns.iter_rows(min_row=2):
        if row[0].value in (None, ""): continue
        hb = _int(row[6].value, 0)
        if now - hb >= NODE_TTL:
            row[5].value = "NotReady"
        else:
            ready.append(str(row[0].value))
    if node not in ready: ready.append(node)   # the caller is Ready by definition

    # 3) scheduler: one pod per replica, sticky to its current node if still Ready,
    #    otherwise placed on the least-loaded Ready node.
    existing = {p["name"]: p for p in _dicts(wb["Pods"])}
    load = {n: 0 for n in ready}
    desired = {}
    for dep in _dicts(wb["Deployments"]):
        name = dep.get("name")
        if not name: continue
        for i in range(1, _int(dep.get("replicas"), 0) + 1):
            pname = f"{name}-{i}"
            prev = existing.get(pname)
            place = prev["node"] if (prev and prev.get("node") in ready) \
                    else min(ready, key=lambda n: load[n])
            load[place] = load.get(place, 0) + 1
            desired[pname] = {"deployment": name, "node": place,
                              "image": dep.get("image"), "command": dep.get("command") or "",
                              "cpu_req": _int(dep.get("cpu_req"), 100),
                              "mem_req": _int(dep.get("mem_req"), 64)}

    # 4) rewrite the Pods tab, carrying over live phase / container_id from reports.
    rep = {p.get("name"): p for p in (reported or [])}
    ps = wb["Pods"]
    if ps.max_row > 1: ps.delete_rows(2, ps.max_row - 1)
    for pname, d in desired.items():
        live = rep.get(pname) or {}
        phase = live.get("phase") or ("Running" if pname in rep else "Pending")
        cid = live.get("container_id") or (existing.get(pname) or {}).get("container_id") or ""
        ps.append([pname, d["deployment"], d["node"], phase, cid])
    wb.save(WORKBOOK)

    # 5) this node's marching orders: run what's assigned here, stop the rest.
    out = [{"name": p, "desired": "Running", "image": d["image"], "command": d["command"],
            "cpu_req": d["cpu_req"], "mem_req": d["mem_req"], "deployment": d["deployment"]}
           for p, d in desired.items() if d["node"] == node]
    for pname in rep:
        d = desired.get(pname)
        if not d or d["node"] != node:
            out.append({"name": pname, "desired": "Terminating"})
    return {"pods": out}

class H(BaseHTTPRequestHandler):
    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
    def do_GET(self):
        q = parse_qs(urlparse(self.path).query)
        if q.get("token", [""])[0] != TOKEN: return self._json({"error": "unauthorized"}, 401)
        items = read_tab(q.get("kind", ["pods"])[0])
        if items is None: return self._json({"error": "unknown kind"}, 400)
        self._json({"items": items})
    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        try: body = json.loads(self.rfile.read(n) or b"{}")
        except Exception: return self._json({"error": "bad json"}, 400)
        if body.get("token") != TOKEN: return self._json({"error": "unauthorized"}, 401)
        a = body.get("action")
        if a is None and body.get("node"):   # kubelet heartbeat
            return self._json(heartbeat(
                body.get("node"), body.get("ip", ""),
                _int(body.get("cpu_total"), 1000), _int(body.get("mem_total"), 512),
                body.get("pods", [])))
        if a == "apply":
            res = [upsert_deployment(d) for d in body.get("deployments", [])]
            self._json({"applied": res})
        elif a == "scale": self._json({"result": scale(body.get("name"), body.get("replicas"))})
        elif a == "delete": self._json({"result": delete(body.get("name"))})
        else: self._json({"error": "unknown action"}, 400)
    def log_message(self, *a): pass

if __name__ == "__main__":
    _wb()
    print(f"[apiserver] serving {WORKBOOK} on http://0.0.0.0:{PORT} (kinds: {', '.join(TABS)})")
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
