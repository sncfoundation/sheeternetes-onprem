#!/usr/bin/env python3
"""
Pluggable storage backends for the Sheeternetes apiserver — no vendor lock.

The apiserver works over an in-memory openpyxl Workbook (tabs of rows). This module
loads and saves that Workbook against different stores, picked by the WORKBOOK value:

  cluster.xlsx            Excel / OpenPyXL           (default)
  cluster.ods             LibreOffice / OpenDocument (odfpy)
  csvdir:/path/ , /path/  a directory of CSV files   (one per tab — sync it with
                          Syncthing / Nextcloud / Dropbox for a serverless mesh)
  cryptpad:<instance-url> CryptPad blob (experimental, self-hosted & end-to-end)

Every backend round-trips the same `openpyxl.Workbook`, so the apiserver's logic is
unchanged — only the substrate differs. That's the point: the Sheet stays the source of
truth, but it doesn't have to be Google's.
"""
import csv, io, os
import openpyxl

def _kind(path):
    if path.startswith("csvdir:") or path.endswith("/") or os.path.isdir(path):
        return "csvdir"
    if path.startswith("cryptpad:"):
        return "cryptpad"
    if path.endswith(".ods"):
        return "ods"
    return "xlsx"

def _csvdir_path(path):
    return path[len("csvdir:"):] if path.startswith("csvdir:") else path

# ------------------------------------------------------------------ xlsx (default)
def _load_xlsx(path):
    return openpyxl.load_workbook(path) if os.path.exists(path) else None

def _save_xlsx(wb, path):
    wb.save(path)

# ------------------------------------------------------------------ ods (LibreOffice)
def _load_ods(path):
    if not os.path.exists(path):
        return None
    from odf.opendocument import load
    from odf.table import Table, TableRow, TableCell
    from odf.text import P
    doc = load(path)
    wb = openpyxl.Workbook(); wb.remove(wb.active)
    for table in doc.spreadsheet.getElementsByType(Table):
        ws = wb.create_sheet(table.getAttribute("name"))
        for tr in table.getElementsByType(TableRow):
            row = []
            for tc in tr.getElementsByType(TableCell):
                rep = int(tc.getAttribute("numbercolumnsrepeated") or 1)
                text = "".join(str(p) for p in tc.getElementsByType(P))
                row.extend([text] * rep)
            while row and row[-1] == "":
                row.pop()
            ws.append(row)
    return wb

def _save_ods(wb, path):
    from odf.opendocument import OpenDocumentSpreadsheet
    from odf.table import Table, TableRow, TableCell
    from odf.text import P
    doc = OpenDocumentSpreadsheet()
    for name in wb.sheetnames:
        ws = wb[name]
        table = Table(name=name)
        for row in ws.iter_rows(values_only=True):
            tr = TableRow()
            for val in row:
                tc = TableCell(valuetype="string")
                tc.addElement(P(text="" if val is None else str(val)))
                tr.addElement(tc)
            table.addElement(tr)
        doc.spreadsheet.addElement(table)
    doc.save(path)

# ------------------------------------------------------------------ csvdir (synced files)
def _load_csvdir(path):
    d = _csvdir_path(path)
    if not os.path.isdir(d):
        return None
    files = [f for f in os.listdir(d) if f.endswith(".csv")]
    if not files:
        return None
    wb = openpyxl.Workbook(); wb.remove(wb.active)
    for f in sorted(files):
        ws = wb.create_sheet(f[:-4])
        with open(os.path.join(d, f), newline="") as fh:
            for row in csv.reader(fh):
                ws.append(row)
    return wb

def _save_csvdir(wb, path):
    d = _csvdir_path(path)
    os.makedirs(d, exist_ok=True)
    for name in wb.sheetnames:
        ws = wb[name]
        with open(os.path.join(d, f"{name}.csv"), "w", newline="") as fh:
            w = csv.writer(fh)
            for row in ws.iter_rows(values_only=True):
                w.writerow(["" if v is None else v for v in row])

# ------------------------------------------------------------------ cryptpad (experimental)
# CryptPad is end-to-end encrypted, so there is no server-side per-cell API. We store the
# whole workbook as one .ods blob in CryptPad's file store and round-trip that. Set the
# blob location + key via CRYPTPAD_URL (e.g. cryptpad:https://pad.example/blob/<id>#<key>).
def _load_cryptpad(path):
    import requests, tempfile
    url = path[len("cryptpad:"):]
    r = requests.get(url, timeout=30)
    if r.status_code == 404 or not r.content:
        return None
    r.raise_for_status()
    with tempfile.NamedTemporaryFile(suffix=".ods", delete=False) as f:
        f.write(r.content); tmp = f.name
    try:
        return _load_ods(tmp)
    finally:
        os.unlink(tmp)

def _save_cryptpad(wb, path):
    import requests, tempfile
    url = path[len("cryptpad:"):]
    with tempfile.NamedTemporaryFile(suffix=".ods", delete=False) as f:
        tmp = f.name
    try:
        _save_ods(wb, tmp)
        with open(tmp, "rb") as fh:
            requests.put(url, data=fh.read(), timeout=30).raise_for_status()
    finally:
        os.unlink(tmp)

_LOAD = {"xlsx": _load_xlsx, "ods": _load_ods, "csvdir": _load_csvdir, "cryptpad": _load_cryptpad}
_SAVE = {"xlsx": _save_xlsx, "ods": _save_ods, "csvdir": _save_csvdir, "cryptpad": _save_cryptpad}

def load(path):
    """Return an openpyxl Workbook from the backing store, or None if it doesn't exist yet."""
    return _LOAD[_kind(path)](path)

def save(wb, path):
    """Persist the openpyxl Workbook to the backing store chosen by `path`."""
    _SAVE[_kind(path)](wb, path)

def backend(path):
    return _kind(path)
