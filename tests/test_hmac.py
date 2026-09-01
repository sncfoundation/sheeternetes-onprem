"""HMAC request signing: pure sign/verify units, cross-consistency between the
apiserver and the bridge, and HTTP integration (signed accepted; unsigned, tampered,
and stale rejected)."""
import os, sys, json, time, threading, importlib
import urllib.request, urllib.error
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pytest
import apiserver
import bridge


# ---- pure units -------------------------------------------------------------

def test_sign_verify_roundtrip():
    ts, body = "1000", b'{"a":1}'
    sig = apiserver.sign("k", ts, body)
    assert apiserver.verify("k", ts, sig, body, now=1000, ttl=300)

def test_verify_rejects_tampered_body():
    ts, body = "1000", b'{"a":1}'
    sig = apiserver.sign("k", ts, body)
    assert not apiserver.verify("k", ts, sig, b'{"a":2}', now=1000, ttl=300)

def test_verify_rejects_stale_timestamp():
    ts, body = "1000", b"{}"
    sig = apiserver.sign("k", ts, body)
    assert not apiserver.verify("k", ts, sig, body, now=1000 + 301, ttl=300)

def test_verify_rejects_wrong_key():
    ts, body = "1000", b"{}"
    sig = apiserver.sign("k", ts, body)
    assert not apiserver.verify("other", ts, sig, body, now=1000, ttl=300)

def test_verify_rejects_missing_parts():
    assert not apiserver.verify("k", "", "", b"{}", now=1, ttl=300)
    assert not apiserver.verify("", "1", "x", b"{}", now=1, ttl=300)

def test_bridge_and_apiserver_sign_identically():
    assert apiserver.sign("k", "5", b"body") == bridge.sign("k", "5", b"body")


# ---- HTTP integration -------------------------------------------------------

def _post(url, body, headers):
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()

@pytest.fixture
def signed_server(tmp_path):
    from http.server import ThreadingHTTPServer
    os.environ.update({"WORKBOOK": str(tmp_path / "c.xlsx"), "TOKEN": "t",
                       "SIGNING_KEY": "sekret", "SIGN_TTL": "300"})
    importlib.reload(apiserver)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), apiserver.H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        yield url
    finally:
        srv.shutdown()
        os.environ.pop("SIGNING_KEY", None)
        importlib.reload(apiserver)          # restore unsigned default for other tests

APPLY = {"token": "t", "action": "apply", "deployments": []}

def test_signed_post_accepted(signed_server):
    body = json.dumps(APPLY).encode()
    ts = str(int(time.time())); sig = apiserver.sign("sekret", ts, body)
    code, resp = _post(signed_server, body,
                       {"Content-Type": "application/json",
                        "X-SNCF-Timestamp": ts, "X-SNCF-Signature": sig})
    assert code == 200 and "applied" in resp

def test_unsigned_post_rejected(signed_server):
    code, resp = _post(signed_server, json.dumps(APPLY).encode(),
                       {"Content-Type": "application/json"})
    assert code == 401 and "signature" in resp

def test_tampered_body_rejected(signed_server):
    ts = str(int(time.time()))
    sig = apiserver.sign("sekret", ts, json.dumps(APPLY).encode())   # sign the benign body
    tampered = json.dumps({"token": "t", "action": "delete", "name": "web"}).encode()
    code, _ = _post(signed_server, tampered,
                    {"Content-Type": "application/json",
                     "X-SNCF-Timestamp": ts, "X-SNCF-Signature": sig})
    assert code == 401

def test_stale_timestamp_rejected(signed_server):
    body = json.dumps(APPLY).encode()
    ts = str(int(time.time()) - 400)                                # older than SIGN_TTL
    sig = apiserver.sign("sekret", ts, body)
    code, _ = _post(signed_server, body,
                    {"Content-Type": "application/json",
                     "X-SNCF-Timestamp": ts, "X-SNCF-Signature": sig})
    assert code == 401

def test_bridge_post_helper_is_accepted(signed_server):
    out = bridge.post(signed_server, APPLY, key="sekret")           # bridge signs for us
    assert "applied" in out
