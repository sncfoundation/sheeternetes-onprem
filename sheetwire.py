#!/usr/bin/env python3
"""
Sheetwire — a cross-substrate data plane whose wire is a spreadsheet.

Federation's hard part isn't discovery, it's reachability: an on-prem cluster and a
Google Sheets cluster both sit behind NAT and can't be dialed inbound. But both CAN
reach a shared Google Sheet. So the sheet becomes the wire: a userspace TCP relay
serializes byte streams into cells and back, and neither side ever needs an open port.

  # side B (the cluster that HOSTS the service): read frames, dial the local Service
  sheetwire.py serve  --wire <sheet-id> --service web --target 127.0.0.1:8080

  # side A (the cluster that wants to REACH it): expose it as a local port
  sheetwire.py expose --wire <sheet-id> --service web --listen 127.0.0.1:9080

  # then, on side A, `curl localhost:9080` reaches side B's `web` — through the cells.

Transit tab `Wire` is append-only frames:
  seq(row) | conn | kind(open|data|close) | dir(a2b|b2a) | service | payload(base64)

Only outbound writes to the sheet are needed on both sides, so it traverses any NAT/firewall.
Honest limit: the Sheets write quota (~60/min/user) makes this a low-throughput,
service-to-service pipe (cross-cluster API calls), not a bulk data path. Outbound frames
are batched per tick to stay under quota. Auth/creds: SHEETSOP_CREDS (authorized-user JSON).
"""
import argparse, base64, json, os, socket, sys, threading, time, uuid
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

WIRE_TAB = "Wire"
COLS = ["conn", "kind", "dir", "service", "payload"]
CHUNK = 30000          # base64 chars per cell (well under the 50k Sheets cap)
TICK = float(os.environ.get("SHEETWIRE_TICK", "1.1"))   # poll/flush period (keeps writes <60/min)

def _svc():
    path = os.environ.get("SHEETSOP_CREDS") or os.path.expanduser("~/.sheetsop/creds.json")
    c = Credentials.from_authorized_user_info(json.load(open(path)))
    if not c.valid:
        c.refresh(Request())
    return build("sheets", "v4", credentials=c, cache_discovery=False).spreadsheets()

def ensure_wire(ss, sid):
    from googleapiclient.errors import HttpError
    meta = ss.get(spreadsheetId=sid).execute()
    if WIRE_TAB not in {s["properties"]["title"] for s in meta["sheets"]}:
        try:
            ss.batchUpdate(spreadsheetId=sid, body={"requests": [{"addSheet": {"properties": {"title": WIRE_TAB}}}]}).execute()
        except HttpError as e:
            if "already exists" not in str(e): raise   # concurrent side won the create — fine
    r = ss.values().get(spreadsheetId=sid, range=f"{WIRE_TAB}!A1:E1").execute().get("values", [])
    if not r:
        try:
            ss.values().update(spreadsheetId=sid, range=f"{WIRE_TAB}!A1", valueInputOption="RAW",
                               body={"values": [COLS]}).execute()
        except HttpError: pass

class Wire:
    """Shared append-only frame log over the Wire tab. One reader cursor per side."""
    def __init__(self, ss, sid, my_dir, peer_dir):
        self.ss, self.sid = ss, sid
        self.my_dir, self.peer_dir = my_dir, peer_dir   # I write my_dir, I read peer_dir
        self.cursor = 1                                  # rows consumed (incl header)
        self.outbox = []                                 # pending frames -> flushed per tick
        self.lock = threading.Lock()

    def send(self, conn, kind, service="", payload=b""):
        b64 = base64.b64encode(payload).decode()
        # shard oversized payloads across multiple data frames, in order
        parts = [b64[i:i+CHUNK] for i in range(0, len(b64), CHUNK)] or [""]
        with self.lock:
            for p in parts:
                self.outbox.append([conn, kind, self.my_dir, service, p])

    def flush(self):
        with self.lock:
            batch, self.outbox = self.outbox, []
        if batch:
            self.ss.values().append(spreadsheetId=self.sid, range=f"{WIRE_TAB}!A1",
                valueInputOption="RAW", insertDataOption="INSERT_ROWS",
                body={"values": batch}).execute()

    def poll(self):
        """Return new frames addressed to me (dir == peer_dir), advancing the cursor."""
        rng = f"{WIRE_TAB}!A{self.cursor+1}:E100000"
        rows = self.ss.values().get(spreadsheetId=self.sid, range=rng).execute().get("values", [])
        out = []
        for row in rows:
            self.cursor += 1
            row = (row + [""] * 5)[:5]
            conn, kind, d, service, payload = row
            if d == self.peer_dir:
                out.append((conn, kind, service, base64.b64decode(payload) if payload else b""))
        return out

# ---------------------------------------------------------------- serve (side B)
def run_serve(sid, service, target):
    ss = _svc(); ensure_wire(ss, sid)
    host, port = target.split(":"); port = int(port)
    wire = Wire(ss, sid, my_dir="b2a", peer_dir="a2b")
    conns = {}            # conn_id -> socket to the local target
    print(f"[sheetwire serve] service={service} target={target}  wire={sid}")

    def pump(conn_id, sock):
        try:
            while True:
                data = sock.recv(65536)
                if not data: break
                wire.send(conn_id, "data", payload=data)
        except OSError: pass
        finally:
            wire.send(conn_id, "close"); sock.close(); conns.pop(conn_id, None)

    while True:
        for conn_id, kind, svc, payload in wire.poll():
            if kind == "open" and svc == service:
                s = socket.create_connection((host, port))
                conns[conn_id] = s
                threading.Thread(target=pump, args=(conn_id, s), daemon=True).start()
            elif kind == "data" and conn_id in conns:
                try: conns[conn_id].sendall(payload)
                except OSError: pass
            elif kind == "close" and conn_id in conns:
                try: conns[conn_id].close()
                except OSError: pass
                conns.pop(conn_id, None)
        wire.flush()
        time.sleep(TICK)

# --------------------------------------------------------------- expose (side A)
def run_expose(sid, service, listen):
    ss = _svc(); ensure_wire(ss, sid)
    host, port = listen.split(":"); port = int(port)
    wire = Wire(ss, sid, my_dir="a2b", peer_dir="b2a")
    conns = {}            # conn_id -> local client socket
    print(f"[sheetwire expose] service={service} listen={listen}  wire={sid}")

    def pump(conn_id, sock):
        try:
            while True:
                data = sock.recv(65536)
                if not data: break
                wire.send(conn_id, "data", payload=data)
        except OSError: pass
        finally:
            wire.send(conn_id, "close"); sock.close(); conns.pop(conn_id, None)

    def accept_loop(srv):
        while True:
            sock, _ = srv.accept()
            conn_id = uuid.uuid4().hex[:12]
            conns[conn_id] = sock
            wire.send(conn_id, "open", service=service)
            threading.Thread(target=pump, args=(conn_id, sock), daemon=True).start()

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((host, port)); srv.listen(64)
    threading.Thread(target=accept_loop, args=(srv,), daemon=True).start()

    while True:
        for conn_id, kind, svc, payload in wire.poll():
            if kind == "data" and conn_id in conns:
                try: conns[conn_id].sendall(payload)
                except OSError: pass
            elif kind == "close" and conn_id in conns:
                try: conns[conn_id].close()
                except OSError: pass
                conns.pop(conn_id, None)
        wire.flush()
        time.sleep(TICK)

def main():
    ap = argparse.ArgumentParser(description="Sheetwire — cross-substrate TCP relay over a spreadsheet")
    sub = ap.add_subparsers(dest="cmd")
    for name in ("serve", "expose"):
        p = sub.add_parser(name); p.add_argument("--wire", required=True); p.add_argument("--service", required=True)
        p.add_argument("--target" if name == "serve" else "--listen", required=True)
    a = ap.parse_args()
    if a.cmd == "serve":   run_serve(a.wire, a.service, a.target)
    elif a.cmd == "expose": run_expose(a.wire, a.service, a.listen)
    else: ap.print_help()

if __name__ == "__main__":
    main()
