"""SheetGate: route model, validation, nginx config generation, gateway scheduling and
the apiserver round trip. No Docker (the live path is in test_e2e_docker.py)."""
import os, sys, importlib
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pytest
import sheetgate as sg
from apiserver import schedule, normalize_deployments


def route(name, service="web", **kw):
    return {"name": name, "service": service, **kw}

GW = [{"name": "public", "listen": 8080}]


# --------------------------------------------------------------- validation

@pytest.mark.parametrize("bad", [
    {"path": "/x;return 200"}, {"path": "/a b"}, {"path": "/{x}"}, {"path": "no-slash"},
    {"host": "evil.com; }"}, {"host": "UPPER_case"}, {"host": "a..b"},
    {"service": "web;"}, {"service": ""}, {"port": "http"}, {"port": 70000},
    {"rewrite": "/x$1"}, {"gateway": "Public!"},
])
def test_validate_route_rejects_config_injection(bad):
    assert sg.validate_route(route("r", **bad))

def test_validate_route_accepts_normal_rows():
    for ok in ({}, {"host": "doom.localhost"}, {"host": "*.example.com"}, {"host": "*"},
               {"path": "/hello/v1"}, {"port": "8080"}, {"port": 8080.0}, {"rewrite": "/"}):
        assert sg.validate_route(route("r", **ok)) is None, ok

def test_validate_gateway():
    assert sg.validate_gateway({"name": "public", "listen": 8080}) is None
    assert sg.validate_gateway({"name": "public", "listen": ""}) is None       # auto NodePort
    assert sg.validate_gateway({"name": "Public"})
    assert sg.validate_gateway({"name": "p", "listen": "eighty"})
    assert sg.validate_gateway({"name": "p", "replicas": "-1"})


# ----------------------------------------------------------------- planning

def test_plan_statuses():
    routes = [route("a", path="/"), route("dup", path="/"), route("lost", gateway="nope"),
              route("nobackend", service="ghost", path="/g"), route("bad", path="x")]
    attached, status = sg.plan(GW, routes, {"web"})
    assert status == {"a": "Accepted", "dup": "Conflicted: a", "lost": "NoSuchGateway",
                      "nobackend": "BackendNotFound", "bad": status["bad"]}
    assert status["bad"].startswith("Invalid:")
    assert [r["name"] for r in attached["public"]] == ["a", "nobackend"]

def test_blank_gateway_attaches_everywhere_and_named_one_only_there():
    gws = [{"name": "public"}, {"name": "internal"}]
    attached, _ = sg.plan(gws, [route("all"), route("pub", gateway="public", path="/p")], {"web"})
    assert [r["name"] for r in attached["public"]] == ["all", "pub"]
    assert [r["name"] for r in attached["internal"]] == ["all"]

def test_route_without_gateways_is_unattached():
    assert sg.plan([], [route("r")], {"web"})[1] == {"r": "NoSuchGateway"}

def test_same_path_different_hosts_do_not_conflict():
    _, status = sg.plan(GW, [route("a", host="a.local"), route("b", host="b.local")], {"web"})
    assert status == {"a": "Accepted", "b": "Accepted"}


# ---------------------------------------------------------------- rendering

def render(routes, services=("web", "hello", "doom")):
    cfgs, _ = sg.configs(GW, routes, set(services))
    return cfgs["public"][0]

def test_render_host_routing_and_default_server():
    cfg = render([route("doom", service="doom", host="doom.localhost"),
                  route("web", path="/")])
    assert "listen 80 default_server;" in cfg and "server_name doom.localhost;" in cfg
    assert "set $sheetgate_upstream doom:80;" in cfg
    assert "resolver 127.0.0.11" in cfg                       # runtime Sheetlium DNS
    assert "proxy_pass http://$sheetgate_upstream;" in cfg

def test_render_prefix_match_is_segment_aware():
    cfg = render([route("hello", service="hello", path="/hello/", port=8080)])
    assert "location = /hello {" in cfg and "location /hello/ {" in cfg
    assert "location /hello {" not in cfg                     # would also match /helloworld
    assert "set $sheetgate_upstream hello:8080;" in cfg

def test_render_rewrite_replaces_prefix():
    cfg = render([route("h", service="hello", path="/hello", rewrite="/")])
    assert "rewrite ^/hello/?(.*)$ /$1 break;" in cfg
    cfg = render([route("h", service="hello", path="/v1.0/api", rewrite="/api")])
    assert r"rewrite ^/v1\.0/api/?(.*)$ /api/$1 break;" in cfg

def test_render_fallback_404_without_root_route():
    cfg = render([route("h", service="hello", path="/hello")])
    assert 'return 404 "sheetgate: no route\\n";' in cfg

def test_render_any_host_routes_apply_to_named_hosts_too():
    cfg = render([route("doom", service="doom", host="doom.localhost"),
                  route("hello", service="hello", path="/hello")])
    doom_server = cfg.split("server_name doom.localhost;")[1]
    assert "location /hello/ {" in doom_server               # hostname-less route matches all hosts

def test_render_host_route_shadows_any_host_route_on_same_path():
    cfg = render([route("doom", service="doom", host="doom.localhost"), route("web")])
    doom_server = cfg.split("server_name doom.localhost;")[1]
    assert "doom:80" in doom_server and "web:80" not in doom_server

def test_render_is_deterministic_and_hash_tracks_content():
    rs = [route("b", host="b.local"), route("a", host="a.local")]
    c1, c2 = sg.configs(GW, rs, {"web"})[0], sg.configs(GW, list(reversed(rs)), {"web"})[0]
    assert c1["public"][0].count("server {") == 3
    c3 = sg.configs(GW, [route("a", host="a.local", port=81)], {"web"})[0]
    assert c1["public"][1] != c3["public"][1]
    assert sg.configs(GW, rs, {"web"})[0]["public"][1] == c1["public"][1]


# ------------------------------------------------------- gateway as workload

def test_gateway_deployment_publishes_listen_port():
    d = sg.gateway_deployments([{"name": "public", "listen": 8080, "replicas": 2},
                                {"name": "auto"}, {"name": "BAD"}])
    assert [(x["name"], x["ports"], x["replicas"]) for x in d] == \
        [("sheetgate-public", "8080:80", 2), ("sheetgate-auto", "80", 1)]
    assert d[0]["image"] == sg.IMAGE and d[0]["gateway"] == "public"

def test_gateway_replicas_spread_by_host_port():
    nodes = [{"name": n, "cpu_total": 4000, "mem_total": 8192, "fresh": True, "schedulable": True,
              "labels": {}, "taints": []} for n in ("a", "b", "c")]
    deps = normalize_deployments([], [{"name": "public", "listen": 8080, "replicas": 3}])
    desired, _ = schedule(deps, nodes, {})
    assert {d["node"] for d in desired.values()} == {"a", "b", "c"}
    assert all(d["gateway"] == "public" for d in desired.values())

def test_gateway_owns_its_deployment_name():
    deps = normalize_deployments([{"name": "sheetgate-public", "image": "evil"}],
                                 [{"name": "public"}])
    assert [d["image"] for d in deps] == [sg.IMAGE]

def test_gateway_status():
    pods = [{"deployment": "sheetgate-public", "phase": "Running",
             "endpoints": "10.0.0.5:8080->80/tcp"},
            {"deployment": "sheetgate-public", "phase": "Unschedulable", "endpoints": ""}]
    st = sg.gateway_status([{"name": "public"}, {"name": "idle", "replicas": 0}, {"name": "X"}], pods)
    assert st["public"] == ("10.0.0.5:8080", "Programmed")
    assert st["idle"] == ("", "NoReplicas")
    assert st["X"][1].startswith("Invalid")


# --------------------------------------------------------- apiserver round trip

@pytest.fixture
def api(tmp_path):
    os.environ["WORKBOOK"] = str(tmp_path / "cluster.xlsx")
    os.environ["TOKEN"] = "t"
    import apiserver
    importlib.reload(apiserver)
    return apiserver

def test_apply_heartbeat_ships_config_and_writes_status(api):
    api.upsert_deployment({"name": "web", "image": "nginx", "replicas": 1, "cpu_req": 100, "mem_req": 64})
    assert api.upsert_gateway({"name": "public", "listen": 8080}) == "created"
    assert api.upsert_route({"name": "web", "gateway": "public", "path": "/", "service": "web"}) == "created"
    assert api.upsert_route({"name": "ghost", "path": "/g", "service": "ghost"}) == "created"
    assert api.upsert_route({"name": "evil", "path": "/;}", "service": "web"}).startswith("invalid")

    resp = api.heartbeat("a", "10.0.0.5", 4000, 8192, [])
    gw = next(p for p in resp["pods"] if p["name"] == "sheetgate-public-1")
    assert gw["ports"] == "8080:80/tcp" and gw["gateway"] == "public"
    assert "set $sheetgate_upstream web:80;" in gw["gateway_config"]
    assert len(gw["gateway_config_hash"]) == 12
    web = next(p for p in resp["pods"] if p["name"] == "web-1")
    assert "gateway_config" not in web

    routes = {r["name"]: r for r in api.read_tab("routes")}
    assert routes["web"]["status"] == "Accepted" and routes["ghost"]["status"] == "BackendNotFound"
    assert api.read_tab("gateways")[0]["status"] == "Pending"

    api.heartbeat("a", "10.0.0.5", 4000, 8192,
                  [{"name": "sheetgate-public-1", "container_id": "c", "ports": "0.0.0.0:8080->80/tcp"}])
    g = api.read_tab("gateways")[0]
    assert (g["address"], g["status"]) == ("10.0.0.5:8080", "Programmed")

def test_editing_a_route_changes_the_shipped_config(api):
    api.upsert_gateway({"name": "public", "listen": 8080})
    api.upsert_route({"name": "r", "path": "/", "service": "web"})
    h1 = next(p for p in api.heartbeat("a", "1", 4000, 8192, [])["pods"])["gateway_config_hash"]
    api.upsert_route({"name": "r", "service": "hello", "port": 8080})
    first = api.heartbeat("a", "1", 4000, 8192, [])["pods"][0]
    assert first["gateway_config_hash"] != h1 and "hello:8080" in first["gateway_config"]

def test_gateway_and_deployment_cannot_share_a_host_port(api):
    api.upsert_deployment({"name": "doom", "image": "nginx", "replicas": 1, "ports": "8080:80"})
    api.upsert_gateway({"name": "public", "listen": 8080})
    api.heartbeat("a", "1", 4000, 8192, [])
    phases = {p["name"]: p["phase"] for p in api.read_tab("pods")}
    assert sorted(phases.values()) == ["Pending", "Unschedulable"]

def test_delete_gateway_and_route(api):
    api.upsert_gateway({"name": "public"}); api.upsert_route({"name": "r", "service": "web"})
    assert api.delete("r", "route") == "deleted"
    assert api.delete("public", "gateway") == "deleted"
    assert api.delete("public", "gateway") == "not found"
    assert api.delete("x", "planet") == "unknown kind"
    assert api.heartbeat("a", "1", 4000, 8192, [])["pods"] == []

def test_schema_upgrade_adds_sheetgate_tabs(tmp_path):
    import openpyxl
    wbpath = tmp_path / "old.xlsx"
    wb = openpyxl.Workbook(); wb.remove(wb.active)
    wb.create_sheet("Deployments").append(["name", "image", "replicas"])
    wb.save(wbpath)
    os.environ["WORKBOOK"] = str(wbpath); os.environ["TOKEN"] = "t"
    import apiserver; importlib.reload(apiserver)
    assert apiserver.read_tab("gateways") == [] and apiserver.read_tab("routes") == []
