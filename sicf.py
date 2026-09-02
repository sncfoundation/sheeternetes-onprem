#!/usr/bin/env python3
"""
sicf.py — resolve a `sicf:<name>` image from the apiserver's in-sheet SICF store into a
local Docker image. The kubelet calls this before `docker run` when a pod's image starts
with `sicf:`.

  python3 sicf.py <apiserver-url> <token> sicf:doom:shareware
      -> reassembles the image from the Images/Layers tabs, verifies every layer's
         sha256, `docker load`s it, and prints the local image ref to run.

Fetch + reassemble + digest-verify is pure (unit-testable offline); `docker load` is the
only host step. Execution stays on the node — the spreadsheet only stores the image.
The store is populated by `sheetbuild import` (see the SICF reference tools in sci).
"""
import base64, hashlib, io, json, os, subprocess, sys, tarfile, tempfile
import urllib.request, urllib.parse

def _get(base, token, kind):
    url = base + ("&" if "?" in base else "?") + urllib.parse.urlencode({"token": token, "kind": kind})
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.loads(r.read()).get("items", [])

def _sha256(b):
    return "sha256:" + hashlib.sha256(b).hexdigest()

def materialize(base, token, ref, out_tar, get_fn=_get):
    """Build a docker-load-compatible tar for `sicf:<name>` from the in-sheet store.
    Verifies each layer's digest; raises on a missing image/layer or a mismatch."""
    name = ref[len("sicf:"):] if ref.startswith("sicf:") else ref
    img = next((r for r in get_fn(base, token, "images") if r.get("name") == name), None)
    if not img:
        raise KeyError(f"image {name} not found in store")
    by_digest = {}
    for r in get_fn(base, token, "layers"):
        by_digest.setdefault(r["digest"], []).append(r)

    config_blob = base64.b64decode(img["config"])
    config_name = hashlib.sha256(config_blob).hexdigest() + ".json"
    layer_digests = [d for d in str(img["layers"]).split(",") if d]

    with tarfile.open(out_tar, "w") as tar:
        def add(arcname, data):
            ti = tarfile.TarInfo(arcname); ti.size = len(data); tar.addfile(ti, io.BytesIO(data))
        add(config_name, config_blob)
        layer_paths = []
        for d in layer_digests:
            parts = sorted(by_digest.get(d, []), key=lambda r: int(r["ordinal"]))
            if not parts:
                raise KeyError(f"layer {d} missing from store")
            blob = base64.b64decode("".join(str(p["data"] or "") for p in parts))
            if _sha256(blob) != d:
                raise ValueError(f"digest mismatch for {d}: store is corrupt")
            path = d.split(":", 1)[1] + "/layer.tar"
            add(path, blob); layer_paths.append(path)
        add("manifest.json", json.dumps([{"Config": config_name, "RepoTags": [name],
                                          "Layers": layer_paths}]).encode())
    return name

def resolve(base, token, ref):
    """materialize + `docker load`; returns the local image ref for the kubelet to run."""
    out = tempfile.mktemp(suffix=".tar")
    try:
        name = materialize(base, token, ref, out)
        subprocess.run(["docker", "load", "-i", out], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return name
    finally:
        if os.path.exists(out):
            os.unlink(out)

if __name__ == "__main__":
    if len(sys.argv) != 4:
        sys.exit("usage: sicf.py <apiserver-url> <token> sicf:<name>")
    print(resolve(sys.argv[1], sys.argv[2], sys.argv[3]))
