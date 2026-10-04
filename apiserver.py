#!/usr/bin/env python3
"""
Sheeternetes on-prem apiserver — a LOCAL, offline control plane over a desktop
spreadsheet (Excel .xlsx). Same verb contract as the Google Apps Script apiserver,
so kubelet.sh / skctl point at it unchanged. Air-gap-friendly: no internet required.

  WORKBOOK=cluster.xlsx TOKEN=secret python3 apiserver.py            # serve on :8787
  GET  /?token=..&kind=pods|nodes|deployments|events|images|layers|secrets   -> {"items":[...]}
  POST /  {"token":..,"action":"apply|scale|delete|cordon|uncordon|drain|migrate|label|taint", ...}
  POST /  {"token":..,"node":..,"ip":..,"cpu_total":..,"mem_total":..,"pods":[...]}  # kubelet heartbeat

The apiserver also SCHEDULES: on each heartbeat it bin-packs Deployment replicas onto
Ready, schedulable nodes by cpu_req/mem_req (a pod that fits nowhere is Unschedulable),
keeps pods sticky to their node, ages out silent nodes (failover), and honors
cordon/drain. The scheduler core is the pure function `schedule()` below.

Published ports (the NodePort analog): a Deployment's `ports` cell ('8080:80', or '80' for
an auto-assigned port from NODEPORT_RANGE) is bound by the kubelet with `docker run -p`.
The scheduler never puts two pods on one node with the same host port, and the Pods tab
reports what the kubelet actually published as `endpoints` (node_ip:host->container/proto).

Auth: every request carries a shared token. Additionally, if SIGNING_KEY is set, POSTs
must be HMAC-SHA256 signed (X-SNCF-Timestamp + X-SNCF-Signature) within SIGN_TTL seconds —
tamper- and replay-resistant. See bridge.py, which signs its cross-substrate payloads.

Requires: openpyxl  (pip install openpyxl). The spreadsheet is the store; this process
is the apiserver. See bridge.py for hybrid federation with Google Sheets.
"""
import hashlib, hmac, json, os, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

WORKBOOK = os.environ.get("WORKBOOK", "cluster.xlsx")
TOKEN = os.environ.get("TOKEN", "CHANGE_ME_super_secret")
PORT = int(os.environ.get("PORT", "8787"))
NODE_TTL = int(os.environ.get("NODE_TTL", "30"))   # seconds before a silent node is NotReady
SIGNING_KEY = os.environ.get("SIGNING_KEY", "")    # if set, POSTs must carry a valid HMAC
SIGN_TTL = int(os.environ.get("SIGN_TTL", "300"))  # max clock skew (s) for a signed request
NODEPORT_RANGE = os.environ.get("NODEPORT_RANGE", "30000-32767")  # auto-assigned host ports
TABS = {
    "Deployments": ["name", "image", "replicas", "cpu_req", "mem_req", "command", "node_selector", "tolerations", "env", "secret_files", "ports"],
    "Nodes": ["name", "ip", "cpu_total", "cpu_used", "mem_total", "status", "last_heartbeat", "schedulable", "labels", "taints"],
    # ports = published mapping assigned by the scheduler (host:container/proto);
    # endpoints = what the kubelet actually bound, as node_ip:host->container/proto.
    "Pods": ["name", "deployment", "node", "phase", "container_id", "ports", "endpoints"],
    "Events": ["ts", "kind", "object", "message"],
    # SICF native image store (see sci: SICF v0.1). Populated by `sheetbuild import`.
    "Images": ["name", "digest", "config", "layers", "created", "size"],
    "Layers": ["digest", "ordinal", "media_type", "data"],
    # Secrets: base64 data mounted into pods as files (secret_files on a Deployment).
    "Secrets": ["name", "data"],
}

# ---------------------------------------------------------------- workbook I/O

import storage   # pluggable backends: .xlsx / .ods / csvdir: / cryptpad:

def _save(wb):
    storage.save(wb, WORKBOOK)

def _wb():
    import openpyxl
    wb = storage.load(WORKBOOK)
    if wb is not None:
        _ensure_schema(wb)
        return wb
    wb = openpyxl.Workbook(); wb.remove(wb.active)
    for tab, headers in TABS.items():
        ws = wb.create_sheet(tab); ws.append(headers)
    _save(wb); return wb

def _ensure_schema(wb):
    """Upgrade older workbooks: create missing tabs and append missing trailing
    columns (e.g. the newer Nodes.schedulable), backfilling a sane default."""
    changed = False
    defaults = {"schedulable": True}
    for tab, headers in TABS.items():
        if tab not in wb.sheetnames:
            wb.create_sheet(tab).append(headers); changed = True; continue
        ws = wb[tab]
        have = [str(c.value) for c in ws[1]] if ws.max_row else []
        for h in headers:
            if h not in have:
                col = len(have) + 1
                ws.cell(row=1, column=col, value=h)
                for r in range(2, ws.max_row + 1):
                    if ws.cell(row=r, column=1).value not in (None, ""):
                        ws.cell(row=r, column=col, value=defaults.get(h, ""))
                have.append(h); changed = True
    if changed: _save(wb)

def _dicts(ws):
    rows = list(ws.iter_rows(values_only=True))
    if not rows: return []
    headers = [str(h) for h in rows[0]]
    return [dict(zip(headers, r)) for r in rows[1:] if r and r[0] not in (None, "")]

KIND2TAB = {"pods": "Pods", "nodes": "Nodes", "deployments": "Deployments", "events": "Events",
            "images": "Images", "layers": "Layers", "secrets": "Secrets"}

def read_tab(kind):
    tab = KIND2TAB.get(kind)
    if not tab: return None
    return _dicts(_wb()[tab])

def _int(v, default=0):
    try: return int(float(v))
    except (TypeError, ValueError): return default

def _truthy(v, default=True):
    if v in (None, ""): return default
    return str(v).strip().upper() not in ("FALSE", "0", "NO")

def sign(key, ts, body):
    """HMAC-SHA256 over 'timestamp.body' — the wire signature. Pure, so the bridge
    can produce byte-identical signatures and both sides can unit-test it."""
    if isinstance(body, str): body = body.encode()
    msg = str(ts).encode() + b"." + body
    return hmac.new(key.encode(), msg, hashlib.sha256).hexdigest()

def verify(key, ts, sig, body, now, ttl):
    """Constant-time verify with a bounded clock skew (anti-replay)."""
    if not (key and ts and sig): return False
    try:
        if abs(now - int(ts)) > ttl: return False
    except (TypeError, ValueError):
        return False
    return hmac.compare_digest(sign(key, ts, body), sig)

def parse_kv(s):
    """'disk=ssd,zone=a' -> {'disk':'ssd','zone':'a'}  (labels / node_selector)."""
    out = {}
    for part in str(s or "").split(","):
        part = part.strip()
        if "=" in part:
            k, v = part.split("=", 1); out[k.strip()] = v.strip()
    return out

def parse_taints(s):
    """'gpu=true:NoSchedule,zone=a' -> [('gpu','true','NoSchedule'),('zone','a','NoSchedule')]."""
    out = []
    for part in str(s or "").split(","):
        part = part.strip()
        if not part: continue
        kv, _, eff = part.partition(":")
        eff = eff.strip() or "NoSchedule"
        k, _, v = kv.partition("="); out.append((k.strip(), v.strip(), eff))
    return out

def parse_tolerations(s):
    """'gpu=true,zone' -> {'gpu=true','zone'}  (matches a taint by 'k=v' or bare 'k')."""
    return {p.strip() for p in str(s or "").split(",") if p.strip()}

def parse_ports(s):
    """Published ports of a Deployment (the NodePort analog), docker -p flavored:

      '80'           container port 80, host port auto-assigned from NODEPORT_RANGE
      '8080:80'      host port 8080 -> container port 80
      '8080:80/udp'  same, UDP (default proto: tcp)

    Accepts a comma-separated string or a JSON list. Returns
    [(host_port or None, container_port, proto)]. Raises ValueError on garbage, so
    `apply` can reject a bad cell instead of the kubelet failing silently later."""
    if isinstance(s, (list, tuple)):
        parts = [str(p) for p in s]
    else:
        parts = str(s or "").split(",")
    out = []
    for part in parts:
        part = part.strip()
        if not part: continue
        spec, _, proto = part.partition("/")
        proto = (proto or "tcp").strip().lower()
        if proto not in ("tcp", "udp"):
            raise ValueError(f"bad protocol in port {part!r}")
        host, sep, cont = spec.rpartition(":")
        try:
            c = int(cont); h = int(host) if sep else None
        except ValueError:
            raise ValueError(f"bad port {part!r} (want 'container', 'host:container' or '…/udp')")
        if not 1 <= c <= 65535 or (h is not None and not 1 <= h <= 65535):
            raise ValueError(f"port out of range in {part!r}")
        out.append((h, c, proto))
    return out

def _ports_lenient(s):
    """parse_ports for the reconcile path: a cell hand-edited into nonsense is
    treated as 'no ports' rather than taking the scheduler down."""
    try: return parse_ports(s)
    except ValueError: return []

def format_ports(mapping):
    """[(host, container, proto)] -> '30000:80/tcp,8080:80/tcp' (the Pods.ports cell)."""
    return ",".join(f"{h}:{c}/{p}" for h, c, p in mapping)

def port_range(spec=None):
    lo, _, hi = str(spec or NODEPORT_RANGE).partition("-")
    lo = _int(lo, 30000); hi = _int(hi, lo)
    return (lo, hi) if lo <= hi else (hi, lo)

def parse_docker_ports(s):
    """`docker ps --format {{.Ports}}` -> {(host, container, proto)} actually published.
    e.g. '0.0.0.0:30000->80/tcp, [::]:30000->80/tcp' -> {(30000, 80, 'tcp')}.
    Exposed-but-unpublished entries ('80/tcp') are ignored."""
    out = set()
    for part in str(s or "").split(","):
        part = part.strip()
        if "->" not in part: continue
        left, right = part.split("->", 1)
        host = left.rpartition(":")[2]
        cont, _, proto = right.partition("/")
        if "-" in host or "-" in cont: continue   # ranges are never produced by the kubelet
        try: out.add((int(host), int(cont), (proto or "tcp").lower()))
        except ValueError: continue
    return out

def _tolerates(tols, taints):
    for k, v, eff in taints:
        if eff == "NoSchedule" and f"{k}={v}" not in tols and k not in tols:
            return False
    return True

def _matches(node, dep):
    """A pod's node_selector must be a subset of the node's labels, and the pod
    must tolerate every NoSchedule taint on the node."""
    labels = node.get("labels") or {}
    for k, v in (dep.get("node_selector") or {}).items():
        if labels.get(k) != v:
            return False
    return _tolerates(dep.get("tolerations") or set(), node.get("taints") or [])

# ------------------------------------------------------------- CRUD (skctl verbs)

def _cell(v):
    """JSON manifests may carry lists (e.g. "ports": ["8080:80", "53/udp"]); a cell holds a string."""
    return ",".join(str(x) for x in v) if isinstance(v, (list, tuple)) else v

def _upsert_row(tab, obj):
    wb = _wb(); ws = wb[tab]; headers = TABS[tab]
    name = obj.get("name")
    for row in ws.iter_rows(min_row=2):
        if row[0].value == name:
            for i, h in enumerate(headers):
                if h in obj: row[i].value = _cell(obj[h])
            _save(wb); return "updated"
    ws.append([_cell(obj.get(h, "")) for h in headers]); _save(wb); return "created"

def _delete_row(tab, name):
    wb = _wb(); ws = wb[tab]
    for i, row in enumerate(ws.iter_rows(min_row=2), start=2):
        if row[0].value == name: ws.delete_rows(i, 1); _save(wb); return "deleted"
    return "not found"

def upsert_deployment(dep):
    if not dep.get("name"): return "invalid: name is required"
    try: parse_ports(dep.get("ports"))
    except ValueError as e: return f"invalid: {e}"
    return _upsert_row("Deployments", dep)

def scale(name, replicas):
    wb = _wb(); ws = wb["Deployments"]
    for row in ws.iter_rows(min_row=2):
        if row[0].value == name: row[2].value = replicas; _save(wb); return "scaled"
    return "not found"

def delete(name, kind="deployment"):
    tab = {"deployment": "Deployments"}.get(kind)
    return _delete_row(tab, name) if tab else "unknown kind"

def set_schedulable(node, value):
    wb = _wb(); ns = wb["Nodes"]; col = TABS["Nodes"].index("schedulable")
    for row in ns.iter_rows(min_row=2):
        if row[0].value == node:
            row[col].value = bool(value)
            _reschedule(wb); _save(wb)
            return "uncordoned" if value else "cordoned"
    return "not found"

def drain(node):
    """Cordon the node and move its pods off now (they reschedule onto survivors)."""
    wb = _wb(); ns = wb["Nodes"]; col = TABS["Nodes"].index("schedulable")
    found = False
    for row in ns.iter_rows(min_row=2):
        if row[0].value == node: row[col].value = False; found = True
    if not found: return "not found"
    _reschedule(wb, exclude={node}); _save(wb)
    return "drained"

def migrate(pod, target):
    """Pin a pod to a target node (make-before-break: the target kubelet starts it,
    the source kubelet then sees it as Terminating and stops it)."""
    wb = _wb(); now = int(time.time())
    nodes = _load_nodes(wb["Nodes"], now)
    tgt = next((n for n in nodes if n["name"] == target), None)
    if not tgt: return "unknown node"
    if not (tgt["fresh"] and tgt["schedulable"]): return "target not schedulable"
    ps = wb["Pods"]
    for row in ps.iter_rows(min_row=2):
        if row[0].value == pod:
            row[2].value = target; _save(wb); return f"migrating {pod} -> {target}"
    return "pod not found"

def _edit_node_csv(node, column, mutate):
    wb = _wb(); ns = wb["Nodes"]; col = TABS["Nodes"].index(column)
    for row in ns.iter_rows(min_row=2):
        if row[0].value == node:
            row[col].value = mutate(str(row[col].value or ""))
            _reschedule(wb); _save(wb); return "ok"
    return "not found"

def label_node(node, spec):
    """label_node('a','disk=ssd')  sets a label; 'disk-' removes it."""
    def mut(cur):
        kv = parse_kv(cur)
        if spec.endswith("-"):
            kv.pop(spec[:-1].strip(), None)
        else:
            k, _, v = spec.partition("="); kv[k.strip()] = v.strip()
        return ",".join(f"{k}={v}" for k, v in kv.items())
    return _edit_node_csv(node, "labels", mut)

def taint_node(node, spec):
    """taint_node('a','gpu=true:NoSchedule')  adds a taint; 'gpu-' removes by key."""
    def mut(cur):
        taints = parse_taints(cur)
        if spec.endswith("-"):
            key = spec[:-1].strip()
            taints = [t for t in taints if t[0] != key]
        else:
            for k, v, eff in parse_taints(spec):
                taints = [t for t in taints if t[0] != k] + [(k, v, eff)]
        return ",".join(f"{k}={v}:{eff}" for k, v, eff in taints)
    return _edit_node_csv(node, "taints", mut)

# ------------------------------------------------------------------ scheduler

def spec_hash(image, command, env, secret_files, ports):
    """Fingerprint of everything that requires a container *restart* when it changes.
    The kubelet labels containers with it and recreates on mismatch (e.g. a new port)."""
    blob = json.dumps([image or "", command or "", env or "", secret_files or "", ports or ""])
    return hashlib.sha256(blob.encode()).hexdigest()[:12]

def schedule(deployments, nodes, existing, exclude=frozenset(), nodeports=None):
    """Pure scheduler — no I/O, fully unit-testable.

    deployments: [{name,image,replicas,cpu_req,mem_req,command,node_selector,tolerations,ports}]
    nodes:       [{name,cpu_total,mem_total,fresh(bool),schedulable(bool),labels,taints}]
    existing:    {podname: {"node": ..., "ports": "30000:80/tcp"}}  (sticky placement + ports)
    exclude:     node names to evict from (drain) — their pods are re-placed.
    nodeports:   'lo-hi' range for auto-assigned host ports (default: NODEPORT_RANGE).

    Affinity: a pod is only placed on a node whose labels are a superset of the
    deployment's node_selector and whose NoSchedule taints the pod tolerates.

    Host ports: a published host port can be bound once per node and protocol, so two
    replicas asking for the same fixed port never share a node (the second one spreads,
    or is Unschedulable). Auto ports ('80') get the lowest free port of the range on the
    chosen node, never one that any Deployment requests as a fixed port, and keep their
    previous port across reschedules on the same node.

    Returns (desired, alloc):
      desired = {podname: {deployment,node,image,command,cpu_req,mem_req,env,secret_files,
                           ports,spec_hash}}
                node == "" means Unschedulable (fits nowhere / no capacity / port taken).
      alloc   = {nodename: {"cpu": millicores, "mem": MiB}}  placed load per node.
    Placement is two-phase so running pods win over newcomers: first every pod that can
    stay on its current node (fresh, not excluded, still matches, still fits) is kept;
    then the rest land on the fresh+schedulable node with the most free CPU that fits.
    """
    keepable = {n["name"] for n in nodes if n["fresh"]} - set(exclude)
    by_name = {n["name"]: n for n in nodes}
    schedulable_fresh = [n for n in nodes if n["fresh"] and n["schedulable"] and n["name"] not in exclude]
    cap = {n["name"]: (n["cpu_total"], n["mem_total"]) for n in nodes}
    alloc = {n["name"]: {"cpu": 0, "mem": 0} for n in nodes}
    bound = {n["name"]: set() for n in nodes}           # (host_port, proto) in use per node
    lo, hi = port_range(nodeports)
    fixed_anywhere = {(h, p) for d in deployments for h, _, p in _ports_lenient(d.get("ports")) if h}

    def fits(name, cpu, mem):
        ct, mt = cap.get(name, (0, 0))
        return alloc[name]["cpu"] + cpu <= ct and alloc[name]["mem"] + mem <= mt

    def bind_ports(name, specs, prev_ports):
        """Host-port assignment for one pod on node `name`, or None if impossible."""
        taken = set(bound[name]); mapping = []
        prev = {(c, p): h for h, c, p in prev_ports}
        for h, c, p in specs:
            if h is None:
                h = prev.get((c, p))
                if h is None or not lo <= h <= hi or (h, p) in taken or (h, p) in fixed_anywhere:
                    h = next((x for x in range(lo, hi + 1)
                              if (x, p) not in taken and (x, p) not in fixed_anywhere), None)
                    if h is None: return None                 # range exhausted on this node
            elif (h, p) in taken:
                return None                                   # fixed host port already bound
            taken.add((h, p)); mapping.append((h, c, p))
        return mapping

    # one slot per replica, in Deployment-row order
    slots = []
    for dep in deployments:
        name = dep.get("name")
        if not name: continue
        cpu_req = _int(dep.get("cpu_req"), 100); mem_req = _int(dep.get("mem_req"), 64)
        specs = _ports_lenient(dep.get("ports"))
        placeable = [n["name"] for n in schedulable_fresh if _matches(n, dep)]   # affinity + taints
        for i in range(1, _int(dep.get("replicas"), 0) + 1):
            pname = f"{name}-{i}"
            ex = existing.get(pname) or {}
            slots.append({"pod": pname, "dep": dep, "cpu": cpu_req, "mem": mem_req, "specs": specs,
                          "placeable": placeable, "prev": ex.get("node"),
                          "prev_ports": _ports_lenient(ex.get("ports")), "node": "", "ports": []})

    def take(slot, node, mapping):
        alloc[node]["cpu"] += slot["cpu"]; alloc[node]["mem"] += slot["mem"]
        bound[node].update((h, p) for h, _, p in mapping)
        slot["node"], slot["ports"] = node, mapping

    for slot in slots:                                       # phase 1: sticky
        prev = slot["prev"]
        if prev in keepable and _matches(by_name[prev], slot["dep"]) and fits(prev, slot["cpu"], slot["mem"]):
            mapping = bind_ports(prev, slot["specs"], slot["prev_ports"])
            if mapping is not None: take(slot, prev, mapping)
    for slot in slots:                                       # phase 2: place the rest
        if slot["node"]: continue
        best = None
        for nm in slot["placeable"]:
            if not fits(nm, slot["cpu"], slot["mem"]): continue
            mapping = bind_ports(nm, slot["specs"], [])
            if mapping is None: continue
            free = cap[nm][0] - alloc[nm]["cpu"]
            if best is None or free > best[0]: best = (free, nm, mapping)
        if best: take(slot, best[1], best[2])

    desired = {}
    for slot in slots:
        dep = slot["dep"]; ports = format_ports(slot["ports"])
        desired[slot["pod"]] = {
            "deployment": dep["name"], "node": slot["node"],
            "image": dep.get("image"), "command": dep.get("command") or "",
            "cpu_req": slot["cpu"], "mem_req": slot["mem"],
            "env": dep.get("env") or "", "secret_files": dep.get("secret_files") or "",
            "ports": ports,
            "spec_hash": spec_hash(dep.get("image"), dep.get("command"), dep.get("env"),
                                   dep.get("secret_files"), ports)}
    return desired, alloc

def _load_nodes(ns, now):
    out = []
    for r in _dicts(ns):
        hb = _int(r.get("last_heartbeat"), 0)
        out.append({"name": r["name"], "cpu_total": _int(r.get("cpu_total"), 1000),
                    "mem_total": _int(r.get("mem_total"), 512),
                    "fresh": now - hb < NODE_TTL,
                    "schedulable": _truthy(r.get("schedulable")),
                    "labels": parse_kv(r.get("labels")),
                    "taints": parse_taints(r.get("taints"))})
    return out

def _load_deployments(ws):
    out = []
    for d in _dicts(ws):
        d = dict(d)
        d["node_selector"] = parse_kv(d.get("node_selector"))
        d["tolerations"] = parse_tolerations(d.get("tolerations"))
        out.append(d)
    return out

def pod_rows(desired, existing, reported, node_ips, reporter=None):
    """Pure: the Pods tab after a scheduling pass.

    reported: {podname: {phase, container_id, ports}} from the reporting kubelet.
    reporter: that kubelet's node (None for control actions like cordon/drain).
    A pod that stays on *another* node keeps its last known phase/endpoints — only its
    own kubelet may vouch for it, so multi-node clusters don't flap Running<->Pending."""
    rows = []
    for pname, d in desired.items():
        live = reported.get(pname)
        ex = existing.get(pname) or {}
        same_node = bool(d["node"]) and ex.get("node") == d["node"]
        if not d["node"]:
            phase, endpoints = "Unschedulable", ""
        elif live is not None:
            phase = live.get("phase") or "Running"
            ip = node_ips.get(d["node"]) or ""
            endpoints = ",".join(f"{ip}:{h}->{c}/{p}" for h, c, p in sorted(parse_docker_ports(live.get("ports"))))
        elif same_node and d["node"] != reporter:
            phase, endpoints = ex.get("phase") or "Pending", ex.get("endpoints") or ""
        else:
            phase, endpoints = "Pending", ""
        cid = (live or {}).get("container_id") or (ex.get("container_id") if same_node else "") or ""
        rows.append({"name": pname, "deployment": d["deployment"], "node": d["node"], "phase": phase,
                     "container_id": cid, "ports": d.get("ports", ""), "endpoints": endpoints})
    return rows

def node_orders(desired, reported, node):
    """Pure: a kubelet's marching orders — run what's assigned here, stop the rest."""
    keys = ("image", "command", "cpu_req", "mem_req", "deployment", "env", "secret_files",
            "ports", "spec_hash")
    out = [{"name": p, "desired": "Running", **{k: d.get(k, "") for k in keys}}
           for p, d in desired.items() if d["node"] == node]
    for pname in reported:
        d = desired.get(pname)
        if not d or d["node"] != node:
            out.append({"name": pname, "desired": "Terminating"})
    return out

def _write_rows(ws, tab, rows):
    if ws.max_row > 1: ws.delete_rows(2, ws.max_row - 1)
    for r in rows: ws.append([r.get(h, "") for h in TABS[tab]])

def _node_ips(ns):
    return {r["name"]: str(r.get("ip") or "") for r in _dicts(ns)}

def _write_node_status(ns, nodes, alloc):
    ci = {h: TABS["Nodes"].index(h) for h in ("cpu_used", "status", "schedulable")}
    for row in ns.iter_rows(min_row=2):
        nm = row[0].value
        n = next((x for x in nodes if x["name"] == nm), None)
        if not n: continue
        row[ci["cpu_used"]].value = alloc.get(nm, {}).get("cpu", 0)
        if not n["fresh"]:            row[ci["status"]].value = "NotReady"
        elif not n["schedulable"]:    row[ci["status"]].value = "SchedulingDisabled"
        else:                         row[ci["status"]].value = "Ready"

def _reconcile(wb, now, reported=None, reporter=None, exclude=frozenset()):
    """Schedule from the workbook's current state and persist Pods + node status."""
    ns = wb["Nodes"]
    nodes = _load_nodes(ns, now)
    existing = {p["name"]: p for p in _dicts(wb["Pods"])}
    rep = reported or {}
    desired, alloc = schedule(_load_deployments(wb["Deployments"]), nodes, existing, exclude)
    _write_rows(wb["Pods"], "Pods", pod_rows(desired, existing, rep, _node_ips(ns), reporter))
    _write_node_status(ns, nodes, alloc)
    return desired

def _reschedule(wb, exclude=frozenset()):
    """Recompute placement for control actions (cordon/drain); no kubelet report to fold in."""
    return _reconcile(wb, int(time.time()), exclude=exclude)

def heartbeat(node, ip, cpu_total, mem_total, reported):
    """Node reports in -> upsert it, run the scheduler over all Deployments, rewrite
    Pods, and return this node's desired pod set (same response shape as the Apps
    Script apiserver, so kubelet.sh is byte-for-byte identical)."""
    wb = _wb(); now = int(time.time())
    ns = wb["Nodes"]; nh = TABS["Nodes"]

    # 1) upsert the reporting node (preserve cpu_used + schedulable/cordon state).
    found = False
    for row in ns.iter_rows(min_row=2):
        if row[0].value == node:
            patch = {"ip": ip, "cpu_total": cpu_total, "mem_total": mem_total, "last_heartbeat": now}
            for i, h in enumerate(nh):
                if h in patch: row[i].value = patch[h]
            found = True; break
    if not found:
        ns.append([node, ip, cpu_total, 0, mem_total, "Ready", now, True])

    # 2) schedule over the current fleet, 3) persist Pods + node status/allocation.
    rep = {p.get("name"): p for p in (reported or [])}
    desired = _reconcile(wb, now, rep, reporter=node)
    _save(wb)

    # 4) this node's marching orders.
    return {"pods": node_orders(desired, rep, node)}

# ----------------------------------------------------------------------- HTTP

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
        raw = self.rfile.read(n) or b"{}"
        if SIGNING_KEY and not verify(SIGNING_KEY, self.headers.get("X-SNCF-Timestamp", ""),
                                      self.headers.get("X-SNCF-Signature", ""), raw,
                                      int(time.time()), SIGN_TTL):
            return self._json({"error": "bad signature"}, 401)
        try: body = json.loads(raw)
        except Exception: return self._json({"error": "bad json"}, 400)
        if body.get("token") != TOKEN: return self._json({"error": "unauthorized"}, 401)
        a = body.get("action")
        if a is None and body.get("node"):   # kubelet heartbeat
            return self._json(heartbeat(
                body.get("node"), body.get("ip", ""),
                _int(body.get("cpu_total"), 1000), _int(body.get("mem_total"), 512),
                body.get("pods", [])))
        if a == "apply":
            self._json({"applied": [upsert_deployment(d) for d in body.get("deployments") or []]})
        elif a == "scale":     self._json({"result": scale(body.get("name"), body.get("replicas"))})
        elif a == "delete":    self._json({"result": delete(body.get("name"), body.get("kind") or "deployment")})
        elif a == "cordon":    self._json({"result": set_schedulable(body.get("name"), False)})
        elif a == "uncordon":  self._json({"result": set_schedulable(body.get("name"), True)})
        elif a == "drain":     self._json({"result": drain(body.get("name"))})
        elif a == "migrate":   self._json({"result": migrate(body.get("name"), body.get("node"))})
        elif a == "label":     self._json({"result": label_node(body.get("name"), body.get("spec", ""))})
        elif a == "taint":     self._json({"result": taint_node(body.get("name"), body.get("spec", ""))})
        else: self._json({"error": "unknown action"}, 400)
    def log_message(self, *a): pass

if __name__ == "__main__":
    _wb()
    print(f"[apiserver] serving {WORKBOOK} on http://0.0.0.0:{PORT} (kinds: {', '.join(TABS)})"
          + (" · HMAC signing REQUIRED on POST" if SIGNING_KEY else ""))
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
