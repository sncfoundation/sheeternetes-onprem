#!/usr/bin/env python3
"""
SheetGate — north-south ingress for Sheeternetes. Our Ingress / Gateway API.

Sheetlium already gives every Deployment an internal DNS name (a Service). SheetGate is
how the *outside world* gets in: one published entrypoint, host/path routing, all of
it declared in two tabs of the cluster spreadsheet.

  Gateways  name | listen | replicas | node_selector | image | address | status
  Routes    name | gateway | host | path | service | port | rewrite | status

A Gateway is reconciled into an ordinary Deployment (`sheetgate-<name>`, nginx) whose
container port 80 is published on the node at `listen` (or an auto-assigned NodePort),
so the scheduler places it like any workload and never double-books its host port.
Routes are HTTPRoute-flavored rows: `host` (exact, `*.wildcard`, or blank for any),
`path` (prefix match, longest wins), `service` (a Deployment name, i.e. a Sheetlium
alias), `port` (the service's container port, default 80), and an optional `rewrite`
that replaces the matched prefix (Gateway API's ReplacePrefixMatch).

The apiserver renders each gateway's nginx config from the Routes tab (pure functions
below) and hands it to the kubelet running the gateway pod, which validates it with
`nginx -t` and hot-reloads it. Edit a cell -> the gateway reloads. It reconciles.

Every value that reaches the config is validated against a strict grammar first: a
spreadsheet cell is user input, and user input does not get to write nginx directives.

  sheetgate.py render --gateway public [--url http://localhost:8787 --token secret]
      print the config the apiserver would ship to that gateway (debugging aid)
"""
import argparse, hashlib, json, os, re, sys, urllib.request

PREFIX = "sheetgate-"                                    # gateway Deployment name prefix
IMAGE = os.environ.get("SHEETGATE_IMAGE", "nginx:alpine")
CONTAINER_PORT = 80
RESOLVER = "127.0.0.11"                                  # Docker's embedded DNS (Sheetlium)

NAME_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,40}[a-z0-9])?$")
ROUTE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$")
LABEL = r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?"
HOST_RE = re.compile(rf"^(\*\.)?({LABEL}\.)*{LABEL}$")
PATH_RE = re.compile(r"^/[A-Za-z0-9._~/-]*$")
SERVICE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$")


def _s(v):
    return "" if v is None else str(v).strip()

def _port(v, default=None):
    v = _s(v)
    if not v: return default
    try: p = int(float(v))
    except ValueError: return None
    return p if 1 <= p <= 65535 else None

def norm_host(h):
    h = _s(h).lower()
    return "" if h in ("", "*") else h

def norm_path(p):
    p = _s(p) or "/"
    return p.rstrip("/") or "/"

# ---------------------------------------------------------------- validation

def validate_gateway(gw):
    """None if the Gateways row is well-formed, else a human-readable reason."""
    if not NAME_RE.match(_s(gw.get("name"))):
        return "name must be lowercase letters, digits and dashes"
    if _s(gw.get("listen")) and _port(gw.get("listen")) is None:
        return "listen must be a port number (1-65535) or blank for an auto NodePort"
    r = _s(gw.get("replicas"))
    if r and (not r.replace(".0", "").isdigit()):
        return "replicas must be a non-negative integer"
    return None

def validate_route(route):
    """None if the Routes row is well-formed, else a human-readable reason."""
    if not ROUTE_NAME_RE.match(_s(route.get("name"))):
        return "name must be letters, digits, '.', '_' or '-'"
    gw = _s(route.get("gateway"))
    if gw and not NAME_RE.match(gw):
        return "gateway must be a gateway name (or blank for every gateway)"
    host = norm_host(route.get("host"))
    if host and not HOST_RE.match(host):
        return "host must be a DNS name, '*.domain', or blank"
    if not PATH_RE.match(_s(route.get("path")) or "/"):
        return "path must start with '/' and use only [A-Za-z0-9._~/-]"
    if not SERVICE_RE.match(_s(route.get("service"))):
        return "service must be a Deployment name"
    if _port(route.get("port"), 80) is None:
        return "port must be 1-65535 (default 80)"
    rw = _s(route.get("rewrite"))
    if rw and not PATH_RE.match(rw):
        return "rewrite must be a path starting with '/'"
    return None

# ----------------------------------------------------------- gateway -> pods

def deployment_name(gateway):
    return PREFIX + _s(gateway)

def gateway_deployments(gateways):
    """Each valid Gateways row becomes a Deployment the scheduler places like any other."""
    out = []
    for gw in gateways:
        if validate_gateway(gw): continue
        listen = _port(gw.get("listen"))
        replicas = _s(gw.get("replicas"))
        out.append({
            "name": deployment_name(gw["name"]), "gateway": _s(gw["name"]),
            "image": _s(gw.get("image")) or IMAGE, "command": "",
            "replicas": int(float(replicas)) if replicas else 1,
            "cpu_req": 100, "mem_req": 64,
            "ports": f"{listen}:{CONTAINER_PORT}" if listen else str(CONTAINER_PORT),
            "node_selector": _s(gw.get("node_selector")), "tolerations": "",
        })
    return out

# ------------------------------------------------------------ route planning

def plan(gateways, routes, services):
    """Pure: which routes each gateway serves, and every route's status.

    gateways: Gateways rows; routes: Routes rows (sheet order = precedence);
    services: names of existing Deployments (Sheetlium aliases).
    Returns (attached, status):
      attached = {gateway: [route, ...]}   (validated, normalized, conflict-free)
      status   = {route_name: 'Accepted' | 'BackendNotFound' | 'NoSuchGateway' |
                              'Conflicted: <winner>' | 'Invalid: <reason>'}
    A route with a blank gateway attaches to every gateway. Two routes with the same
    host+path on one gateway: the earlier row wins (Gateway API: oldest wins)."""
    gws = [_s(g.get("name")) for g in gateways if not validate_gateway(g)]
    services = set(services)
    attached = {g: [] for g in gws}
    claimed = {g: {} for g in gws}
    status = {}
    for r in routes:
        name = _s(r.get("name"))
        if not name: continue
        err = validate_route(r)
        if err:
            status[name] = f"Invalid: {err}"; continue
        targets = [_s(r["gateway"])] if _s(r.get("gateway")) else gws
        if not targets or targets[0] not in attached:
            status[name] = "NoSuchGateway"; continue
        route = {"name": name, "host": norm_host(r.get("host")), "path": norm_path(r.get("path")),
                 "service": _s(r["service"]), "port": _port(r.get("port"), 80),
                 "rewrite": _s(r.get("rewrite"))}
        winner = None
        for g in targets:
            key = (route["host"], route["path"])
            if key in claimed[g]:
                winner = winner or claimed[g][key]; continue
            claimed[g][key] = name; attached[g].append(route)
        if winner:
            status[name] = f"Conflicted: {winner}"
        else:
            status[name] = "Accepted" if route["service"] in services else "BackendNotFound"
    return attached, status

# ----------------------------------------------------------------- rendering

def _location(route, indent="    "):
    p, i = route["path"], indent
    body = [f"{i}    set $sheetgate_upstream {route['service']}:{route['port']};"]
    if route["rewrite"]:
        target = route["rewrite"].rstrip("/") + "/"
        prefix = "" if p == "/" else re.escape(p)
        body.append(f"{i}    rewrite ^{prefix}/?(.*)$ {target}$1 break;")
    body += [f"{i}    proxy_pass http://$sheetgate_upstream;",
             f"{i}    proxy_http_version 1.1;",
             f"{i}    proxy_set_header Host $host;",
             f"{i}    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;",
             f"{i}    proxy_set_header X-Forwarded-Proto $scheme;",
             f"{i}    proxy_set_header X-Forwarded-Host $host;",
             f"{i}    proxy_set_header Upgrade $http_upgrade;",
             f"{i}    proxy_set_header Connection $sheetgate_connection;",
             f"{i}    add_header X-SheetGate-Route {route['name']} always;"]
    blocks = []
    matchers = ["/"] if p == "/" else [f"= {p}", f"{p}/"]
    for m in matchers:
        blocks.append(f"{i}location {m} {{  # route {route['name']}\n" + "\n".join(body) + f"\n{i}}}")
    return blocks

def _server(server_name, routes, default=False):
    listen = f"listen {CONTAINER_PORT} default_server;" if default else f"listen {CONTAINER_PORT};"
    lines = ["server {", f"    {listen}", f"    server_name {server_name};"]
    for r in sorted(routes, key=lambda r: (-len(r["path"]), r["path"])):
        lines += _location(r)
    if not any(r["path"] == "/" for r in routes):
        lines += ["    location / {",
                  "        default_type text/plain;",
                  "        return 404 \"sheetgate: no route\\n\";",
                  "    }"]
    lines.append("}")
    return "\n".join(lines)

def render(gateway, routes):
    """Pure: the nginx conf.d file for one gateway, from its attached routes.

    Routes with a host get their own server block; routes without one go to the default
    server and also apply to every named host that doesn't define that path itself
    (a hostname-less HTTPRoute matches all hosts). Upstreams are resolved at request
    time through Docker DNS, so a missing Service is a 502, never a failed reload."""
    any_host = [r for r in routes if not r["host"]]
    by_host = {}
    for r in routes:
        if r["host"]: by_host.setdefault(r["host"], []).append(r)
    out = [f"# SheetGate gateway '{gateway}' — generated from the Routes tab.",
           "# Do not edit: edit the spreadsheet. It reconciles.",
           f"resolver {RESOLVER} valid=5s ipv6=off;",
           "map $http_upgrade $sheetgate_connection { default upgrade; '' close; }",
           "",
           _server("_", any_host, default=True)]
    for host in sorted(by_host):
        own = by_host[host]
        paths = {r["path"] for r in own}
        out += ["", _server(host, own + [r for r in any_host if r["path"] not in paths])]
    return "\n".join(out) + "\n"

def config_hash(text):
    return hashlib.sha256(text.encode()).hexdigest()[:12]

def configs(gateways, routes, services):
    """Pure: {gateway: (config_text, hash)} and the route status map, in one go."""
    attached, status = plan(gateways, routes, services)
    out = {}
    for g, rs in attached.items():
        text = render(g, rs); out[g] = (text, config_hash(text))
    return out, status

def gateway_status(gateways, pod_rows):
    """Pure: {gateway: (address, status)} from the Pods tab rows of its gateway pods."""
    out = {}
    for gw in gateways:
        name = _s(gw.get("name"))
        if not name: continue
        if validate_gateway(gw):
            out[name] = ("", f"Invalid: {validate_gateway(gw)}"); continue
        pods = [p for p in pod_rows if p.get("deployment") == deployment_name(name)]
        running = [p for p in pods if p.get("phase") == "Running"]
        address = ",".join(e.split("->")[0] for p in running
                           for e in _s(p.get("endpoints")).split(",") if e)
        if not pods:                                        status = "NoReplicas"
        elif running:                                       status = "Programmed"
        elif all(p.get("phase") == "Unschedulable" for p in pods): status = "Unschedulable"
        else:                                               status = "Pending"
        out[name] = (address, status)
    return out

# ----------------------------------------------------------------------- CLI

def _get(url, token, kind):
    with urllib.request.urlopen(f"{url}?token={token}&kind={kind}", timeout=30) as r:
        return json.loads(r.read())["items"]

def main(argv=None):
    ap = argparse.ArgumentParser(prog="sheetgate", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("render", help="print a gateway's generated nginx config")
    r.add_argument("--gateway", required=True)
    r.add_argument("--url", default=os.environ.get("WEBAPP_URL", "http://localhost:8787"))
    r.add_argument("--token", default=os.environ.get("TOKEN", "CHANGE_ME_super_secret"))
    a = ap.parse_args(argv)
    gws, routes = _get(a.url, a.token, "gateways"), _get(a.url, a.token, "routes")
    services = [d["name"] for d in _get(a.url, a.token, "deployments")]
    cfgs, status = configs(gws, routes, services)
    if a.gateway not in cfgs:
        sys.exit(f"no such gateway: {a.gateway}")
    sys.stdout.write(cfgs[a.gateway][0])
    for name, st in status.items():
        print(f"# route {name}: {st}", file=sys.stderr)

if __name__ == "__main__":
    main()
