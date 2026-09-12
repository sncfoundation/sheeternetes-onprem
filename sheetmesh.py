#!/usr/bin/env python3
"""
Sheetmesh — federate many Sheeternetes clusters into one mesh, through a rendezvous sheet.

`bridge.py` federates two clusters point-to-point. Sheetmesh generalises that to N clusters
with a shared **Mesh** spreadsheet as the rendezvous — the multi-peer topology the roadmap
called for. It borrows Sheeternetes' own control model: nobody pushes work to anyone else
(which would need everyone's tokens in a shared sheet). Instead:

  * every cluster runs an **agent** that publishes its capacity to the Mesh sheet and
    reconciles the assignments addressed to *it* — using its own local token;
  * a **stretch** planner reads the whole mesh's free capacity and writes a desired split
    (which cluster runs how many replicas) into the Mesh sheet — no tokens required to plan.

Mesh sheet tabs:
  Members     : name | apiserver | cpu_total | cpu_free | last_seen
  Assignments : deploy | member | replicas | cpu | mem | image

  # on each cluster: publish capacity + run its own assignments
  sheetmesh.py agent  --mesh <sheet-id> --name A --apiserver http://localhost:8801 --token secret --interval 10

  # from anywhere (read-only plan, or write the split):
  sheetmesh.py view    --mesh <sheet-id>
  sheetmesh.py stretch web --replicas 20 --cpu 300 --mesh <sheet-id> --home A [--plan]

Auth: SHEETSOP_CREDS (authorized-user JSON) for the Mesh sheet; each agent holds only its
own cluster token. Requires google-api-python-client, google-auth.
"""
import argparse, json, os, time, urllib.parse, urllib.request
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

MEMBERS = "Members"
ASSIGN = "Assignments"
MEMBER_COLS = ["name", "apiserver", "cpu_total", "cpu_free", "last_seen"]
ASSIGN_COLS = ["deploy", "member", "replicas", "cpu", "mem", "image"]
MEMBER_TTL = int(os.environ.get("MESH_TTL", "60"))   # a member is stale after this many seconds

def _svc():
    path = os.environ.get("SHEETSOP_CREDS") or os.path.expanduser("~/.sheetsop/creds.json")
    c = Credentials.from_authorized_user_info(json.load(open(path)))
    if not c.valid:
        c.refresh(Request())
    return build("sheets", "v4", credentials=c, cache_discovery=False).spreadsheets()

def _ensure(ss, sid):
    from googleapiclient.errors import HttpError
    have = {s["properties"]["title"] for s in ss.get(spreadsheetId=sid).execute()["sheets"]}
    for tab, cols in ((MEMBERS, MEMBER_COLS), (ASSIGN, ASSIGN_COLS)):
        if tab not in have:
            try: ss.batchUpdate(spreadsheetId=sid, body={"requests": [{"addSheet": {"properties": {"title": tab}}}]}).execute()
            except HttpError as e:
                if "already exists" not in str(e): raise
        if not ss.values().get(spreadsheetId=sid, range=f"{tab}!A1:F1").execute().get("values", []):
            ss.values().update(spreadsheetId=sid, range=f"{tab}!A1", valueInputOption="RAW",
                               body={"values": [cols]}).execute()

def _records(ss, sid, tab, cols):
    rows = ss.values().get(spreadsheetId=sid, range=f"{tab}!A1:F5000").execute().get("values", [])
    return [dict(zip(cols, (r + [""] * len(cols))[:len(cols)])) for r in rows[1:] if r and r[0]]

def _write(ss, sid, tab, cols, dicts):
    body = [cols] + [[d.get(c, "") for c in cols] for d in dicts]
    ss.values().clear(spreadsheetId=sid, range=f"{tab}!A1:F5000").execute()
    ss.values().update(spreadsheetId=sid, range=f"{tab}!A1", valueInputOption="RAW", body={"values": body}).execute()

# ------------------------------------------------------------------ apiserver I/O
def api_get(base, token, kind):
    url = base + ("&" if "?" in base else "?") + urllib.parse.urlencode({"token": token, "kind": kind})
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.loads(r.read()).get("items", [])

def api_post(base, payload):
    req = urllib.request.Request(base, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode()

def _free_cpu(nodes):
    free = tot = 0
    for n in nodes:
        fresh = str(n.get("status", "")).lower() not in ("notready", "")
        sched = str(n.get("schedulable", "true")).lower() not in ("false", "0", "no")
        ct = int(n.get("cpu_total") or 0)
        tot += ct
        if fresh and sched:
            free += max(0, ct - int(n.get("cpu_used") or 0))
    return tot, free

# ------------------------------------------------------------------ agent
def agent(sid, name, base, token, interval):
    ss = _svc(); _ensure(ss, sid)
    print(f"[mesh agent] {name} @ {base} -> mesh {sid}")
    while True:
        # 1) publish my capacity — APPEND only my own row (many agents write this tab
        #    concurrently, so a read-modify-write of the whole tab would clobber peers).
        tot, free = _free_cpu(api_get(base, token, "nodes"))
        ss.values().append(spreadsheetId=sid, range=f"{MEMBERS}!A1", valueInputOption="RAW",
            insertDataOption="INSERT_ROWS",
            body={"values": [[name, base, tot, free, int(time.time())]]}).execute()
        # 2) reconcile the assignments addressed to ME, with my own token
        mine = [a for a in _records(ss, sid, ASSIGN, ASSIGN_COLS) if a["member"] == name]
        deps = [{"name": a["deploy"], "image": a.get("image") or "nginx:alpine",
                 "replicas": int(a.get("replicas") or 0), "cpu_req": int(a.get("cpu") or 100),
                 "mem_req": int(a.get("mem") or 64), "command": ""} for a in mine]
        if deps:
            api_post(base, {"token": token, "action": "apply", "deployments": deps})
            print(f"  reconciled {len(deps)} assignment(s): " + ", ".join(f"{d['name']}x{d['replicas']}" for d in deps))
        if interval <= 0:
            break
        time.sleep(interval)

# ------------------------------------------------------------------ planner
def live_members(ss, sid):
    now = int(time.time())
    latest = {}   # name -> newest heartbeat row (Members is an append-only log)
    for m in _records(ss, sid, MEMBERS, MEMBER_COLS):
        seen = int(m.get("last_seen") or 0)
        if m["name"] not in latest or seen >= int(latest[m["name"]].get("last_seen") or 0):
            latest[m["name"]] = m
    out = []
    for m in latest.values():
        m["cpu_free"] = int(m.get("cpu_free") or 0); m["cpu_total"] = int(m.get("cpu_total") or 0)
        m["fresh"] = now - int(m.get("last_seen") or 0) < MEMBER_TTL
        out.append(m)
    return sorted(out, key=lambda m: m["name"])

def plan_stretch(members, replicas, cpu, home):
    """Fill the home cluster first, then spill across the rest by most free CPU. Pure."""
    fresh = [m for m in members if m["fresh"]]
    order = ([m for m in fresh if m["name"] == home] +
             sorted([m for m in fresh if m["name"] != home], key=lambda m: -m["cpu_free"]))
    plan, left = {}, replicas
    for m in order:
        if left <= 0: break
        fit = min(left, m["cpu_free"] // max(1, cpu))
        if fit > 0:
            plan[m["name"]] = fit; left -= fit
    return plan, left   # left > 0 => that many replicas fit nowhere

def stretch(sid, deploy, replicas, cpu, mem, image, home, apply=True):
    ss = _svc(); _ensure(ss, sid)
    members = live_members(ss, sid)
    plan, unplaced = plan_stretch(members, replicas, cpu, home)
    total_free = sum(m["cpu_free"] for m in members if m["fresh"])
    print(f"[mesh stretch] {deploy} x{replicas} @ {cpu}m across {sum(1 for m in members if m['fresh'])} live members "
          f"(free {total_free}m):")
    for name, n in plan.items():
        print(f"    {name:10} <- {n}")
    if unplaced:
        print(f"    (unschedulable: {unplaced} — mesh is out of capacity)")
    if apply:
        # replace this deploy's assignments with the new split
        others = [a for a in _records(ss, sid, ASSIGN, ASSIGN_COLS) if a["deploy"] != deploy]
        rows = others + [{"deploy": deploy, "member": name, "replicas": n, "cpu": cpu, "mem": mem, "image": image}
                         for name, n in plan.items()]
        _write(ss, sid, ASSIGN, ASSIGN_COLS, rows)
        print("    written to the Mesh — each member's agent will reconcile its share.")
    return {"plan": plan, "unplaced": unplaced}

def view(sid):
    ss = _svc(); _ensure(ss, sid)
    members = live_members(ss, sid); assigns = _records(ss, sid, ASSIGN, ASSIGN_COLS)
    print(f"=== Mesh {sid} ===\nMembers:")
    for m in members:
        flag = "" if m["fresh"] else "  (stale)"
        print(f"  {m['name']:10} {m['apiserver']:32} free {m['cpu_free']}/{m['cpu_total']}m{flag}")
    print("Assignments (desired):")
    by = {}
    for a in assigns: by.setdefault(a["deploy"], []).append(f"{a['member']}x{a['replicas']}")
    for dep, parts in sorted(by.items()):
        print(f"  {dep:12} -> {', '.join(parts)}")

def main():
    ap = argparse.ArgumentParser(description="Sheetmesh — N-cluster federation over a rendezvous sheet")
    sub = ap.add_subparsers(dest="cmd")
    ag = sub.add_parser("agent"); ag.add_argument("--mesh", required=True); ag.add_argument("--name", required=True)
    ag.add_argument("--apiserver", required=True); ag.add_argument("--token", required=True)
    ag.add_argument("--interval", type=int, default=10)
    st = sub.add_parser("stretch"); st.add_argument("deploy"); st.add_argument("--mesh", required=True)
    st.add_argument("--replicas", type=int, required=True); st.add_argument("--cpu", type=int, default=100)
    st.add_argument("--mem", type=int, default=64); st.add_argument("--image", default="nginx:alpine")
    st.add_argument("--home", required=True, help="the cluster to fill first")
    st.add_argument("--plan", action="store_true", help="show the split without writing it")
    vw = sub.add_parser("view"); vw.add_argument("--mesh", required=True)
    a = ap.parse_args()
    if a.cmd == "agent":    agent(a.mesh, a.name, a.apiserver, a.token, a.interval)
    elif a.cmd == "stretch": stretch(a.mesh, a.deploy, a.replicas, a.cpu, a.mem, a.image, a.home, apply=not a.plan)
    elif a.cmd == "view":    view(a.mesh)
    else: ap.print_help()

if __name__ == "__main__":
    main()
