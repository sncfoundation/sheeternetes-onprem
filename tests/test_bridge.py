"""Unit tests for cross-substrate live migration in bridge.py — fully offline via
injected get/post/clock fakes. Verifies make-before-break ordering and safety."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from bridge import migrate, federated_view

SRC = {"base": "SRC", "token": "x"}
DST = {"base": "DST", "token": "y"}
SPEC = [{"name": "web", "image": "nginx", "replicas": 2}]


def harness(src_deploys, ready_after_polls):
    st = {"polls": 0, "t": 0.0, "applied": [], "deleted": [], "order": []}
    def get_fn(base, token, kind):
        st["order"].append(("get", base, kind))
        if base == "SRC" and kind == "deployments":
            return src_deploys
        if base == "DST" and kind == "pods":
            phase = "Running" if st["polls"] >= ready_after_polls else "Pending"
            return [{"deployment": "web", "phase": phase}] * 2
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

def run(src_deploys, ready_after, **kw):
    st, g, p, s, n = harness(src_deploys, ready_after)
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
