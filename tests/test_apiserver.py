"""Integration tests over a real temp workbook (openpyxl), exercising the control
plane end to end: apply, heartbeat/scheduling, scale, cordon, drain, migrate, and
schema upgrade of an older workbook. No HTTP and no Docker required."""
import os, sys, importlib
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pytest


@pytest.fixture
def api(tmp_path):
    """Fresh apiserver module bound to an isolated workbook per test."""
    os.environ["WORKBOOK"] = str(tmp_path / "cluster.xlsx")
    os.environ["TOKEN"] = "t"
    os.environ["NODE_TTL"] = "30"
    import apiserver
    importlib.reload(apiserver)
    return apiserver


def names_on(api, node):
    return sorted(p["name"] for p in api.read_tab("pods") if p["node"] == node)

def all_pods(api):
    return {p["name"]: p for p in api.read_tab("pods")}


def test_apply_then_heartbeat_places_pods(api):
    api.upsert_deployment({"name": "web", "image": "nginx", "replicas": 3,
                           "cpu_req": 100, "mem_req": 64, "command": ""})
    resp = api.heartbeat("node-a", "10.0.0.1", 4000, 8192, [])
    assert sorted(p["name"] for p in resp["pods"]) == ["web-1", "web-2", "web-3"]
    assert all(p["desired"] == "Running" for p in resp["pods"])
    assert names_on(api, "node-a") == ["web-1", "web-2", "web-3"]

def test_two_nodes_spread(api):
    api.upsert_deployment({"name": "web", "image": "nginx", "replicas": 4,
                           "cpu_req": 100, "mem_req": 64})
    api.heartbeat("a", "1", 4000, 8192, [])
    api.heartbeat("b", "2", 4000, 8192, [])
    # a second round lets the scheduler see both nodes as Ready and rebalance new pods
    api.heartbeat("a", "1", 4000, 8192, [])
    pods = api.read_tab("pods")
    assert len(pods) == 4

def test_scale_down_removes_pods(api):
    api.upsert_deployment({"name": "web", "image": "nginx", "replicas": 3, "cpu_req": 100, "mem_req": 64})
    api.heartbeat("a", "1", 4000, 8192, [])
    assert len(api.read_tab("pods")) == 3
    api.scale("web", 1)
    api.heartbeat("a", "1", 4000, 8192, [])
    assert [p["name"] for p in api.read_tab("pods")] == ["web-1"]

def test_cordon_marks_status_and_blocks_scheduling(api):
    api.upsert_deployment({"name": "web", "image": "nginx", "replicas": 1, "cpu_req": 100, "mem_req": 64})
    api.heartbeat("a", "1", 4000, 8192, [])
    api.heartbeat("b", "2", 4000, 8192, [])
    api.set_schedulable("a", False)
    nodes = {n["name"]: n for n in api.read_tab("nodes")}
    assert nodes["a"]["status"] == "SchedulingDisabled"
    # a fresh deployment's new pod must not land on the cordoned node
    api.upsert_deployment({"name": "new", "image": "nginx", "replicas": 1, "cpu_req": 100, "mem_req": 64})
    api.heartbeat("b", "2", 4000, 8192, [])
    assert all_pods(api)["new-1"]["node"] == "b"

def test_drain_moves_pods_off(api):
    api.upsert_deployment({"name": "web", "image": "nginx", "replicas": 2, "cpu_req": 100, "mem_req": 64})
    api.heartbeat("a", "1", 4000, 8192, [])       # both land on a (only node)
    api.heartbeat("b", "2", 4000, 8192, [])       # b joins; pods stay sticky on a
    assert names_on(api, "a") == ["web-1", "web-2"]
    api.drain("a")
    assert names_on(api, "a") == []
    assert names_on(api, "b") == ["web-1", "web-2"]
    nodes = {n["name"]: n for n in api.read_tab("nodes")}
    assert nodes["a"]["status"] == "SchedulingDisabled"

def test_migrate_pins_pod_to_target(api):
    api.upsert_deployment({"name": "web", "image": "nginx", "replicas": 1, "cpu_req": 100, "mem_req": 64})
    api.heartbeat("a", "1", 4000, 8192, [])
    api.heartbeat("b", "2", 4000, 8192, [])
    assert api.migrate("web-1", "b") == "migrating web-1 -> b"
    assert all_pods(api)["web-1"]["node"] == "b"
    # and it sticks across the next heartbeat
    api.heartbeat("b", "2", 4000, 8192, [])
    assert all_pods(api)["web-1"]["node"] == "b"

def test_migrate_rejects_unknown_target(api):
    api.upsert_deployment({"name": "web", "image": "nginx", "replicas": 1, "cpu_req": 100, "mem_req": 64})
    api.heartbeat("a", "1", 4000, 8192, [])
    assert api.migrate("web-1", "ghost") == "unknown node"

def test_cpu_used_reflects_allocation(api):
    api.upsert_deployment({"name": "web", "image": "nginx", "replicas": 2, "cpu_req": 250, "mem_req": 64})
    api.heartbeat("a", "1", 4000, 8192, [])
    a = {n["name"]: n for n in api.read_tab("nodes")}["a"]
    assert int(a["cpu_used"]) == 500

def test_label_then_node_selector_places_pod(api):
    api.upsert_deployment({"name": "gpu", "image": "nginx", "replicas": 1,
                           "cpu_req": 100, "mem_req": 64, "node_selector": "accel=gpu"})
    api.heartbeat("a", "1", 4000, 8192, [])
    api.heartbeat("b", "2", 4000, 8192, [])
    assert all_pods(api)["gpu-1"]["phase"] == "Unschedulable"   # nothing matches yet
    api.label_node("b", "accel=gpu")
    api.heartbeat("b", "2", 4000, 8192, [])
    assert all_pods(api)["gpu-1"]["node"] == "b"           # now b matches the selector

def test_taint_evicts_untolerating_pods(api):
    api.upsert_deployment({"name": "web", "image": "nginx", "replicas": 1, "cpu_req": 100, "mem_req": 64})
    api.heartbeat("a", "1", 4000, 8192, [])
    api.heartbeat("b", "2", 4000, 8192, [])
    assert all_pods(api)["web-1"]["node"] == "a"
    api.taint_node("a", "maint=true:NoSchedule")           # taint reschedules off a
    assert all_pods(api)["web-1"]["node"] == "b"

def test_taint_removal_restores_schedulability(api):
    assert api.taint_node("a", "x=y:NoSchedule") == "not found"   # no such node yet
    api.heartbeat("a", "1", 4000, 8192, [])
    assert api.taint_node("a", "x=y:NoSchedule") == "ok"
    nodes = {n["name"]: n for n in api.read_tab("nodes")}
    assert "x=y:NoSchedule" in str(nodes["a"]["taints"])
    assert api.taint_node("a", "x-") == "ok"               # remove by key
    nodes = {n["name"]: n for n in api.read_tab("nodes")}
    assert "x=y" not in str(nodes["a"]["taints"] or "")

def test_schema_upgrade_adds_schedulable(tmp_path):
    # build an OLD workbook: Nodes without the schedulable column
    import openpyxl
    wbpath = tmp_path / "old.xlsx"
    wb = openpyxl.Workbook(); wb.remove(wb.active)
    wb.create_sheet("Deployments").append(["name", "image", "replicas", "cpu_req", "mem_req", "command"])
    wb.create_sheet("Nodes").append(["name", "ip", "cpu_total", "cpu_used", "mem_total", "status", "last_heartbeat"])
    ns = wb["Nodes"]; ns.append(["a", "1", 4000, 0, 8192, "Ready", 0])
    wb.create_sheet("Pods").append(["name", "deployment", "node", "phase", "container_id"])
    wb.create_sheet("Events").append(["ts", "kind", "object", "message"])
    wb.save(wbpath)

    os.environ["WORKBOOK"] = str(wbpath); os.environ["TOKEN"] = "t"
    import apiserver; importlib.reload(apiserver)
    nodes = apiserver.read_tab("nodes")
    assert "schedulable" in nodes[0]                       # column was added
    assert apiserver._truthy(nodes[0]["schedulable"]) is True   # backfilled default
