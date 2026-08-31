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
keeps pods sticky to their node, ages out silent nodes after `NODE_TTL` (**failover**), and
honors `cordon`/`drain`. It hands each node its pod set — so the bundled `kubelet.sh` actually
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

**Node maintenance** (kubectl-style):

```bash
./skctl cordon a          # a takes no new pods (existing ones keep running)
./skctl drain  a          # evict a's pods onto other nodes, then cordon a
./skctl uncordon a        # a is schedulable again
./skctl migrate web-1 b   # move one pod to node b (make-before-break)
```

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
python3 bridge.py --local http://localhost:8787 --local-token secret \
                  --peer https://script.google.com/macros/s/XXXX/exec --peer-token secret2 --push
```

Trust: a shared token/HMAC. Consistency: eventually-consistent (no consensus across substrates).
Latency: bounded by the peer's sync rate (Apps Script triggers ~1/min).

## Roadmap

- `.ods` + Python-UNO runtime; a VBA polling kubelet; a shared-file (no-server) transport.
- Scheduler: taints/tolerations and node labels/affinity (bin-packing, cordon, drain, migrate — done).
- HMAC signatures on bridge payloads; a rendezvous "Mesh" tab; multi-peer topology.
- Cross-substrate live migration in `bridge.py` (local `.xlsx` ↔ Google Sheets).

Tracking: [on-prem edition](https://github.com/sncfoundation/sheeternetes/issues/45) ·
[hybrid federation](https://github.com/sncfoundation/sheetmesh/issues/1)

---
<sub>Apache-2.0. Do not run production on a desktop spreadsheet. If you do, please film the save dialog.</sub>
