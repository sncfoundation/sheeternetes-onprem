"""Unit tests for the pure scheduler core (no I/O, no HTTP, no workbook)."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from apiserver import schedule


def node(name, cpu=4000, mem=8192, fresh=True, schedulable=True, labels=None, taints=None):
    return {"name": name, "cpu_total": cpu, "mem_total": mem,
            "fresh": fresh, "schedulable": schedulable,
            "labels": labels or {}, "taints": taints or []}

def dep(name, replicas=1, cpu=100, mem=64, image="nginx", node_selector=None, tolerations=None):
    return {"name": name, "image": image, "replicas": replicas,
            "cpu_req": cpu, "mem_req": mem, "command": "",
            "node_selector": node_selector or {}, "tolerations": tolerations or set()}

def placed(desired):
    return {p: d["node"] for p, d in desired.items()}


def test_spreads_across_ready_nodes():
    desired, alloc = schedule([dep("web", replicas=4)],
                              [node("a"), node("b")], existing={})
    on_a = [p for p, n in placed(desired).items() if n == "a"]
    on_b = [p for p, n in placed(desired).items() if n == "b"]
    assert len(on_a) == 2 and len(on_b) == 2      # even spread by most-free-CPU
    assert alloc["a"]["cpu"] == 200 and alloc["b"]["cpu"] == 200

def test_sticky_placement_is_stable():
    existing = {"web-1": {"node": "b"}, "web-2": {"node": "a"}}
    desired, _ = schedule([dep("web", replicas=2)],
                          [node("a"), node("b")], existing)
    assert placed(desired) == {"web-1": "b", "web-2": "a"}   # unchanged

def test_failover_when_node_not_fresh():
    existing = {"web-1": {"node": "a"}, "web-2": {"node": "a"}}
    desired, _ = schedule([dep("web", replicas=2)],
                          [node("a", fresh=False), node("b")], existing)
    assert set(placed(desired).values()) == {"b"}           # both moved off dead a

def test_pod_too_big_is_unschedulable():
    desired, _ = schedule([dep("hog", replicas=1, cpu=9000)],
                          [node("a", cpu=4000)], existing={})
    assert placed(desired) == {"hog-1": ""}                 # fits nowhere

def test_capacity_bin_packing_limits_pods():
    # each pod wants 2000m; a 4000m node holds exactly 2, a 2000m node holds 1.
    desired, _ = schedule([dep("svc", replicas=4, cpu=2000)],
                          [node("a", cpu=4000), node("b", cpu=2000)], existing={})
    p = placed(desired)
    assert sum(1 for n in p.values() if n == "a") == 2
    assert sum(1 for n in p.values() if n == "b") == 1
    assert sum(1 for n in p.values() if n == "") == 1       # the 4th fits nowhere

def test_memory_is_also_a_constraint():
    desired, _ = schedule([dep("mem", replicas=2, cpu=100, mem=6000)],
                          [node("a", cpu=4000, mem=8192)], existing={})
    p = placed(desired)
    assert sum(1 for n in p.values() if n == "a") == 1      # only one 6000MiB pod fits
    assert sum(1 for n in p.values() if n == "") == 1

def test_cordon_blocks_new_pods_but_keeps_existing():
    existing = {"web-1": {"node": "a"}}                     # already on cordoned a
    desired, _ = schedule([dep("web", replicas=2)],
                          [node("a", schedulable=False), node("b")], existing)
    p = placed(desired)
    assert p["web-1"] == "a"                                # kept on cordoned node
    assert p["web-2"] == "b"                                # new pod avoids cordon

def test_drain_evicts_via_exclude():
    existing = {"web-1": {"node": "a"}, "web-2": {"node": "a"}}
    desired, _ = schedule([dep("web", replicas=2)],
                          [node("a"), node("b")], existing, exclude={"a"})
    assert set(placed(desired).values()) == {"b"}           # drained off a

def test_unschedulable_when_all_nodes_cordoned():
    desired, _ = schedule([dep("web", replicas=1)],
                          [node("a", schedulable=False)], existing={})
    assert placed(desired) == {"web-1": ""}

def test_node_selector_pins_to_matching_labels():
    desired, _ = schedule([dep("gpu-job", replicas=2, node_selector={"disk": "ssd"})],
                          [node("a", labels={"disk": "ssd"}), node("b", labels={"disk": "hdd"})],
                          existing={})
    assert set(placed(desired).values()) == {"a"}          # only the ssd node matches

def test_node_selector_unschedulable_when_no_match():
    desired, _ = schedule([dep("x", replicas=1, node_selector={"zone": "eu"})],
                          [node("a", labels={"zone": "us"})], existing={})
    assert placed(desired) == {"x-1": ""}

def test_taint_repels_pods_without_toleration():
    desired, _ = schedule([dep("web", replicas=1)],
                          [node("a", taints=[("gpu", "true", "NoSchedule")]), node("b")],
                          existing={})
    assert placed(desired) == {"web-1": "b"}               # avoids the tainted node

def test_toleration_allows_scheduling_onto_taint():
    desired, _ = schedule([dep("ml", replicas=1, tolerations={"gpu=true"})],
                          [node("a", taints=[("gpu", "true", "NoSchedule")])], existing={})
    assert placed(desired) == {"ml-1": "a"}                # tolerated -> lands there

def test_sticky_pod_moves_when_node_label_no_longer_matches():
    existing = {"web-1": {"node": "a"}}
    desired, _ = schedule([dep("web", replicas=1, node_selector={"disk": "ssd"})],
                          [node("a", labels={"disk": "hdd"}), node("b", labels={"disk": "ssd"})],
                          existing)
    assert placed(desired) == {"web-1": "b"}               # affinity overrides stickiness
