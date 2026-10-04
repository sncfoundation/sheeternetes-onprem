"""The Google Sheets backend over an in-memory fake of the Sheets v4 API (the google
client modules are stubbed, so this runs without credentials or network)."""
import json, os, re, sys, types, importlib
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pytest


class _Exec:
    def __init__(self, fn): self.fn = fn
    def execute(self): return self.fn()

class FakeSheets:
    """Just enough of spreadsheets() for apiserver_sheets: tabs of string rows."""
    def __init__(self): self.tabs = {}

    @staticmethod
    def _parse(rng):
        tab, _, cells = rng.partition("!")
        m = re.match(r"([A-Z]+)(\d+)", cells)
        return tab, ord(m.group(1)[0]) - ord("A"), int(m.group(2)) - 1

    def get(self, spreadsheetId):
        return _Exec(lambda: {"sheets": [{"properties": {"title": t}} for t in self.tabs]})
    def batchUpdate(self, spreadsheetId, body):
        def run():
            for r in body["requests"]: self.tabs.setdefault(r["addSheet"]["properties"]["title"], [])
            return {}
        return _Exec(run)
    def get_values(self, rng):
        return [list(r) for r in self.tabs.get(rng.partition("!")[0], [])]
    def clear(self, spreadsheetId, range):
        return _Exec(lambda: self.tabs.__setitem__(range.partition("!")[0], []))
    def update(self, spreadsheetId, range, valueInputOption, body):
        def run():
            tab, col, row = self._parse(range)
            rows = self.tabs.setdefault(tab, [])
            for i, vals in enumerate(body["values"]):
                while len(rows) <= row + i: rows.append([])
                r = rows[row + i]
                while len(r) < col + len(vals): r.append("")
                for j, v in enumerate(vals): r[col + j] = "" if v is None else str(v)
            return {}
        return _Exec(run)

def _get(fake, spreadsheetId, range):
    return _Exec(lambda: {"values": fake.get_values(range)})


@pytest.fixture
def sheets(tmp_path, monkeypatch):
    fake = FakeSheets()

    class Values:
        def get(self, spreadsheetId, range): return _get(fake, spreadsheetId, range)
        def clear(self, **kw): return fake.clear(**kw)
        def update(self, **kw): return fake.update(**kw)

    class SS:
        def get(self, spreadsheetId): return fake.get(spreadsheetId)
        def batchUpdate(self, **kw): return fake.batchUpdate(**kw)
        def values(self): return Values()

    class Creds:
        valid = True
        @staticmethod
        def from_authorized_user_info(info): return Creds()

    mods = {
        "google": types.ModuleType("google"), "google.oauth2": types.ModuleType("google.oauth2"),
        "google.oauth2.credentials": types.ModuleType("google.oauth2.credentials"),
        "google.auth": types.ModuleType("google.auth"),
        "google.auth.transport": types.ModuleType("google.auth.transport"),
        "google.auth.transport.requests": types.ModuleType("google.auth.transport.requests"),
        "googleapiclient": types.ModuleType("googleapiclient"),
        "googleapiclient.discovery": types.ModuleType("googleapiclient.discovery"),
    }
    mods["google.oauth2.credentials"].Credentials = Creds
    mods["google.auth.transport.requests"].Request = object
    mods["googleapiclient.discovery"].build = lambda *a, **k: types.SimpleNamespace(spreadsheets=lambda: SS())
    for k, v in mods.items(): monkeypatch.setitem(sys.modules, k, v)
    creds = tmp_path / "creds.json"; creds.write_text(json.dumps({}))
    monkeypatch.setenv("CLUSTER_SHEET", "sheet-id"); monkeypatch.setenv("SHEETSOP_CREDS", str(creds))
    monkeypatch.setenv("WORKBOOK", str(tmp_path / "unused.xlsx"))
    import apiserver; importlib.reload(apiserver)
    sys.modules.pop("apiserver_sheets", None)
    import apiserver_sheets
    apiserver_sheets._ensure_tabs()
    return apiserver_sheets, fake


def test_sheets_backend_ports_and_gateway(sheets):
    api, fake = sheets
    assert api.apply_deps([{"name": "web", "image": "nginx", "replicas": 1, "cpu_req": 100,
                            "mem_req": 64, "ports": ["8080:80"]}]) == ["web"]
    assert api.apply_rows("Gateways", [{"name": "public", "listen": 8081}],
                          api.core.sheetgate.validate_gateway) == ["created"]
    assert api.apply_rows("Routes", [{"name": "r1", "gateway": "public", "path": "/", "service": "web"},
                                     {"name": "bad", "path": "/x y", "service": "web"}],
                          api.core.sheetgate.validate_route) == ["created", "invalid: path must start "
                                                                 "with '/' and use only [A-Za-z0-9._~/-]"]
    resp = api.heartbeat("a", "10.0.0.5", 4000, 8192, [])
    orders = {p["name"]: p for p in resp["pods"]}
    assert orders["web-1"]["ports"] == "8080:80/tcp"
    gw = orders["sheetgate-public-1"]
    assert gw["ports"] == "8081:80/tcp" and "set $sheetgate_upstream web:80;" in gw["gateway_config"]
    assert api.records("Routes")[0]["status"] == "Accepted"
    # the kubelet reports the gateway running -> Gateways tab shows the address
    api.heartbeat("a", "10.0.0.5", 4000, 8192,
                  [{"name": "sheetgate-public-1", "container_id": "c", "ports": "0.0.0.0:8081->80/tcp"}])
    g = api.records("Gateways")[0]
    assert (g["address"], g["status"]) == ("10.0.0.5:8081", "Programmed")
    assert api.records("Gateways")[0]["listen"] == "8081"          # user cells untouched
    assert api.delete("r1", "route") == "deleted" and api.records("Routes") == []
