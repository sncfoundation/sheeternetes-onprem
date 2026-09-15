<p align="center"><img src="https://sncfoundation.github.io/logos/sheetos.svg" width="96" alt="Sheeternetes on-prem"></p>
<h1 align="center">Sheeternetes on-prem</h1>
<p align="center"><b>A local, offline apiserver over Excel / LibreOffice — and a bridge to federate with Google Sheets clusters.</b><br>
A <a href="https://sncfoundation.github.io">Sheet-Native Computing Foundation</a> project · bare-metal &amp; hybrid</p>
<p align="center"><a href="https://github.com/sncfoundation/sheeternetes-onprem/actions/workflows/ci.yml"><img src="https://github.com/sncfoundation/sheeternetes-onprem/actions/workflows/ci.yml/badge.svg" alt="CI"></a></p>

---

**Status:** 📝 Unsaved Draft (working starter). Bare-metal + hybrid federation.

## Bare-metal (Excel / LibreOffice)

`apiserver.py` turns a **local desktop spreadsheet** into a Sheeternetes control plane —
same verb contract as the Google Apps Script apiserver, so `kubelet.sh` and `skctl` point at
it unchanged. It also **schedules**: on each kubelet heartbeat it **bin-packs** Deployment replicas onto
Ready, schedulable nodes by `cpu_req`/`mem_req` (a pod that fits nowhere is `Unschedulable`),
keeps pods sticky to their node, ages out silent nodes after `NODE_TTL` (**failover**), honors
`cordon`/`drain`, and respects **node affinity** (`node_selector` vs node labels) and
**taints/tolerations**. It hands each node its pod set — so the bundled `kubelet.sh` actually
runs your containers on bare metal. No internet required (air-gap-friendly): the spreadsheet is
the store, this process is the apiserver. The scheduler core is a pure, unit-tested function.

Everything ships in this repo: `apiserver.py` (control plane + scheduler), `kubelet.sh` (node
agent, needs docker), `skctl` (CLI), a `Makefile`, and `lab/hello-web.json`.

```bash
pip install openpyxl
cp .skctl.env.example .skctl.env      # WEBAPP_URL=http://localhost:8787, TOKEN=...

make up                               # terminal 1: apiserver over cluster.xlsx on :8787
make node                             # terminal 2: a kubelet (this host becomes a node)
make apply                            # terminal 3: apply lab/hello-web.json
make pods                             # watch the scheduler place & run them
./skctl scale web 4                   # scale; kubelet converges docker to match
```

### Storage backends (no vendor lock)

The control plane is a spreadsheet — but not necessarily *Google's*. `storage.py` makes the
backing store pluggable; the apiserver picks it from `WORKBOOK`, and nothing else changes.
Vendor neutrality is, after all, the whole point of a foundation.

| `WORKBOOK` | Backend | Needs |
|---|---|---|
| `cluster.xlsx` | Excel / OpenPyXL (default) | `openpyxl` |
| `cluster.ods` | **LibreOffice / OpenDocument** | `pip install odfpy` |
| `csvdir:/path` or `/path/` | **A directory of CSVs** — one file per tab | stdlib only |
| `cryptpad:<blob-url>` | **CryptPad** blob (self-hosted, end-to-end) — experimental | `requests` |

```bash
WORKBOOK=cluster.ods       TOKEN=secret python3 apiserver.py    # a LibreOffice Calc file
WORKBOOK=csvdir:/data/cl   TOKEN=secret python3 apiserver.py    # plain CSVs — sync the dir
```

The CSV-directory backend is the serverless answer to "get me off Google": point `WORKBOOK` at a
folder and let **Syncthing / Nextcloud / Dropbox** replicate it — the sheet stays the source of
truth, with no server and no cloud vendor. (CryptPad has no server-side per-cell API, so that
backend stores the whole workbook as one encrypted `.ods` blob; it's marked experimental.)

**Node maintenance** (kubectl-style):

```bash
./skctl cordon a          # a takes no new pods (existing ones keep running)
./skctl drain  a          # evict a's pods onto other nodes, then cordon a
./skctl uncordon a        # a is schedulable again
./skctl migrate web-1 b   # move one pod to node b (make-before-break)
./skctl label a disk=ssd  # set a node label (disk- to remove)
./skctl taint a gpu=true:NoSchedule   # repel pods that don't tolerate it (gpu- to remove)
```

**Affinity & taints.** A Deployment can pin itself with `node_selector` (e.g. `disk=ssd`) and
carry `tolerations` (e.g. `gpu=true`) — a pod only lands on a node whose labels satisfy the
selector and whose `NoSchedule` taints it tolerates. Set these as columns in the Deployments
tab, or in an applied manifest:

```json
{ "name": "ml", "image": "tensorflow", "replicas": 1, "cpu_req": 500, "mem_req": 512,
  "node_selector": "accel=gpu", "tolerations": "gpu=true" }
```

**SICF native images (the image lives in the sheet).** Besides normal OCI references
(`image: nginx:alpine`, pulled from a registry), a workload can run an image stored **inside the
workbook** — `image: sicf:<name>`. Pack one with [`sheetbuild`](https://github.com/sncfoundation/sci)
(`sheetbuild import doom.tar --store cluster.xlsx`); the kubelet then resolves `sicf:` via
`sicf.py` — it fetches the layers from the apiserver, verifies every sha256, `docker load`s the
image, and runs it. Execution stays on the node; the sheet only stores + schedules. See
[`lab/doom/`](lab/doom/) for the full "run DOOM from a spreadsheet" walkthrough.

- **Excel:** the `.xlsx` opens in Excel; edit workloads in the Deployments tab, the apiserver serves them.
- **LibreOffice Calc:** openpyxl reads `.xlsx` only — in Calc do **Save As → Excel 2007-365 (.xlsx)**.
  (Native `.ods` + Python-UNO is on the roadmap.)
- **Multi-node:** run `kubelet.sh` on other machines with `WEBAPP_URL=http://<apiserver-host>:8787`;
  each becomes a node and the scheduler spreads pods across them. A node that stops heartbeating
  goes `NotReady` after `NODE_TTL` (30s) and its pods are rescheduled onto survivors.
- Or coordinate with **no server at all** via a **shared file** (SMB/NFS) — see the roadmap.

## Hybrid federation (local ↔ Google Sheets)

On-prem clusters can't be reached inbound, so `bridge.py` **dials out**: it reads the local
apiserver and reconciles against a **rendezvous** peer (a Google Sheets Apps Script cluster, or
another node), giving cross-substrate service discovery.

```bash
# federated service view (+ --push to publish local deployments to the peer)
python3 bridge.py status  --local http://localhost:8787 --local-token secret \
                          --peer https://script.google.com/macros/s/XXXX/exec --peer-token secret2 --push

# live-migrate a deployment across substrates — make-before-break, zero downtime
python3 bridge.py migrate web --from local --to peer \
                          --local http://localhost:8787 --local-token secret \
                          --peer https://script.google.com/macros/s/XXXX/exec --peer-token secret2
```

`migrate` copies the deployment spec to the target, **waits until it reports Ready there**, and
only then drains it from the source — if the target never comes up, the source is left intact
(no downtime). With `--rollback-window N` it then watches the target for `N` seconds and, if it
degrades below the replica count, **automatically restores the deployment on the source** and
removes it from the target.

`sync` keeps both substrates federated: a union reconcile that pushes each side's missing
deployments to the other (the owning side stays authoritative, so it never fights a rename or a
scale). Run it once, or as a daemon with `--interval N`:

```bash
python3 bridge.py sync --interval 60 --local http://localhost:8787 --local-token secret \
                       --peer https://script.google.com/macros/s/XXXX/exec --peer-token secret2
```

**Signed payloads (HMAC).** A shared token authenticates every request; for cross-substrate
traffic you can additionally require **HMAC-SHA256 signatures**. Start the apiserver with
`SIGNING_KEY=…` and every POST must carry `X-SNCF-Timestamp` + `X-SNCF-Signature` within
`SIGN_TTL` seconds (tamper- and replay-resistant). The bridge signs automatically when you pass
the matching key:

```bash
SIGNING_KEY=shared-secret WORKBOOK=cluster.xlsx python3 apiserver.py     # peer requires signatures
python3 bridge.py sync --peer-signing-key shared-secret --local … --peer …   # bridge signs its POSTs
```

Consistency: eventually-consistent (no consensus across substrates). Latency: bounded by the
peer's sync rate (Apps Script triggers ~1/min).

## Cross-substrate networking (Sheetwire)

Federation's hard part isn't discovery, it's **reachability**: two clusters behind NAT can't be
dialed inbound, but both can reach a shared Google Sheet. So `sheetwire.py` makes the sheet the
wire — a userspace TCP relay that serializes byte streams into cells and back. Neither side ever
opens a port; both only write and read the sheet.

```bash
# side B (hosts the service): read frames, dial the local Service
sheetwire.py serve  --wire <shared-sheet-id> --service web --target 127.0.0.1:8080

# side A (wants to reach it): expose it as a local port
sheetwire.py expose --wire <shared-sheet-id> --service web --listen 127.0.0.1:9080

# then, on side A:  curl localhost:9080  reaches side B's web — through the cells
```

The `Wire` tab is append-only frames: `conn | kind(open|data|close) | dir(a2b|b2a) | service |
payload(base64)`. Outbound frames are batched per tick to stay under the Sheets write quota
(~60/min/user), which makes this a **low-throughput, service-to-service** pipe (cross-cluster API
calls) — not a bulk data path.

**Validated two-site:** a `curl` on one host reached an HTTP service on a *different physical host*
purely through a shared sheet, with no inbound ports on either side. The whole TCP exchange
(`open → GET → 200 OK → body → close`) is visible as rows in the `Wire` tab.

## Stretching a cluster (pool the peer's capacity)

`bridge.py stretch` treats both clusters' nodes as one pool: it fills the local cluster first, then
**orders the remaining replicas from the peer**. From the outside it's one deployment spanning
on-prem Excel + Google Sheets; Sheetwire stitches its Service across substrates, and `migrate`
moves replicas between them.

```bash
# fill local, order the rest from the peer (add --plan to see the split without applying)
python3 bridge.py stretch web --replicas 10 --cpu 300 \
    --local http://localhost:8787 --local-token secret \
    --peer  https://script.google.com/macros/s/XXXX/exec --peer-token secret2
# -> web x10 @ 300m | local free 1000m -> 3, ordered from peer -> 7
```

## Sheetmesh — N clusters over a rendezvous sheet

`bridge.py` federates two clusters point-to-point. `sheetmesh.py` generalises that to **many**
clusters with a shared **Mesh** spreadsheet as the rendezvous. It reuses Sheeternetes' own
control model — nobody pushes work to anyone else (which would put every cluster's token in a
shared sheet). Instead each cluster runs an **agent** that publishes its capacity and reconciles
the assignments addressed to *it*, with its own local token; a **stretch** planner reads the
whole mesh's free capacity and writes a desired split — no tokens needed to plan.

```bash
# on each cluster: publish capacity + run its own assignments
sheetmesh.py agent --mesh <sheet-id> --name A --apiserver http://localhost:8801 --token secret --interval 10

# from anywhere: see the mesh, or spill a deployment across it by capacity
sheetmesh.py view    --mesh <sheet-id>
sheetmesh.py stretch web --replicas 20 --cpu 300 --mesh <sheet-id> --home A
# -> web x20 across 3 live members: A <- 3 (home, full), C <- 17 (most free); each agent applies its share
```

### Provisioning on-prem clusters *from* the sheet

You can also declare clusters in the Mesh sheet and have them brought up on-prem — a
spreadsheet-driven Cluster API. Add a row to the `Clusters` tab (`name | provisioner | node_cpu
| node_mem | port | state`) and a **provisioner** running on that host reconciles it into a real
local cluster: it seeds an `.xlsx`, starts an `apiserver` for it, marks the row `Running`, and
registers it into the mesh. It also serves as the mesh agent for the clusters it owns.

```bash
# on the on-prem host: watch the sheet, bring up the clusters declared for this host
sheetmesh.py provisioner --mesh <sheet-id> --host mac --base-port 8920 --interval 8

# declaring `edge-1` (2000m) and `edge-2` (5000m) in the Clusters tab brings them up:
#   edge-1  provisioner=mac  node 2000m -> Running :8920
#   edge-2  provisioner=mac  node 5000m -> Running :8921
# then `stretch web --replicas 20 --home edge-2` places 16 on edge-2, 4 on edge-1 — from the sheet.
```

The Mesh sheet has three tabs: `Members` (an append-only heartbeat log — each agent appends only
its own row, so concurrent writers never clobber each other; readers dedup by newest `last_seen`),
`Assignments` (the desired workload split, written by the planner, reconciled by each member), and
`Clusters` (declared clusters, reconciled by a provisioner). Demonstrated end to end: declare
clusters and a workload in a Google Sheet, and on-prem clusters come up and run their share. Next:
Sheetwire mesh routing (a service resolves to whichever member hosts it) and mixed Excel+Google
members in one mesh.

## A sheet-native runtime (WASM in a cell)

Docker is only the *executor*; the more sheet-native runtime is **WASM/WASI**. A `.wasm` module is
small enough to live entirely in a cell (base64 + sha256), and a WASI runtime needs nothing but the
bytes — no Docker daemon, no registry. `wasmlet.py` pulls a module out of a spreadsheet cell,
verifies its digest, and runs it with `wasmtime`:

```bash
wasmlet.py --store <sheet-id> --name hello:v1     # pull from a cell, verify sha256, run — no Docker
# -> pulled hello:v1 from a spreadsheet cell (158 bytes), sha256 OK
# -> hello from a spreadsheet cell
```

**Honest scope:** the whole federation stack above has been exercised on real Google Sheets and,
for Sheetwire, across two physical hosts. Everything else (stretch across a live Excel↔Sheets pair,
migration) has been demonstrated with real clusters but on a single machine; a full multi-host
production run is the next validation. Do not run production on any of this. It reconciles.

## Roadmap

- `.ods` + Python-UNO runtime; a VBA polling kubelet; a shared-file (no-server) transport.
- Scheduler: pod anti-affinity / topology spread (bin-packing, affinity, taints, cordon, drain, migrate — done).
- HMAC-signed bridge payloads (done); next: a rendezvous "Mesh" tab and multi-peer topology.
- Cross-substrate live migration, auto-rollback, and a two-way sync loop are in `bridge.py` (done).

Tracking: [on-prem edition](https://github.com/sncfoundation/sheeternetes/issues/45) ·
[hybrid federation](https://github.com/sncfoundation/sheetmesh/issues/1)

---
<sub>Apache-2.0. Do not run production on a desktop spreadsheet. If you do, please film the save dialog.</sub>
