import datetime
import json
import os
import re
import secrets
import socket
import subprocess
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
TOKENS = {}
FAILS = {}
PUBLIC_URL = ""

# Default passcode hashes (same as the app). Custom passcodes saved in Settings override these.
DEFAULT_PINS = {
    "admin": "h:8655ca6449430ea4ce042b6d692607c954028d6b30cdff5fd9f0c62456e32bf5",
    "manager": "h:bdbe1459df27553707efce285b2ba5591a1e66615984c2cfbad4f27edd36cd4d",
    "pharmacy1": "h:e85ab2b1c2feeba410c31ae6f44dd67101554ec0187372888d5d2f9d2fc6d8e2",
    "pharmacy2": "h:30caa9c845455fab29371edf24d0206aac94ba80601c0572992f22e8acf3922b",
    "pharmacy3": "h:8c113698b11356f5222c37702d094e514800c7a4047247530625664893c887c2",
}


def pin_hash_for(role):
    if role not in DEFAULT_PINS:
        return None
    cur = read_state() or {}
    pins = (cur.get("settings") or {}).get("pins") or {}
    return pins.get(role) or DEFAULT_PINS[role]


def server_info():
    return {
        "dataFile": DATA_FILE,
        "lanUrl": f"http://{get_lan_ip()}:{PORT}",
        "publicUrl": PUBLIC_URL,
    }


def start_tunnel():
    """Start a free Cloudflare Quick Tunnel (if cloudflared.exe is next to this file) so the app works from anywhere."""
    global PUBLIC_URL
    exe = os.path.join(BASE_DIR, "cloudflared.exe")
    if not os.path.exists(exe):
        print("  (Internet access link not enabled: cloudflared.exe not found next to server.py)")
        return None
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = subprocess.Popen(
        [exe, "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{PORT}"],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, creationflags=flags,
    )

    def reader():
        global PUBLIC_URL
        for line in proc.stderr:
            m = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", line)
            if m and not PUBLIC_URL:
                PUBLIC_URL = m.group(0)
                try:
                    with open(os.path.join(DATA_DIR, "public-link.txt"), "w", encoding="utf-8") as f:
                        f.write(PUBLIC_URL + "\n")
                except Exception:
                    pass
                print("")
                print("=" * 62)
                print("  ANYWHERE LINK (open on any phone / PC with internet):")
                print(f"  {PUBLIC_URL}")
                print("  (Also saved in data\\public-link.txt and shown in Admin > Settings)")
                print("=" * 62)
                sys.stdout.flush()

    threading.Thread(target=reader, daemon=True).start()
    return proc


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
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-PO-Token")
        self.send_header("Access-Control-Allow-Private-Network", "true")
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def client_ip(self):
        return (self.headers.get("Cf-Connecting-Ip") or self.headers.get("X-Forwarded-For") or self.client_address[0] or "?").split(",")[0].strip()

    def do_GET(self):
        clean_path = self.path.split("?", 1)[0]
        if clean_path == "/api/ping":
            with LOCK:
                cur = read_state()
            self.send_json({
                "ok": True,
                "localServer": True,
                "authRequired": True,
                "hasBackup": cur is not None,
                "rev": str(cur.get("_rev", "")) if cur else ""
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
        if clean_path not in ("/api/sync", "/api/login"):
            self.send_json({"ok": False, "error": "Not found"}, 404)
            return
        try:
            ln = int(self.headers.get("Content-Length", "0"))
            req = json.loads(self.rfile.read(ln).decode("utf-8") or "{}")
            action = req.get("action")

            if clean_path == "/api/login":
                ip = self.client_ip()
                now = time.time()
                fails = [t for t in FAILS.get(ip, []) if now - t < 300]
                FAILS[ip] = fails
                if len(fails) >= 8:
                    self.send_json({"ok": False, "error": "Too many wrong attempts. Try again in 5 minutes."}, 429)
                    return
                role = str(req.get("role", ""))
                with LOCK:
                    expected = pin_hash_for(role)
                if expected and secrets.compare_digest(str(req.get("hash", "")), expected):
                    token = secrets.token_urlsafe(24)
                    TOKENS[token] = role
                    FAILS[ip] = []
                    self.send_json({"ok": True, "token": token, "role": role, **server_info()})
                else:
                    fails.append(now)
                    FAILS[ip] = fails
                    self.send_json({"ok": False, "error": "Wrong passcode"}, 401)
                return

            token = self.headers.get("X-PO-Token", "")
            token_role = TOKENS.get(token)
            if not token_role:
                self.send_json({"ok": False, "error": "auth"}, 401)
                return

            if action == "info":
                self.send_json({"ok": True, **server_info()})
                return

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
                    if token_role != "admin":
                        self.send_json({"ok": False, "error": "Only Admin can erase data"}, 403)
                        return
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
    tunnel_proc = None
    if "--no-tunnel" not in sys.argv:
        try:
            tunnel_proc = start_tunnel()
        except Exception as exc:
            print(f"  (Could not start internet link: {exc})")
    if "--no-browser" not in sys.argv:
        threading.Timer(0.8, lambda: webbrowser.open(f"http://localhost:{PORT}")).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")
    finally:
        if tunnel_proc:
            try:
                tunnel_proc.terminate()
            except Exception:
                pass
