# DOOM as a SICF image — the whole game lives in the spreadsheet

DOOM is small enough to store entirely inside a workbook: shareware `DOOM1.WAD` (~4 MB) plus a
tiny web front-end is a few hundred base64 cells — far under the 10M-cell limit. The cluster then
runs it from there. **Execution is on the node** (real Docker); the spreadsheet only stores the
image and schedules the pod.

```bash
# 1. build a web-DOOM image on any Docker host
docker build -t doom:shareware lab/doom
docker save  doom:shareware -o doom.tar

# 2. pack it INTO the cluster workbook (layers -> base64 cells, sha256-addressed)
python3 /path/to/sci/tools/sheetbuild.py import doom.tar --name doom:shareware --store cluster.xlsx

# 3. deploy it — the image reference is sicf:, resolved from the sheet
./skctl apply lab/doom.json           # image: sicf:doom:shareware
./skctl get pods                      # doom-1  Running

# the kubelet saw sicf:, called sicf.py: fetched the layers from the apiserver,
# verified every sha256, docker-loaded the image, and ran it. Open the container's
# port 80 in a browser and play. It reconciles. (Now with more demons.)
```

The `sicf:` resolution is implemented in `sicf.py`; the pack/unpack tool is `sheetbuild` in the
[sci](https://github.com/sncfoundation/sci) repo. Tracked in sci#6 / sheeternetes#52.
