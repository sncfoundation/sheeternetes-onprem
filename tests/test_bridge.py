"""Unit tests for cross-substrate live migration in bridge.py — fully offline via
injected get/post/clock fakes. Verifies make-before-break ordering and safety."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from bridge import migrate, federated_view, plan_sync, sync_once

SRC = {"base": "SRC", "token": "x"}
DST = {"base": "DST", "token": "y"}
SPEC = [{"name": "web", "image": "nginx", "replicas": 2}]


def harness(src_deploys, ready_after_polls, degrade_after_polls=None):
    st = {"polls": 0, "t": 0.0, "applied": [], "deleted": [], "order": []}
    def get_fn(base, token, kind):
        st["order"].append(("get", base, kind))
        if base == "SRC" and kind == "deployments":
            return src_deploys
        if base == "DST" and kind == "pods":
            up = st["polls"] >= ready_after_polls and \
                 (degrade_after_polls is None or st["polls"] < degrade_after_polls)
            return [{"deployment": "web", "phase": "Running" if up else "Pending"}] * 2
        return []
    def post_fn(base, payload):
        act = payload.get("action")
        st["order"].append(("post", base, act))
        if act == "apply":  st["applied"].append(base)
        if act == "delete": st["deleted"].append((base, payload.get("name")))
        return "{}"
    def sleep_fn(s): st["polls"] += 1; st["t"] += s
    def now_fn():   return st["t"]
    return st, get_fn, post_fn, sleep_fn, now_fn

def run(src_deploys, ready_after, degrade_after=None, **kw):
    st, g, p, s, n = harness(src_deploys, ready_after, degrade_after)
    res = migrate("web", SRC, DST, get_fn=g, post_fn=p, sleep_fn=s, now_fn=n,
                  log=lambda *a: None, **kw)
    return res, st


def test_make_before_break_order():
    res, st = run(SPEC, ready_after=2, wait=100, poll=1)
    assert res["ok"] and res["replicas"] == 2
    assert st["applied"] == ["DST"]
    assert st["deleted"] == [("SRC", "web")]
    posts = [o for o in st["order"] if o[0] == "post"]
    assert posts[0] == ("post", "DST", "apply")     # target comes up first
    assert posts[-1] == ("post", "SRC", "delete")   # source drained last

def test_timeout_leaves_source_intact():
    res, st = run(SPEC, ready_after=999, wait=2, poll=1)
    assert not res["ok"] and "timeout" in res["error"]
    assert st["applied"] == ["DST"]                 # target got the spec (harmless)
    assert st["deleted"] == []                      # source NOT drained -> no downtime

def test_missing_deployment_is_a_noop_error():
    res, st = run([], ready_after=0)
    assert not res["ok"] and "not found" in res["error"]
    assert st["applied"] == [] and st["deleted"] == []
    assert st["order"] == [("get", "SRC", "deployments")]

def test_skip_wait_applies_then_drains_without_polling():
    res, st = run(SPEC, ready_after=999, skip_wait=True)
    assert res["ok"]
    assert st["applied"] == ["DST"] and st["deleted"] == [("SRC", "web")]
    assert ("get", "DST", "pods") not in st["order"]   # never polled readiness

def test_federated_view_merges_by_name():
    view = federated_view([{"name": "web"}, {"name": "api"}], [{"name": "api"}, {"name": "db"}])
    assert view == {"web": "local", "api": "local", "db": "peer"}


# ---- rollback ---------------------------------------------------------------

def test_rollback_when_target_degrades_after_cutover():
    # ready immediately, then the target drops below replicas within the window
    res, st = run(SPEC, ready_after=0, degrade_after=1, wait=100, poll=1, rollback_window=5)
    assert res["ok"] is False and res.get("rolled_back") is True
    # cutover applied to DST, then rollback re-applied the spec to SRC
    assert st["applied"] == ["DST", "SRC"]
    # source was drained (step 3) then the degraded target was torn back down
    assert st["deleted"] == [("SRC", "web"), ("DST", "web")]

def test_no_rollback_when_target_stays_healthy():
    res, st = run(SPEC, ready_after=0, degrade_after=None, wait=100, poll=1, rollback_window=3)
    assert res["ok"] is True
    assert st["applied"] == ["DST"]              # only the initial cutover apply
    assert st["deleted"] == [("SRC", "web")]     # source drained, never restored


# ---- two-way sync -----------------------------------------------------------

def test_plan_sync_computes_missing_each_way():
    local = [{"name": "web"}, {"name": "api"}]
    peer  = [{"name": "api"}, {"name": "db"}]
    to_local, to_peer = plan_sync(local, peer)
    assert [d["name"] for d in to_local] == ["db"]     # peer-only -> push to local
    assert [d["name"] for d in to_peer] == ["web"]     # local-only -> push to peer

def test_sync_once_applies_both_directions():
    calls = {"applied": []}
    stores = {"LOCAL": [{"name": "web"}], "PEER": [{"name": "db"}]}
    def get_fn(base, token, kind): return stores[base]
    def post_fn(base, payload):
        calls["applied"].append((base, [d["name"] for d in payload["deployments"]]))
        return "{}"
    res = sync_once({"base": "LOCAL", "token": "x"}, {"base": "PEER", "token": "y"},
                    get_fn=get_fn, post_fn=post_fn, log=lambda *a: None)
    assert res == {"to_local": ["db"], "to_peer": ["web"]}
    assert ("LOCAL", ["db"]) in calls["applied"]
    assert ("PEER", ["web"]) in calls["applied"]

def test_sync_noop_when_already_in_sync():
    same = [{"name": "web"}]
    def get_fn(base, token, kind): return same
    posts = []
    def post_fn(base, payload): posts.append(base); return "{}"
    res = sync_once({"base": "L", "token": "x"}, {"base": "P", "token": "y"},
                    get_fn=get_fn, post_fn=post_fn, log=lambda *a: None)
    assert res == {"to_local": [], "to_peer": []} and posts == []
