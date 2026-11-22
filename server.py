import datetime
import json
import os
import socket
import sys
import threading
import time
import webbrowser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("PO_PORT", "8080"))
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
DAILY_DIR = os.path.join(DATA_DIR, "daily")
DATA_FILE = os.path.join(DATA_DIR, "po-maker-data.json")
os.makedirs(DAILY_DIR, exist_ok=True)

LOCK = threading.Lock()


def get_lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def read_state():
    if not os.path.exists(DATA_FILE):
        return None
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def write_state(data, save_daily=False):
    tmp = DATA_FILE + ".tmp"
    text = json.dumps(data, ensure_ascii=False, indent=2)
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, DATA_FILE)
    if save_daily:
        day_tag = datetime.datetime.now().strftime("%Y-%m-%d")
        daily_file = os.path.join(DAILY_DIR, f"po-maker-{day_tag}.json")
        dtmp = daily_file + ".tmp"
        with open(dtmp, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(dtmp, daily_file)


def is_recent_iso(iso_str, hours=24):
    if not iso_str:
        return True
    try:
        s = str(iso_str).replace("Z", "+00:00")
        dt = datetime.datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        age = (datetime.datetime.now(datetime.timezone.utc) - dt).total_seconds()
        return age < hours * 3600
    except Exception:
        return True


def merge_states(existing, incoming, cleared_channel="", draft_owner=""):
    if not existing:
        return incoming
    out = dict(incoming)

    # Smart-merge pharmacy requests so concurrent counter submissions are never lost
    incoming_req_ids = {str(r.get("id")) for r in (incoming.get("requests") or [])}
    req_map = {}
    for r in (existing.get("requests") or []):
        k = str(r.get("id"))
        st = r.get("status") or "pending"
        if st == "pending" or is_recent_iso(r.get("date"), 24) or k in incoming_req_ids:
            req_map[k] = r
    for r in (incoming.get("requests") or []):
        k = str(r.get("id"))
        prev = req_map.get(k)
        if prev and prev.get("status") not in (None, "", "pending") and r.get("status") in (None, "", "pending"):
            req_map[k] = prev
        else:
            req_map[k] = r
    out["requests"] = sorted(req_map.values(), key=lambda x: str(x.get("date", "")))

    # Smart-merge messages & read receipts
    msg_map = {}
    for m in (existing.get("messages") or []):
        if cleared_channel and m.get("channel") == cleared_channel:
            continue
        msg_map[str(m.get("id"))] = m
    for m in (incoming.get("messages") or []):
        k = str(m.get("id"))
        prev = msg_map.get(k)
        if prev:
            rb = list(dict.fromkeys((prev.get("readBy") or []) + (m.get("readBy") or [])))
            merged_m = dict(m)
            merged_m["readBy"] = rb
            msg_map[k] = merged_m
        else:
            msg_map[k] = m
    out["messages"] = sorted(msg_map.values(), key=lambda x: str(x.get("date", "")))[-500:]

    # Preserve per-pharmacy ticked drafts from other counters unless explicitly updated by draft_owner
    sd = dict(existing.get("staffDrafts") or {})
    for k, v in (incoming.get("staffDrafts") or {}).items():
        if k == draft_owner or (k not in sd and v):
            sd[k] = v
    out["staffDrafts"] = sd
    return out


class POHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=BASE_DIR, **kwargs)

    def log_message(self, fmt, *args):
        # Quiet normal polling logs so the console stays clean
        if args and "/api/sync" in str(args[0]):
            return
        super().log_message(fmt, *args)

    def send_json(self, obj, code=200):
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def end_headers(self):
        if not self.path.startswith("/api/"):
            self.send_header("Cache-Control", "no-store")
        # Allow the GitHub Pages copy of the app (opened on this PC) to reach this local server
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Private-Network", "true")
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        clean_path = self.path.split("?", 1)[0]
        if clean_path == "/api/ping":
            with LOCK:
                cur = read_state()
            self.send_json({
                "ok": True,
                "localServer": True,
                "hasBackup": cur is not None,
                "rev": str(cur.get("_rev", "")) if cur else "",
                "dataFile": DATA_FILE,
                "lanUrl": f"http://{get_lan_ip()}:{PORT}"
            })
            return
        if clean_path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
            return
        if clean_path in ("/", "/index.html", "/PO-Maker.html"):
            target = "index.html" if os.path.exists(os.path.join(BASE_DIR, "index.html")) else "PO-Maker.html"
            self.path = "/" + target
            super().do_GET()
            return
        self.send_json({"ok": False, "error": "Not found"}, 404)

    def do_POST(self):
        clean_path = self.path.split("?", 1)[0]
        if clean_path != "/api/sync":
            self.send_json({"ok": False, "error": "Not found"}, 404)
            return
        try:
            ln = int(self.headers.get("Content-Length", "0"))
            req = json.loads(self.rfile.read(ln).decode("utf-8") or "{}")
            action = req.get("action")

            if action == "ping":
                with LOCK:
                    cur = read_state()
                self.send_json({
                    "ok": True,
                    "localServer": True,
                    "hasBackup": cur is not None,
                    "rev": str(cur.get("_rev", "")) if cur else "",
                    "lastUpdated": datetime.datetime.now(datetime.timezone.utc).isoformat()
                })
                return

            if action in ("pull", "restore"):
                with LOCK:
                    cur = read_state()
                if not cur:
                    if action == "restore":
                        self.send_json({"ok": False, "error": "No local server data file yet."})
                    else:
                        self.send_json({"ok": True, "empty": True, "rev": ""})
                    return
                cur_rev = str(cur.get("_rev", ""))
                if action == "pull" and req.get("sinceRev") and req.get("sinceRev") == cur_rev:
                    self.send_json({"ok": True, "unchanged": True, "rev": cur_rev})
                    return
                self.send_json({"ok": True, "rev": cur_rev, "data": cur})
                return

            with LOCK:
                cur = read_state()
                now_rev = str(int(time.time() * 1000))
                now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()

                if action == "submit_request":
                    base = cur or req.get("fallback") or {"settings": {}, "stockists": [], "products": [], "draft": [], "requests": [], "staffDrafts": {}, "messages": [], "history": [], "seq": 0, "uid": 1}
                    base.setdefault("requests", [])
                    r = req.get("request")
                    if r and not any(str(x.get("id")) == str(r.get("id")) for x in base["requests"]):
                        base["requests"].append(r)
                    # Clear sent pharmacy's saved draft on server if provided
                    dk = req.get("draftKey")
                    if dk is not None and "draftRows" in req:
                        base.setdefault("staffDrafts", {})[dk] = req.get("draftRows") or []
                    base["_rev"] = now_rev
                    write_state(base, save_daily=True)
                    self.send_json({"ok": True, "rev": now_rev, "data": base, "syncedAt": now_iso})
                    return

                if action == "save_draft":
                    base = cur or req.get("fallback") or {"settings": {}, "stockists": [], "products": [], "draft": [], "requests": [], "staffDrafts": {}, "messages": [], "history": [], "seq": 0, "uid": 1}
                    dk = req.get("draftKey")
                    if dk:
                        base.setdefault("staffDrafts", {})[dk] = req.get("draftRows") or []
                        base["_rev"] = now_rev
                        write_state(base, save_daily=False)
                    self.send_json({"ok": True, "rev": str(base.get("_rev", now_rev)), "syncedAt": now_iso})
                    return

                if action == "send_message":
                    base = cur or req.get("fallback") or {"settings": {}, "stockists": [], "products": [], "draft": [], "requests": [], "staffDrafts": {}, "messages": [], "history": [], "seq": 0, "uid": 1}
                    base.setdefault("messages", [])
                    for m in (req.get("messages") or []):
                        if not any(str(x.get("id")) == str(m.get("id")) for x in base["messages"]):
                            base["messages"].append(m)
                    base["messages"] = base["messages"][-500:]
                    base["_rev"] = now_rev
                    write_state(base, save_daily=False)
                    self.send_json({"ok": True, "rev": now_rev, "data": base, "syncedAt": now_iso})
                    return

                if action == "mark_read":
                    if not cur:
                        self.send_json({"ok": True, "rev": ""})
                        return
                    changed = False
                    role = req.get("role")
                    ch = req.get("channel")
                    for m in (cur.get("messages") or []):
                        if m.get("channel") == ch and m.get("from") != role:
                            rb = m.setdefault("readBy", [])
                            if role not in rb:
                                rb.append(role)
                                changed = True
                    if changed:
                        cur["_rev"] = now_rev
                        write_state(cur, save_daily=False)
                    self.send_json({"ok": True, "rev": str(cur.get("_rev", now_rev))})
                    return

                if action == "backup":
                    inc = req.get("backup") or {}
                    merged = merge_states(cur, inc, req.get("clearedChannel") or "", req.get("draftOwner") or "")
                    merged["_rev"] = now_rev
                    write_state(merged, save_daily=True)
                    self.send_json({"ok": True, "rev": now_rev, "data": merged, "syncedAt": now_iso})
                    return

                if action == "reset":
                    if cur:
                        ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
                        snap = os.path.join(DAILY_DIR, f"po-maker-before-reset-{ts}.json")
                        with open(snap, "w", encoding="utf-8") as f:
                            json.dump(cur, f, ensure_ascii=False, indent=2)
                    blank = req.get("backup") or {"settings": {}, "stockists": [], "products": [], "draft": [], "requests": [], "staffDrafts": {}, "messages": [], "history": [], "seq": 0, "uid": 1}
                    blank["_rev"] = now_rev
                    write_state(blank, save_daily=False)
                    self.send_json({"ok": True, "rev": now_rev, "data": blank, "syncedAt": now_iso})
                    return

                self.send_json({"ok": False, "error": "Unknown action"}, 400)
        except Exception as exc:
            self.send_json({"ok": False, "error": str(exc)}, 500)


if __name__ == "__main__":
    lan_ip = get_lan_ip()
    try:
        srv = ThreadingHTTPServer(("0.0.0.0", PORT), POHandler)
    except OSError as err:
        print("=" * 62)
        print(f"  Port {PORT} is already in use — server may already be running!")
        print(f"  Opening http://localhost:{PORT} ...")
        print(f"  (Details: {err})")
        print("=" * 62)
        if "--no-browser" not in sys.argv:
            webbrowser.open(f"http://localhost:{PORT}")
        sys.exit(0)
    print("=" * 62)
    print("  Pharmacy PO Maker - Local Live Sync Server Running")
    print("=" * 62)
    print(f"  This PC:            http://localhost:{PORT}")
    print(f"  Other PCs / Phones: http://{lan_ip}:{PORT}")
    print(f"  Local Data File:    {DATA_FILE}")
    print(f"  Daily Backups:      {DAILY_DIR}")
    print("=" * 62)
    print("  Keep this window open while counters use the app.")
    print("  Press Ctrl+C to stop.")
    sys.stdout.flush()
    if "--no-browser" not in sys.argv:
        threading.Timer(0.8, lambda: webbrowser.open(f"http://localhost:{PORT}")).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")
