"""Concurrent requests against one workbook: heartbeats, applies and reads interleaving
over the threaded HTTP server must neither crash a reader nor lose an update."""
import json, os, sys, importlib, threading, urllib.request
from concurrent.futures import ThreadPoolExecutor
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from http.server import ThreadingHTTPServer


def test_parallel_requests_are_serialized(tmp_path):
    os.environ["WORKBOOK"] = str(tmp_path / "cluster.xlsx"); os.environ["TOKEN"] = "t"
    import apiserver; importlib.reload(apiserver)
    apiserver._wb()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), apiserver.H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}/"

    def post(body):
        req = urllib.request.Request(url, data=json.dumps({"token": "t", **body}).encode())
        with urllib.request.urlopen(req, timeout=30) as r: return json.loads(r.read())

    def get(kind):
        with urllib.request.urlopen(f"{url}?token=t&kind={kind}", timeout=30) as r:
            return json.loads(r.read())

    def job(i):
        if i % 3 == 0:
            return post({"action": "apply", "deployments": [{"name": f"d{i}", "image": "nginx", "replicas": 1}]})
        if i % 3 == 1:
            return post({"node": "a", "ip": "1", "cpu_total": 64000, "mem_total": 65536, "pods": []})
        return get("pods")

    try:
        with ThreadPoolExecutor(12) as ex:
            results = list(ex.map(job, range(60)))
    finally:
        srv.shutdown()
    assert all("error" not in r for r in results)
    assert len(apiserver.read_tab("deployments")) == 20            # no lost update
    assert not [f for f in os.listdir(tmp_path) if f.startswith(".~sk-")]   # no temp leftovers
