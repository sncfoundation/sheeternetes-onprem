<p align="center"><img src="https://sncfoundation.github.io/logos/sheetos.svg" width="96" alt="Sheeternetes on-prem"></p>
<h1 align="center">Sheeternetes on-prem</h1>
<p align="center"><b>A local, offline apiserver over Excel / LibreOffice — and a bridge to federate with Google Sheets clusters.</b><br>
A <a href="https://sncfoundation.github.io">Sheet-Native Computing Foundation</a> project · bare-metal &amp; hybrid</p>

---

**Status:** 📝 Unsaved Draft (working starter). Bare-metal + hybrid federation.

## Bare-metal (Excel / LibreOffice)

`apiserver.py` turns a **local desktop spreadsheet** into a Sheeternetes control plane —
same verb contract as the Google Apps Script apiserver, so `kubelet.sh` and `skctl` point at
it unchanged. No internet required (air-gap-friendly): the spreadsheet is the store, this
process is the apiserver.

```bash
pip install openpyxl
WORKBOOK=cluster.xlsx TOKEN=secret python3 apiserver.py      # serves on :8787
# then point a kubelet / skctl at http://<host>:8787
```

- **Excel:** the `.xlsx` opens in Excel; edit workloads in the Deployments tab, the apiserver serves them.
- **LibreOffice Calc:** save as `.xlsx` (or extend to `.ods`); Python-UNO is the alternative in-process runtime.
- Nodes on other machines reach it over the **LAN**, or coordinate via a **shared file** (SMB/NFS) with no server at all.

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
- HMAC signatures on bridge payloads; a rendezvous "Mesh" tab; multi-peer topology.

Tracking: [on-prem edition](https://github.com/sncfoundation/sheeternetes/issues/45) ·
[hybrid federation](https://github.com/sncfoundation/sheetmesh/issues/1)

---
<sub>Apache-2.0. Do not run production on a desktop spreadsheet. If you do, please film the save dialog.</sub>
