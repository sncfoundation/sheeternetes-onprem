"""Live end-to-end: a real apiserver + a real kubelet.sh + real Docker.

Opt-in (pulls images, binds host ports):  SK_E2E=1 python3 -m pytest -q tests/test_e2e_docker.py
or `make e2e`. Everything it creates is labelled with a throwaway node name and removed
afterwards (containers on that node + the throwaway network); nothing else is touched.
Host ports used: SK_E2E_PORT_BASE (default 18600) .. +20.
"""
import json, os, shutil, socket, subprocess, sys, time, urllib.request, uuid
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE = int(os.environ.get("SK_E2E_PORT_BASE", "18600"))


def _docker_ok():
    if not (shutil.which("docker") and shutil.which("jq") and shutil.which("curl")):
        return False
    return subprocess.run(["docker", "info"], capture_output=True).returncode == 0

pytestmark = pytest.mark.skipif(os.environ.get("SK_E2E") != "1" or not _docker_ok(),
                                reason="live Docker e2e: set SK_E2E=1 (needs docker, jq, curl)")


def _free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p

def http_get(url, host=None, timeout=3):
    req = urllib.request.Request(url, headers={"Host": host} if host else {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode(errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")
    except OSError:
        return None, ""

def wait_for(pred, timeout=90, every=1.0):
    end = time.time() + timeout; last = None
    while time.time() < end:
        last = pred()
        if last: return last
        time.sleep(every)
    raise AssertionError(f"timed out waiting (last={last!r})")


class Cluster:
    def __init__(self, tmp):
        self.id = "e2e" + uuid.uuid4().hex[:6]
        self.node, self.net, self.token = f"{self.id}-node", f"{self.id}-net", "t"
        self.port = _free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        env = dict(os.environ, WORKBOOK=str(tmp / "cluster.xlsx"), TOKEN=self.token,
                   PORT=str(self.port), NODE_TTL="60")
        self.api = subprocess.Popen([sys.executable, os.path.join(ROOT, "apiserver.py")], env=env,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        wait_for(lambda: http_get(f"{self.url}/?token=t&kind=pods")[0] == 200, timeout=20, every=0.3)
        self.log = open(tmp / "kubelet.log", "w")
        kenv = dict(os.environ, WEBAPP_URL=self.url, TOKEN=self.token, NODE_NAME=self.node,
                    NODE_IP="127.0.0.1", CPU_TOTAL="4000", MEM_TOTAL="4096", INTERVAL="2",
                    SK_NET=self.net)
        self.kubelet = subprocess.Popen(["bash", os.path.join(ROOT, "kubelet.sh")], env=kenv,
                                        stdout=self.log, stderr=subprocess.STDOUT)

    def post(self, body):
        req = urllib.request.Request(self.url, data=json.dumps({"token": self.token, **body}).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())

    def get(self, kind):
        with urllib.request.urlopen(f"{self.url}/?token={self.token}&kind={kind}", timeout=10) as r:
            return json.loads(r.read())["items"]

    def pods(self):
        return {p["name"]: p for p in self.get("pods")}

    def pod(self, name):
        return self.pods().get(name) or {}

    def close(self):
        for p in (self.kubelet, self.api):
            p.terminate()
            try: p.wait(10)
            except subprocess.TimeoutExpired: p.kill()
        self.log.close()
        ids = subprocess.run(["docker", "ps", "-aq", "--filter", f"label=sheeternetes.node={self.node}"],
                             capture_output=True, text=True).stdout.split()
        if ids: subprocess.run(["docker", "rm", "-f", *ids], capture_output=True)
        subprocess.run(["docker", "network", "rm", self.net], capture_output=True)


@pytest.fixture
def cluster(tmp_path):
    c = Cluster(tmp_path)
    try: yield c
    finally:
        c.close()
        print(open(tmp_path / "kubelet.log").read()[-3000:])


def hello(name, text, ports=""):
    return {"name": name, "image": "busybox:stable", "replicas": 1, "cpu_req": 50, "mem_req": 16,
            "ports": ports,
            "command": f"mkdir -p /www && echo {text} > /www/index.html && httpd -f -p 8080 -h /www"}


def test_published_port_and_endpoint_report(cluster):
    c = cluster; svc = f"{c.id}-hello"
    fixed = BASE
    assert c.post({"action": "apply", "deployments": [hello(svc, "hi-from-a-cell", f"{fixed}:8080")]}) \
        == {"applied": ["created"]}
    wait_for(lambda: http_get(f"http://127.0.0.1:{fixed}/")[1].strip() == "hi-from-a-cell")
    ep = wait_for(lambda: c.pod(f"{svc}-1").get("endpoints"))
    assert ep == f"127.0.0.1:{fixed}->8080/tcp"

    # changing the ports cell re-creates the pod on the new host port (spec_hash)
    moved = BASE + 1
    c.post({"action": "apply", "deployments": [{"name": svc, "ports": f"{moved}:8080"}]})
    wait_for(lambda: http_get(f"http://127.0.0.1:{moved}/")[1].strip() == "hi-from-a-cell")
    wait_for(lambda: http_get(f"http://127.0.0.1:{fixed}/")[0] is None)

def test_two_replicas_one_fixed_port_one_node(cluster):
    c = cluster; svc = f"{c.id}-pin"
    spec = hello(svc, "pinned", f"{BASE + 2}:8080"); spec["replicas"] = 2
    c.post({"action": "apply", "deployments": [spec]})
    wait_for(lambda: c.pod(f"{svc}-1").get("phase") == "Running")
    assert c.pod(f"{svc}-2")["phase"] == "Unschedulable"   # host port already bound


def test_sheetgate_routes_host_and_path_and_hot_reloads(cluster):
    c = cluster; gw = c.id; listen = BASE + 3
    hello_svc, web_svc = f"{c.id}-hello", f"{c.id}-web"
    c.post({"action": "apply",
            "deployments": [hello(hello_svc, "hello-through-sheetgate"), hello(web_svc, "web-page")],
            "gateways": [{"name": gw, "listen": listen}],
            "routes": [
                {"name": f"{gw}-hello", "gateway": gw, "host": "hello.localhost",
                 "service": hello_svc, "port": 8080},
                {"name": f"{gw}-web", "gateway": gw, "path": "/web", "service": web_svc,
                 "port": 8080, "rewrite": "/"},
            ]})
    url = f"http://127.0.0.1:{listen}"
    # host routing
    wait_for(lambda: http_get(f"{url}/", host="hello.localhost")[1].strip() == "hello-through-sheetgate")
    # path routing with prefix rewrite (/web -> /), and the hostname-less route also
    # applies under the named host
    assert http_get(f"{url}/web/")[1].strip() == "web-page"
    assert http_get(f"{url}/web", host="hello.localhost")[1].strip() == "web-page"
    # no route -> SheetGate's 404
    status, body = http_get(f"{url}/nothing-here")
    assert status == 404 and "sheetgate: no route" in body
    # status written back to the sheet
    g = wait_for(lambda: next((x for x in c.get("gateways") if x["status"] == "Programmed"), None))
    assert g["address"] == f"127.0.0.1:{listen}"
    assert {r["status"] for r in c.get("routes")} == {"Accepted"}

    # edit a cell: point the hello host at the web service -> the gateway hot-reloads
    cid = c.pod(f"sheetgate-{gw}-1")["container_id"]
    c.post({"action": "apply", "routes": [{"name": f"{gw}-hello", "service": web_svc}]})
    wait_for(lambda: http_get(f"{url}/", host="hello.localhost")[1].strip() == "web-page")
    assert c.pod(f"sheetgate-{gw}-1")["container_id"] == cid     # reloaded, not restarted

    # a route whose service doesn't exist (yet) is a 502, never a failed reload
    c.post({"action": "apply", "routes": [{"name": f"{gw}-later", "gateway": gw, "path": "/later",
                                           "service": f"{c.id}-later"}]})
    wait_for(lambda: http_get(f"{url}/later")[0] == 502)
    assert http_get(f"{url}/web/")[1].strip() == "web-page"
