"""Tests for the SICF runtime resolver (sicf.py). The fetch+reassemble+verify path is
pure (fake get_fn); one end-to-end test proves the apiserver actually serves the
Images/Layers store over HTTP. No Docker required (the `docker load` step is separate)."""
import os, sys, io, json, base64, tarfile, hashlib, threading, importlib
import urllib.request
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pytest
import sicf


def _d(b):
    return "sha256:" + hashlib.sha256(b).hexdigest()

def fake_store(layers, config=b'{"architecture":"amd64"}', name="doom:sw", max_cell=64):
    """Build Images/Layers rows the way sheetbuild would (chunked base64)."""
    images = [{"name": name, "digest": _d(config), "config": base64.b64encode(config).decode(),
               "layers": ",".join(_d(l) for l in layers), "created": "t",
               "size": sum(len(l) for l in layers)}]
    rows = []
    for l in layers:
        d = _d(l); b = base64.b64encode(l).decode()
        chunks = [b[i:i + max_cell] for i in range(0, len(b), max_cell)] or [""]
        for ordinal, ch in enumerate(chunks):
            rows.append({"digest": d, "ordinal": ordinal, "media_type": "x", "data": ch})
    return images, rows

def get_fn_for(images, rows):
    def g(base, token, kind):
        return images if kind == "images" else rows if kind == "layers" else []
    return g

def layers_from_tar(path):
    with tarfile.open(path) as t:
        m = json.load(t.extractfile("manifest.json"))[0]
        return [t.extractfile(p).read() for p in m["Layers"]], t.extractfile(m["Config"]).read()


# ---- pure resolver tests ----------------------------------------------------

def test_materialize_round_trip(tmp_path):
    layers = [b"DOOM engine + DOOM1.WAD payload" * 20]     # multi-chunk at max_cell=64
    images, rows = fake_store(layers)
    out = tmp_path / "img.tar"
    name = sicf.materialize("http://x", "t", "sicf:doom:sw", str(out), get_fn=get_fn_for(images, rows))
    assert name == "doom:sw"
    got_layers, cfg = layers_from_tar(str(out))
    assert got_layers == layers and cfg == b'{"architecture":"amd64"}'

def test_materialize_multi_layer_order(tmp_path):
    layers = [b"base", b"middle", b"top"]
    images, rows = fake_store(layers)
    out = tmp_path / "m.tar"
    sicf.materialize("http://x", "t", "sicf:doom:sw", str(out), get_fn=get_fn_for(images, rows))
    assert layers_from_tar(str(out))[0] == layers

def test_materialize_rejects_digest_mismatch(tmp_path):
    layers = [b"trust but verify"]
    images, rows = fake_store(layers)
    rows[0]["data"] = base64.b64encode(b"tampered").decode()   # corrupt the only chunk
    out = tmp_path / "b.tar"
    with pytest.raises(ValueError, match="digest mismatch"):
        sicf.materialize("http://x", "t", "sicf:doom:sw", str(out), get_fn=get_fn_for(images, rows))

def test_materialize_missing_image(tmp_path):
    with pytest.raises(KeyError):
        sicf.materialize("http://x", "t", "sicf:nope", str(tmp_path / "o.tar"),
                         get_fn=get_fn_for([], []))


# ---- end-to-end over the real apiserver -------------------------------------

@pytest.fixture
def live_store(tmp_path):
    from http.server import ThreadingHTTPServer
    os.environ.update({"WORKBOOK": str(tmp_path / "c.xlsx"), "TOKEN": "t"})
    os.environ.pop("SIGNING_KEY", None)
    import apiserver; importlib.reload(apiserver)
    # populate the Images/Layers tabs directly (as sheetbuild would)
    layers = [b"a real-ish layer blob " * 30]
    images, rows = fake_store(layers, name="doom:shareware")
    wb = apiserver._wb()
    for r in images:
        wb["Images"].append([r[c] for c in apiserver.TABS["Images"]])
    for r in rows:
        wb["Layers"].append([r["digest"], r["ordinal"], r["media_type"], r["data"]])
    wb.save(os.environ["WORKBOOK"])
    srv = ThreadingHTTPServer(("127.0.0.1", 0), apiserver.H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}", layers
    finally:
        srv.shutdown()

def test_e2e_apiserver_serves_store_and_resolver_reassembles(tmp_path, live_store):
    url, layers = live_store
    out = tmp_path / "e2e.tar"
    name = sicf.materialize(url, "t", "sicf:doom:shareware", str(out))   # real HTTP
    assert name == "doom:shareware"
    assert layers_from_tar(str(out))[0] == layers                        # byte-exact over the wire
