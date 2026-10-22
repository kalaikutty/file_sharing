#!/usr/bin/env python3
"""
DropBeam v3 — Direct File Transfer
New in v3:
  ✅ Real-time sync     — SSE push (instant updates, no polling)
  ✅ File expiry        — auto-delete after N minutes (configurable per-session)
  ✅ Folder upload      — drag a folder → client zips it → uploads as .zip

Usage:
  python server.py              # LAN mode
  python server.py --hotspot    # PC becomes the WiFi hotspot (no router!)
  python server.py --expiry 30  # Files auto-delete after 30 minutes (default: 0 = off)
"""

import os, io, sys, json, base64, socket, time, mimetypes, platform
import subprocess, argparse, threading, queue
from pathlib import Path
from typing import Optional, List
from flask import Flask, request, jsonify, send_file, abort, Response

# ── Optional qrcode ───────────────────────────────────────────────────────────
try:
    import qrcode; HAS_QR = True
except ImportError:
    HAS_QR = False

# ── Config ────────────────────────────────────────────────────────────────────
PORT           = 5000
HOTSPOT_SSID   = "DropBeam"
HOTSPOT_PASS   = "dropbeam123"
UPLOAD_DIR     = Path(__file__).parent / "uploads"
META_FILE      = UPLOAD_DIR / ".meta.json"   # expiry + original names store
MAX_MB         = 2048
DEFAULT_EXPIRY = 0    # minutes; 0 = disabled. Overridable via --expiry flag
UPLOAD_DIR.mkdir(exist_ok=True)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_MB * 1024 * 1024
OS = platform.system()

# ── Session state (set in main()) ─────────────────────────────────────────────
SERVER_IP   = ""
BASE_URL    = ""
MODE        = "lan"
EXPIRY_MIN  = DEFAULT_EXPIRY   # 0 = off; can be changed via /settings endpoint

# ─────────────────────────────────────────────────────────────────────────────
# METADATA  — tracks original filenames, upload time, expiry per file
# ─────────────────────────────────────────────────────────────────────────────

_meta_lock = threading.Lock()

def _load_meta() -> dict:
    try:
        if META_FILE.exists():
            return json.loads(META_FILE.read_text())
    except Exception:
        pass
    return {}

def _save_meta(meta: dict):
    META_FILE.write_text(json.dumps(meta, indent=2))

def meta_add(safe_name: str, original: str, size: int, expiry_min: int):
    with _meta_lock:
        meta = _load_meta()
        meta[safe_name] = {
            "original":    original,
            "size":        size,
            "uploaded_at": time.time(),
            "expires_at":  (time.time() + expiry_min * 60) if expiry_min > 0 else None,
        }
        _save_meta(meta)

def meta_remove(safe_name: str):
    with _meta_lock:
        meta = _load_meta()
        meta.pop(safe_name, None)
        _save_meta(meta)

def meta_get(safe_name: str) -> dict:
    with _meta_lock:
        return _load_meta().get(safe_name, {})

def meta_all() -> dict:
    with _meta_lock:
        return _load_meta()

# ─────────────────────────────────────────────────────────────────────────────
# SSE  — Server-Sent Events for real-time push to all connected browsers
# ─────────────────────────────────────────────────────────────────────────────

_sse_clients = []  # type: List[queue.Queue]
_sse_lock = threading.Lock()

def sse_subscribe() -> queue.Queue:
    q = queue.Queue(maxsize=20)
    with _sse_lock:
        _sse_clients.append(q)
    return q

def sse_unsubscribe(q: queue.Queue):
    with _sse_lock:
        try:
            _sse_clients.remove(q)
        except ValueError:
            pass

def sse_broadcast(event_type: str, payload: Optional[dict] = None):
    """Push a JSON event to every connected browser tab."""
    msg = json.dumps({"type": event_type, **(payload or {})})
    data = f"data: {msg}\n\n"
    dead = []
    with _sse_lock:
        for q in _sse_clients:
            try:
                q.put_nowait(data)
            except queue.Full:
                dead.append(q)
    for q in dead:
        sse_unsubscribe(q)

@app.route("/events")
def sse_stream():
    """SSE endpoint — each browser tab connects here and receives live events."""
    q = sse_subscribe()
    def stream():
        # send a heartbeat comment every 20s to keep connections alive
        try:
            while True:
                try:
                    data = q.get(timeout=20)
                    yield data
                except queue.Empty:
                    yield ": heartbeat\n\n"
        except GeneratorExit:
            pass
        finally:
            sse_unsubscribe(q)

    return Response(
        stream(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control":    "no-cache",
            "X-Accel-Buffering": "no",   # disable nginx buffering if behind proxy
            "Connection":       "keep-alive",
        }
    )

# ─────────────────────────────────────────────────────────────────────────────
# EXPIRY WATCHDOG  — background thread that deletes expired files
# ─────────────────────────────────────────────────────────────────────────────

def _expiry_watchdog():
    """Runs every 15 seconds, deletes files whose expires_at has passed."""
    while True:
        time.sleep(15)
        now = time.time()
        try:
            meta = meta_all()
            for safe_name, info in list(meta.items()):
                exp = info.get("expires_at")
                if exp and now >= exp:
                    path = UPLOAD_DIR / safe_name
                    if path.exists():
                        path.unlink()
                        print(f"  ⏳  Expired & deleted: {info.get('original', safe_name)}")
                    meta_remove(safe_name)
                    sse_broadcast("expired", {"filename": safe_name,
                                              "name": info.get("original", safe_name)})
        except Exception as e:
            print(f"  ⚠️  Expiry watchdog error: {e}")

threading.Thread(target=_expiry_watchdog, daemon=True).start()

# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def get_lan_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"

def make_safe_filename(original: str) -> str:
    safe = "".join(c if c.isalnum() or c in "._- " else "_" for c in original)
    return f"{int(time.time() * 1000)}-{safe}"

def make_qr_b64(url: str) -> str:
    if not HAS_QR:
        return ""
    qr = qrcode.QRCode(version=1, box_size=8, border=2,
                        error_correction=qrcode.constants.ERROR_CORRECT_M)
    qr.add_data(url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="#0a0a14", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()

def build_file_record(p: Path) -> Optional[dict]:
    if not p.is_file() or p.name.startswith("."):
        return None
    info = meta_get(p.name)
    original = info.get("original") or p.name.split("-", 1)[-1]
    return {
        "id":          p.name,
        "filename":    p.name,
        "name":        original,
        "size":        p.stat().st_size,
        "time":        p.stat().st_mtime,
        "expires_at":  info.get("expires_at"),   # Unix timestamp or null
        "uploaded_at": info.get("uploaded_at"),
    }

# ─────────────────────────────────────────────────────────────────────────────
# FLASK ROUTES
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/qr")
def get_qr():
    return jsonify({
        "qr":   make_qr_b64(BASE_URL),
        "url":  BASE_URL,
        "mode": MODE,
        "ssid": HOTSPOT_SSID if MODE == "hotspot" else "",
        "pass": HOTSPOT_PASS if MODE == "hotspot" else "",
    })

@app.route("/settings", methods=["GET", "POST"])
def settings():
    global EXPIRY_MIN
    if request.method == "POST":
        data = request.get_json(force=True)
        if "expiry_min" in data:
            EXPIRY_MIN = max(0, int(data["expiry_min"]))
    return jsonify({"expiry_min": EXPIRY_MIN})

@app.route("/upload", methods=["POST"])
def upload():
    files = request.files.getlist("files")
    if not files:
        return jsonify({"error": "No files"}), 400

    uploaded = []
    for f in files:
        if not f.filename:
            continue
        safe = make_safe_filename(f.filename)
        dest = UPLOAD_DIR / safe
        f.save(dest)
        size = dest.stat().st_size
        meta_add(safe, f.filename, size, EXPIRY_MIN)
        rec = build_file_record(dest)
        uploaded.append(rec)

    # Push real-time update to ALL connected browsers
    sse_broadcast("uploaded", {"files": uploaded})
    return jsonify({"success": True, "files": uploaded})

@app.route("/files")
def list_files():
    result = []
    for p in UPLOAD_DIR.iterdir():
        rec = build_file_record(p)
        if rec:
            result.append(rec)
    result.sort(key=lambda x: x["time"], reverse=True)
    return jsonify(result)

@app.route("/download/<filename>")
def download(filename):
    path = UPLOAD_DIR / filename
    if not path.exists() or not path.is_file():
        abort(404)
    info = meta_get(filename)
    display = info.get("original") or filename.split("-", 1)[-1]
    mime, _ = mimetypes.guess_type(display)
    return send_file(path, as_attachment=True,
                     download_name=display,
                     mimetype=mime or "application/octet-stream")

@app.route("/files/<filename>", methods=["DELETE"])
def delete_file(filename):
    path = UPLOAD_DIR / filename
    if path.exists():
        path.unlink()
    meta_remove(filename)
    sse_broadcast("deleted", {"filename": filename})
    return jsonify({"success": True})

@app.route("/")
def index():
    return HTML

# ─────────────────────────────────────────────────────────────────────────────
# HOTSPOT MANAGER
# ─────────────────────────────────────────────────────────────────────────────

class HotspotManager:
    def __init__(self):
        self.started = False

    def start(self) -> Optional[str]:
        print(f"\n  📡  Starting hotspot  SSID='{HOTSPOT_SSID}'  Pass='{HOTSPOT_PASS}'")
        try:
            if OS == "Windows": ip = self._windows()
            elif OS == "Darwin": ip = self._mac()
            else: ip = self._linux()
            if ip:
                self.started = True
                print(f"  ✅  Hotspot up — connect phone to WiFi '{HOTSPOT_SSID}'")
                return ip
            print("  ⚠️  Hotspot failed — falling back to LAN mode.")
            return None
        except Exception as e:
            print(f"  ⚠️  Hotspot error: {e} — using LAN.")
            return None

    def stop(self):
        if not self.started: return
        try:
            if OS == "Windows":
                subprocess.run(["netsh","wlan","stop","hostednetwork"], capture_output=True)
            elif OS == "Darwin":
                pass  # user-managed
            else:
                subprocess.run(["nmcli","connection","down", HOTSPOT_SSID], capture_output=True)
        except Exception: pass

    def _windows(self):
        for cmd in [
            ["netsh","wlan","set","hostednetwork","mode=allow",f"ssid={HOTSPOT_SSID}",f"key={HOTSPOT_PASS}"],
            ["netsh","wlan","start","hostednetwork"],
        ]:
            subprocess.run(cmd, capture_output=True, text=True)
        time.sleep(2)
        return self._find_ip("192.168.137.") or "192.168.137.1"

    def _mac(self):
        airport = "/System/Library/PrivateFrameworks/Apple80211.framework/Versions/Current/Resources/airport"
        try:
            subprocess.Popen([airport,"--net",f"--ssid={HOTSPOT_SSID}",f"--password={HOTSPOT_PASS}","--channel=6"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(3)
            ip = self._find_ip("192.168.")
            if ip: return ip
        except FileNotFoundError: pass
        print("\n  ℹ️  macOS: Open System Settings → Sharing → Internet Sharing")
        print(f"     SSID: {HOTSPOT_SSID}  Password: {HOTSPOT_PASS}")
        input("  Press Enter when hotspot is on…\n")
        return self._find_ip("192.168.")

    def _linux(self):
        subprocess.run(["nmcli","connection","delete",HOTSPOT_SSID], capture_output=True)
        time.sleep(1)
        r = subprocess.run(["nmcli","device","wifi","hotspot","ssid",HOTSPOT_SSID,
                             "password",HOTSPOT_PASS,"band","bg"],
                           capture_output=True, text=True)
        if r.returncode != 0:
            print(f"    nmcli: {r.stderr.strip()}")
            return None
        time.sleep(3)
        return self._find_ip("10.42.0.") or "10.42.0.1"

    def _find_ip(self, prefix):
        try:
            all_ips = socket.getaddrinfo(socket.gethostname(), None)
            for info in all_ips:
                ip = info[4][0]
                if ip.startswith(prefix): return ip
        except Exception: pass
        try:
            tool = "ipconfig" if OS == "Windows" else "ip addr"
            out  = subprocess.check_output(tool.split(), text=True, stderr=subprocess.DEVNULL)
            import re
            for ip in re.findall(r'(\d+\.\d+\.\d+\.\d+)', out):
                if ip.startswith(prefix): return ip
        except Exception: pass
        return None

HOTSPOT_MGR = HotspotManager()

# ─────────────────────────────────────────────────────────────────────────────
# HTML  ──  all three new features wired in
# ─────────────────────────────────────────────────────────────────────────────

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1.0"/>
<title>DropBeam</title>
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;600;700&family=Syne:wght@400;700;800&display=swap" rel="stylesheet"/>
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#03080f;--surface:#080f1a;--surface2:#0d1826;
  --border:rgba(0,200,130,0.12);--accent:#00c882;--accent2:#00e5ff;
  --glow:rgba(0,200,130,0.3);--text:#d4f5e9;--muted:#3d7a60;
  --danger:#ff5a5a;--warn:#f5a623;
  --mono:'JetBrains Mono',monospace;--display:'Syne',sans-serif;
}
html,body{min-height:100vh;background:var(--bg);color:var(--text);font-family:var(--display);overflow-x:hidden}
body::before{content:'';position:fixed;inset:0;pointer-events:none;z-index:0;
  background:repeating-linear-gradient(0deg,transparent,transparent 2px,rgba(0,200,130,0.013) 2px,rgba(0,200,130,0.013) 4px)}
body::after{content:'';position:fixed;bottom:-20%;right:-10%;width:55vw;height:55vw;border-radius:50%;
  background:radial-gradient(circle,rgba(0,200,130,0.07) 0%,transparent 70%);
  pointer-events:none;z-index:0;animation:breathe 9s ease-in-out infinite alternate}
@keyframes breathe{from{opacity:.5}to{opacity:1;transform:scale(1.05)}}
.app{position:relative;z-index:1;max-width:1060px;margin:0 auto;padding:0 16px 70px}

/* ── Header ── */
header{display:flex;align-items:center;justify-content:space-between;
  padding:22px 0 18px;border-bottom:1px solid var(--border);flex-wrap:wrap;gap:12px}
.logo{display:flex;align-items:center;gap:14px}
.logo-icon{width:44px;height:44px;border-radius:12px;flex-shrink:0;
  border:1.5px solid var(--accent);display:flex;align-items:center;justify-content:center;font-size:22px;
  box-shadow:0 0 28px var(--glow),inset 0 0 12px rgba(0,200,130,0.08)}
.logo-name{font-size:1.5rem;font-weight:800;letter-spacing:-1px;color:#fff}
.logo-name span{color:var(--accent)}
.logo-tag{font-family:var(--mono);font-size:0.55rem;color:var(--muted);margin-top:3px;letter-spacing:2px;text-transform:uppercase}
.hdr-right{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.mode-badge{font-family:var(--mono);font-size:0.63rem;letter-spacing:2px;text-transform:uppercase;
  padding:5px 11px;border-radius:999px;border:1px solid}
.mode-badge.hotspot{background:rgba(0,200,130,0.1);border-color:var(--accent);color:var(--accent)}
.mode-badge.lan{background:rgba(0,229,255,0.08);border-color:var(--accent2);color:var(--accent2)}
.live-pill{display:flex;align-items:center;gap:6px;font-family:var(--mono);font-size:0.63rem;
  color:var(--accent);border:1px solid var(--border);border-radius:999px;padding:5px 11px;
  background:rgba(0,200,130,0.04)}
.live-dot{width:6px;height:6px;border-radius:50%;background:var(--accent);flex-shrink:0;animation:blink 1.8s infinite}
@keyframes blink{0%,100%{opacity:1}50%{opacity:.2}}
.status-pill{display:flex;align-items:center;gap:8px;border:1px solid var(--border);border-radius:999px;
  padding:6px 13px;background:rgba(0,200,130,0.04);font-family:var(--mono);font-size:0.68rem;
  color:var(--accent);word-break:break-all}
.dot{width:6px;height:6px;border-radius:50%;background:var(--accent);flex-shrink:0;animation:blink 1.8s infinite}

/* ── Expiry bar ── */
.expiry-bar{display:flex;align-items:center;gap:14px;flex-wrap:wrap;
  margin-top:18px;padding:14px 18px;
  background:rgba(0,200,130,0.04);border:1px solid var(--border);border-radius:12px}
.expiry-label{font-family:var(--mono);font-size:0.7rem;color:var(--muted);white-space:nowrap}
.expiry-label strong{color:var(--text)}
.expiry-slider{flex:1;min-width:120px;max-width:260px;accent-color:var(--accent)}
.expiry-val{font-family:var(--mono);font-size:0.78rem;color:var(--accent);min-width:80px;font-weight:600}
.expiry-toggle{font-family:var(--mono);font-size:0.68rem;color:var(--muted);
  background:transparent;border:1px solid var(--border);border-radius:6px;
  padding:5px 12px;cursor:pointer;transition:all 0.2s}
.expiry-toggle:hover{border-color:var(--accent);color:var(--accent)}
.expiry-toggle.active{border-color:var(--accent);color:var(--accent);background:rgba(0,200,130,0.1)}

/* ── Hotspot banner ── */
.hotspot-banner{display:none;margin-top:14px;background:rgba(0,200,130,0.05);
  border:1px solid rgba(0,200,130,0.2);border-radius:14px;padding:14px 18px;
  align-items:center;gap:16px;flex-wrap:wrap}
.hotspot-banner.show{display:flex}
.hb-icon{font-size:1.6rem;flex-shrink:0}
.hb-title{font-size:.95rem;font-weight:700;color:#fff;margin-bottom:4px}
.hb-creds{display:flex;gap:10px;flex-wrap:wrap;margin-top:5px}
.cred{background:var(--surface2);border:1px solid var(--border);border-radius:8px;padding:6px 13px;font-family:var(--mono);font-size:.75rem}
.cred-label{color:var(--muted);font-size:.6rem;letter-spacing:1px;text-transform:uppercase;margin-bottom:2px}
.cred-val{color:var(--accent);font-weight:700;font-size:.88rem}

/* ── Grid ── */
.grid{display:grid;grid-template-columns:1fr 305px;gap:18px;margin-top:18px}
@media(max-width:720px){.grid{grid-template-columns:1fr}}

/* ── Panel ── */
.panel{background:var(--surface);border:1px solid var(--border);border-radius:16px;overflow:hidden}
.panel-header{display:flex;align-items:center;justify-content:space-between;
  padding:13px 18px;border-bottom:1px solid var(--border);background:rgba(0,200,130,0.025)}
.panel-title{font-family:var(--mono);font-size:.63rem;letter-spacing:3px;text-transform:uppercase;color:var(--muted)}
.count-badge{font-family:var(--mono);font-size:.62rem;border:1px solid var(--border);
  border-radius:6px;padding:3px 9px;color:var(--accent)}
.panel-body{padding:18px}

/* ── Drop zone ── */
.drop-zone{position:relative;border:1.5px dashed rgba(0,200,130,0.25);border-radius:14px;
  text-align:center;cursor:pointer;transition:all 0.25s;background:rgba(0,200,130,0.02);overflow:hidden}
.drop-zone:hover,.drop-zone.drag-over{border-color:var(--accent);
  box-shadow:0 0 40px rgba(0,200,130,0.1),inset 0 0 30px rgba(0,200,130,0.04)}
.drop-zone.drag-folder{border-color:var(--warn);
  box-shadow:0 0 40px rgba(245,166,35,0.15),inset 0 0 30px rgba(245,166,35,0.04)}
.drop-label{display:block;padding:46px 24px 34px;cursor:pointer}
#fileInput{position:absolute;width:1px;height:1px;opacity:0;pointer-events:none}
.drop-icon{font-size:2.6rem;display:block;margin-bottom:12px;transition:transform .3s}
.drop-zone.drag-over .drop-icon,.drop-zone.drag-folder .drop-icon{transform:scale(1.15)}
.drop-title{font-size:1.15rem;font-weight:800;color:#fff;margin-bottom:6px}
.drop-sub{font-family:var(--mono);font-size:.68rem;color:var(--muted);margin-bottom:18px}
.drop-sub span{color:var(--warn)}
.browse-btn{display:inline-block;color:var(--accent);font-family:var(--mono);font-size:.76rem;
  font-weight:600;letter-spacing:1px;padding:10px 26px;border-radius:8px;
  border:1.5px solid var(--accent);box-shadow:0 0 16px rgba(0,200,130,0.18);
  transition:all 0.2s;user-select:none}
.drop-zone:hover .browse-btn{background:var(--accent);color:#000;box-shadow:0 0 28px var(--glow)}

/* ── Progress ── */
.progress-wrap{margin-top:14px;display:none}
.progress-head{display:flex;justify-content:space-between;margin-bottom:8px;
  font-family:var(--mono);font-size:.64rem;color:var(--muted)}
.progress-track{background:var(--surface2);border-radius:999px;height:5px;overflow:hidden}
.progress-fill{height:100%;border-radius:999px;
  background:linear-gradient(90deg,var(--accent),var(--accent2));width:0%;transition:width .15s}

/* ── File list ── */
.file-list{margin-top:14px;display:flex;flex-direction:column;gap:8px}
.file-item{display:flex;align-items:center;gap:12px;background:var(--surface2);
  border:1px solid var(--border);border-radius:10px;padding:11px 14px;
  transition:all .2s;animation:appear .3s ease;position:relative;overflow:hidden}
.file-item::after{content:'';position:absolute;bottom:0;left:0;height:2px;
  background:linear-gradient(90deg,var(--accent),var(--accent2));
  width:var(--expiry-pct,0%);transition:width 1s linear;border-radius:0 0 0 10px}
.file-item.expiring-soon{border-color:rgba(245,166,35,0.4)}
.file-item.expiring-soon::after{background:linear-gradient(90deg,var(--warn),var(--accent))}
@keyframes appear{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:translateY(0)}}
.file-item:hover{border-color:rgba(0,200,130,0.3)}
.file-thumb{width:38px;height:38px;border-radius:8px;flex-shrink:0;display:flex;
  align-items:center;justify-content:center;font-size:1.1rem;
  background:rgba(0,200,130,0.08);border:1px solid var(--border)}
.file-meta{flex:1;min-width:0}
.file-name{font-size:.86rem;font-weight:600;color:#fff;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.file-size{font-family:var(--mono);font-size:.62rem;color:var(--muted);margin-top:2px}
.expiry-tag{font-family:var(--mono);font-size:.58rem;color:var(--warn);
  margin-top:2px;display:none}
.file-item[data-expires] .expiry-tag{display:block}
.file-btns{display:flex;gap:6px;flex-shrink:0}
.icon-btn{min-width:44px;min-height:44px;border-radius:8px;display:flex;
  align-items:center;justify-content:center;font-size:1rem;
  border:1px solid var(--border);background:transparent;cursor:pointer;
  color:var(--text);transition:all .2s}
.icon-btn:hover,.icon-btn:active{border-color:var(--accent);background:rgba(0,200,130,0.12)}
.icon-btn.del:hover,.icon-btn.del:active{border-color:var(--danger);background:rgba(255,90,90,0.1)}
.empty{text-align:center;padding:38px 20px;font-family:var(--mono);font-size:.7rem;color:var(--muted)}
.empty-ico{font-size:2.2rem;display:block;margin-bottom:10px;opacity:.3}

/* ── Right col ── */
.right-col{display:flex;flex-direction:column;gap:14px}
.qr-panel{background:var(--surface);border:1px solid var(--border);border-radius:16px;overflow:hidden}
.qr-inner{padding:20px;text-align:center}
.qr-frame{display:inline-block;padding:13px;border-radius:12px;background:#fff;
  box-shadow:0 0 48px rgba(0,200,130,0.22)}
.qr-frame img{width:165px;height:165px;display:block}
.qr-hint{font-family:var(--mono);font-size:.63rem;color:var(--muted);margin-top:11px}
.qr-url{display:flex;align-items:center;gap:8px;background:var(--surface2);
  border:1px solid var(--border);border-radius:8px;padding:10px 13px;margin-top:11px;
  cursor:pointer;font-family:var(--mono);font-size:.68rem;color:var(--accent);
  text-align:left;word-break:break-all;transition:background .2s}
.qr-url:hover{background:rgba(0,200,130,0.08)}
.stats-row{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.stat-card{background:var(--surface);border:1px solid var(--border);
  border-radius:12px;padding:14px;text-align:center}
.stat-val{font-size:1.4rem;font-weight:800;color:var(--accent)}
.stat-lbl{font-family:var(--mono);font-size:.57rem;color:var(--muted);
  margin-top:4px;text-transform:uppercase;letter-spacing:2px}
.steps-panel{background:var(--surface);border:1px solid var(--border);border-radius:14px;padding:16px}
.steps-title{font-family:var(--mono);font-size:.6rem;letter-spacing:3px;
  text-transform:uppercase;color:var(--muted);margin-bottom:13px}
.step{display:flex;gap:10px;align-items:flex-start;margin-bottom:9px}
.step:last-child{margin-bottom:0}
.step-n{width:22px;height:22px;flex-shrink:0;border-radius:5px;
  background:rgba(0,200,130,0.15);border:1px solid rgba(0,200,130,0.3);
  display:flex;align-items:center;justify-content:center;
  font-family:var(--mono);font-size:.6rem;color:var(--accent);font-weight:700}
.step-t{font-size:.77rem;color:var(--muted);line-height:1.5;padding-top:2px}
.step-t strong{color:var(--accent)}

/* ── Toast ── */
#toast{position:fixed;bottom:22px;right:22px;left:22px;max-width:320px;margin:auto;
  background:var(--surface2);border:1px solid var(--border);border-radius:10px;
  padding:13px 18px;font-family:var(--mono);font-size:.74rem;
  transform:translateY(90px);opacity:0;
  transition:all .35s cubic-bezier(.34,1.56,.64,1);
  z-index:9999;box-shadow:0 8px 40px rgba(0,0,0,0.5);text-align:center}
#toast.show{transform:translateY(0);opacity:1}
#toast.ok{border-color:var(--accent);color:var(--accent)}
#toast.err{border-color:var(--danger);color:var(--danger)}
#toast.warn{border-color:var(--warn);color:var(--warn)}

/* ── Zip progress overlay ── */
#zipOverlay{position:fixed;inset:0;background:rgba(3,8,15,.85);z-index:9000;
  display:none;flex-direction:column;align-items:center;justify-content:center;gap:18px}
#zipOverlay.show{display:flex}
.zip-box{background:var(--surface);border:1px solid var(--border);border-radius:16px;
  padding:32px 40px;text-align:center;max-width:380px;width:90%}
.zip-title{font-size:1.1rem;font-weight:800;color:#fff;margin-bottom:8px}
.zip-sub{font-family:var(--mono);font-size:.72rem;color:var(--muted);margin-bottom:20px}
.zip-track{background:var(--surface2);border-radius:999px;height:6px;overflow:hidden}
.zip-fill{height:100%;border-radius:999px;
  background:linear-gradient(90deg,var(--warn),var(--accent));width:0%;transition:width .2s}
.zip-pct{font-family:var(--mono);font-size:.78rem;color:var(--warn);margin-top:10px}
</style>
</head>
<body>

<!-- JSZip — for client-side folder compression -->
<script src="https://cdnjs.cloudflare.com/ajax/libs/jszip/3.10.1/jszip.min.js"></script>

<div class="app">
  <header>
    <div class="logo">
      <div class="logo-icon">⚡</div>
      <div>
        <div class="logo-name">Drop<span>Beam</span></div>
        <div class="logo-tag">v3 · Real-time · Direct Transfer</div>
      </div>
    </div>
    <div class="hdr-right">
      <div class="live-pill"><div class="live-dot"></div><span id="liveStatus">connecting…</span></div>
      <div class="mode-badge" id="modeBadge">…</div>
      <div class="status-pill"><div class="dot"></div><span id="headerUrl">…</span></div>
    </div>
  </header>

  <!-- Expiry control bar -->
  <div class="expiry-bar">
    <span class="expiry-label"><strong>File expiry:</strong></span>
    <input type="range" class="expiry-slider" id="expirySlider" min="1" max="120" value="30" step="1"/>
    <span class="expiry-val" id="expiryVal">30 min</span>
    <button class="expiry-toggle" id="expiryToggle" onclick="toggleExpiry()">Enable</button>
    <span class="expiry-label" style="font-size:.62rem">New files auto-delete after the timer</span>
  </div>

  <!-- Hotspot creds banner -->
  <div class="hotspot-banner" id="hotspotBanner">
    <div class="hb-icon">📡</div>
    <div>
      <div class="hb-title">PC Hotspot is ON — connect phone to this WiFi:</div>
      <div class="hb-creds">
        <div class="cred"><div class="cred-label">WiFi Name</div><div class="cred-val" id="bannerSsid">—</div></div>
        <div class="cred"><div class="cred-label">Password</div><div class="cred-val" id="bannerPass">—</div></div>
      </div>
    </div>
  </div>

  <div class="grid">
    <div>
      <div class="panel">
        <div class="panel-header">
          <span class="panel-title">Upload Files</span>
          <span class="count-badge" id="countBadge">0 FILES</span>
        </div>
        <div class="panel-body">
          <!-- Drop zone: label trick for mobile compatibility -->
          <div class="drop-zone" id="dropZone">
            <label class="drop-label" for="fileInput">
              <span class="drop-icon" id="dropIcon">📂</span>
              <div class="drop-title" id="dropTitle">Drop files or folders here</div>
              <div class="drop-sub">Files · <span>Folders (auto-zipped)</span> · Up to 2 GB</div>
              <span class="browse-btn">[ BROWSE FILES ]</span>
            </label>
            <input type="file" id="fileInput" multiple/>
          </div>

          <!-- Upload progress -->
          <div class="progress-wrap" id="progressWrap">
            <div class="progress-head">
              <span id="progLabel">Uploading…</span>
              <span id="progPct">0%</span>
            </div>
            <div class="progress-track"><div class="progress-fill" id="progBar"></div></div>
          </div>

          <div class="file-list" id="fileList"></div>
        </div>
      </div>
    </div>

    <div class="right-col">
      <div class="qr-panel">
        <div class="panel-header"><span class="panel-title">Scan to Connect</span></div>
        <div class="qr-inner">
          <div class="qr-frame"><img id="qrImg" src="" alt="QR" width="165" height="165"/></div>
          <div class="qr-hint" id="qrHint">Point phone camera at QR code</div>
          <div class="qr-url" onclick="copyUrl()">
            <span id="qrUrlTxt" style="flex:1">—</span><span>📋</span>
          </div>
        </div>
      </div>

      <div class="stats-row">
        <div class="stat-card"><div class="stat-val" id="sFiles">0</div><div class="stat-lbl">Files</div></div>
        <div class="stat-card"><div class="stat-val" id="sSize">0 B</div><div class="stat-lbl">Total</div></div>
      </div>

      <div class="steps-panel">
        <div class="steps-title">What's new in v3</div>
        <div class="step"><div class="step-n">⚡</div><div class="step-t"><strong>Real-time</strong> — file list syncs instantly via SSE push</div></div>
        <div class="step"><div class="step-n">⏳</div><div class="step-t"><strong>Expiry</strong> — set a timer, files auto-delete</div></div>
        <div class="step"><div class="step-n">📁</div><div class="step-t"><strong>Folders</strong> — drop a whole folder, gets zipped & sent</div></div>
      </div>
    </div>
  </div>
</div>

<!-- Zip compression overlay (shown while zipping large folders) -->
<div id="zipOverlay">
  <div class="zip-box">
    <div class="zip-title">📁 Zipping folder…</div>
    <div class="zip-sub" id="zipSub">Reading files…</div>
    <div class="zip-track"><div class="zip-fill" id="zipBar"></div></div>
    <div class="zip-pct" id="zipPct">0%</div>
  </div>
</div>

<div id="toast"></div>

<script>
/* ═══════════════════════════════════════════════════════════════
   STATE
═══════════════════════════════════════════════════════════════ */
let serverUrl   = '';
let expiryOn    = false;
let expiryMin   = 30;
let fileCache   = [];   // latest known file list
let sseConn     = null;

/* ═══════════════════════════════════════════════════════════════
   INIT
═══════════════════════════════════════════════════════════════ */
async function init() {
  // Fetch QR + mode info
  try {
    const data = await fetch('/qr').then(r => r.json());
    serverUrl = data.url;
    if (data.qr) document.getElementById('qrImg').src = data.qr;
    document.getElementById('qrUrlTxt').textContent  = data.url;
    document.getElementById('headerUrl').textContent = data.url.replace('http://','');

    const badge = document.getElementById('modeBadge');
    if (data.mode === 'hotspot') {
      badge.textContent = '📡 HOTSPOT';
      badge.className   = 'mode-badge hotspot';
      const b = document.getElementById('hotspotBanner');
      b.classList.add('show');
      document.getElementById('bannerSsid').textContent = data.ssid;
      document.getElementById('bannerPass').textContent = data.pass;
      document.getElementById('qrHint').textContent = 'Join WiFi above first, then scan';
    } else {
      badge.textContent = '🌐 LAN';
      badge.className   = 'mode-badge lan';
    }
  } catch(e) { console.error('init qr', e); }

  // Fetch current expiry setting from server
  try {
    const s = await fetch('/settings').then(r => r.json());
    expiryMin = s.expiry_min || 30;
    expiryOn  = s.expiry_min > 0;
    document.getElementById('expirySlider').value = expiryMin;
    updateExpiryUI();
  } catch(e) {}

  // Load initial file list
  await loadFiles();

  // Connect SSE for real-time updates
  connectSSE();

  // Expiry slider
  document.getElementById('expirySlider').addEventListener('input', function() {
    expiryMin = parseInt(this.value);
    document.getElementById('expiryVal').textContent = fmtDur(expiryMin);
    if (expiryOn) pushExpirySetting();
  });

  // Countdown ticks
  setInterval(tickCountdowns, 1000);
}

/* ═══════════════════════════════════════════════════════════════
   SSE — REAL-TIME SYNC
   Server sends events when: file uploaded, deleted, or expired
═══════════════════════════════════════════════════════════════ */
function connectSSE() {
  if (sseConn) sseConn.close();
  sseConn = new EventSource('/events');

  sseConn.onopen = () => {
    document.getElementById('liveStatus').textContent = 'live';
  };

  sseConn.onmessage = (e) => {
    const msg = JSON.parse(e.data);

    if (msg.type === 'uploaded') {
      // Add new files to the top of the list (no full reload needed)
      msg.files.forEach(f => {
        fileCache = fileCache.filter(x => x.id !== f.id);
        fileCache.unshift(f);
      });
      renderFiles();
      toast('📥 New file received!', 'ok');
    }
    else if (msg.type === 'deleted') {
      fileCache = fileCache.filter(f => f.filename !== msg.filename);
      renderFiles();
    }
    else if (msg.type === 'expired') {
      fileCache = fileCache.filter(f => f.filename !== msg.filename);
      renderFiles();
      toast(`⏳ "${msg.name}" expired & deleted`, 'warn');
    }
  };

  sseConn.onerror = () => {
    document.getElementById('liveStatus').textContent = 'reconnecting…';
    sseConn.close();
    setTimeout(connectSSE, 3000);
  };
}

/* ═══════════════════════════════════════════════════════════════
   EXPIRY CONTROLS
═══════════════════════════════════════════════════════════════ */
function toggleExpiry() {
  expiryOn = !expiryOn;
  updateExpiryUI();
  pushExpirySetting();
}

function updateExpiryUI() {
  const btn = document.getElementById('expiryToggle');
  const slider = document.getElementById('expirySlider');
  if (expiryOn) {
    btn.textContent = 'Enabled ✓';
    btn.classList.add('active');
    slider.disabled = false;
    document.getElementById('expiryVal').textContent = fmtDur(expiryMin);
  } else {
    btn.textContent = 'Enable';
    btn.classList.remove('active');
    slider.disabled = false;
    document.getElementById('expiryVal').textContent = 'Off';
  }
}

function pushExpirySetting() {
  fetch('/settings', {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({expiry_min: expiryOn ? expiryMin : 0})
  }).catch(console.error);
}

function fmtDur(m) {
  if (m < 60) return m + ' min';
  return (m / 60).toFixed(1) + ' hr';
}

/* ═══════════════════════════════════════════════════════════════
   COUNTDOWN TICKS — update expiry timers every second
═══════════════════════════════════════════════════════════════ */
function tickCountdowns() {
  const now = Date.now() / 1000;
  document.querySelectorAll('.file-item[data-expires]').forEach(el => {
    const exp    = parseFloat(el.dataset.expires);
    const upl    = parseFloat(el.dataset.uploaded || exp - 3600);
    const total  = exp - upl;
    const remain = exp - now;
    if (remain <= 0) return;  // expiry watcher will remove it

    // Expiry progress bar width (shrinks as time runs out)
    const pct = Math.max(0, Math.min(100, (remain / total) * 100));
    el.style.setProperty('--expiry-pct', pct.toFixed(1) + '%');

    // Countdown text
    const tag = el.querySelector('.expiry-tag');
    if (tag) tag.textContent = '⏳ expires in ' + fmtRemain(remain);

    // Warn color when < 20% time left
    if (pct < 20) el.classList.add('expiring-soon');
    else el.classList.remove('expiring-soon');
  });
}

function fmtRemain(s) {
  if (s < 60) return Math.ceil(s) + 's';
  if (s < 3600) return Math.ceil(s / 60) + 'm';
  return (s / 3600).toFixed(1) + 'h';
}

/* ═══════════════════════════════════════════════════════════════
   DRAG & DROP — with folder detection
═══════════════════════════════════════════════════════════════ */
const dz    = document.getElementById('dropZone');
const dzIcon  = document.getElementById('dropIcon');
const dzTitle = document.getElementById('dropTitle');

dz.addEventListener('dragenter', e => {
  e.preventDefault(); e.stopPropagation();
  const isFolder = isContainingFolder(e.dataTransfer);
  dz.classList.add(isFolder ? 'drag-folder' : 'drag-over');
  dzIcon.textContent  = isFolder ? '📁' : '📂';
  dzTitle.textContent = isFolder ? 'Drop folder to zip & upload' : 'Drop files here';
});
dz.addEventListener('dragover', e => { e.preventDefault(); e.stopPropagation(); });
['dragleave','dragend'].forEach(ev =>
  dz.addEventListener(ev, e => {
    e.preventDefault(); e.stopPropagation();
    dz.classList.remove('drag-over','drag-folder');
    dzIcon.textContent  = '📂';
    dzTitle.textContent = 'Drop files or folders here';
  })
);

dz.addEventListener('drop', async e => {
  e.preventDefault(); e.stopPropagation();
  dz.classList.remove('drag-over','drag-folder');
  dzIcon.textContent  = '📂';
  dzTitle.textContent = 'Drop files or folders here';

  const items = e.dataTransfer.items;
  if (!items || !items.length) return;

  // Check if any item is a directory
  const entries = [];
  for (let i = 0; i < items.length; i++) {
    const entry = items[i].webkitGetAsEntry && items[i].webkitGetAsEntry();
    if (entry) entries.push(entry);
  }

  const hasFolder = entries.some(en => en.isDirectory);

  if (hasFolder) {
    // ── Folder mode: zip everything client-side ──────────────────────────
    await handleFolderDrop(entries);
  } else {
    // ── Normal file drop ─────────────────────────────────────────────────
    if (e.dataTransfer.files.length) uploadFiles(e.dataTransfer.files);
  }
});

function isContainingFolder(dt) {
  if (!dt || !dt.items) return false;
  for (let i = 0; i < dt.items.length; i++) {
    const e = dt.items[i].webkitGetAsEntry && dt.items[i].webkitGetAsEntry();
    if (e && e.isDirectory) return true;
  }
  return false;
}

/* ═══════════════════════════════════════════════════════════════
   FOLDER UPLOAD — client-side zip using JSZip
═══════════════════════════════════════════════════════════════ */
async function handleFolderDrop(entries) {
  // Show zip overlay
  const overlay = document.getElementById('zipOverlay');
  const zipBar  = document.getElementById('zipBar');
  const zipSub  = document.getElementById('zipSub');
  const zipPct  = document.getElementById('zipPct');
  overlay.classList.add('show');

  try {
    const zip = new JSZip();
    let totalFiles = 0;
    let processed  = 0;

    // Collect all files recursively
    const allFiles = [];   // [{entry, path}]
    async function readDir(dirEntry, basePath) {
      const reader = dirEntry.createReader();
      await new Promise((res, rej) => {
        function readBatch() {
          reader.readEntries(async batch => {
            if (!batch.length) { res(); return; }
            for (const e of batch) {
              if (e.isFile) allFiles.push({entry: e, path: basePath + e.name});
              else if (e.isDirectory) await readDir(e, basePath + e.name + '/');
            }
            readBatch();
          }, rej);
        }
        readBatch();
      });
    }

    for (const en of entries) {
      if (en.isDirectory) await readDir(en, en.name + '/');
      else allFiles.push({entry: en, path: en.name});
    }

    totalFiles = allFiles.length;
    zipSub.textContent = `Found ${totalFiles} file(s)…`;

    // Add each file to zip
    for (const {entry, path} of allFiles) {
      const file = await new Promise((res, rej) => entry.file(res, rej));
      zip.file(path, file);
      processed++;
      const pct = Math.round(processed / totalFiles * 60);  // 0–60% for reading
      zipBar.style.width  = pct + '%';
      zipPct.textContent  = pct + '%';
      zipSub.textContent  = `Adding ${processed}/${totalFiles}: ${entry.name}`;
    }

    // Determine zip name from top-level folder or "DropBeam_files"
    const folderName = entries.length === 1 && entries[0].isDirectory
                       ? entries[0].name
                       : 'DropBeam_files';
    const zipName = folderName + '.zip';

    zipSub.textContent = 'Compressing…';

    const blob = await zip.generateAsync(
      {type: 'blob', compression: 'DEFLATE', compressionOptions: {level: 6}},
      meta => {
        const pct = 60 + Math.round(meta.percent * 0.4);  // 60–100%
        zipBar.style.width  = pct + '%';
        zipPct.textContent  = pct + '%';
      }
    );

    overlay.classList.remove('show');
    zipBar.style.width = '0%';

    // Upload the zip as a regular file
    const zipFile = new File([blob], zipName, {type: 'application/zip'});
    const dt      = new DataTransfer();
    dt.items.add(zipFile);
    toast(`📦 "${zipName}" ready — uploading…`, 'ok');
    uploadFiles(dt.files);

  } catch(err) {
    overlay.classList.remove('show');
    console.error('Folder zip error', err);
    toast('❌ Folder zip failed: ' + err.message, 'err');
  }
}

/* ═══════════════════════════════════════════════════════════════
   FILE INPUT (label-linked — works on iOS Safari + Android)
═══════════════════════════════════════════════════════════════ */
document.getElementById('fileInput').addEventListener('change', function() {
  if (this.files && this.files.length) uploadFiles(this.files);
});

/* ═══════════════════════════════════════════════════════════════
   UPLOAD
═══════════════════════════════════════════════════════════════ */
function uploadFiles(files) {
  if (!files || !files.length) return;
  const fd  = new FormData();
  for (let i = 0; i < files.length; i++) fd.append('files', files[i]);

  const pw  = document.getElementById('progressWrap');
  const bar = document.getElementById('progBar');
  const lbl = document.getElementById('progLabel');
  const pct = document.getElementById('progPct');
  pw.style.display = 'block'; bar.style.width = '0%';
  lbl.textContent  = 'Uploading ' + files.length + ' file(s)…';
  pct.textContent  = '0%';

  const xhr = new XMLHttpRequest();
  xhr.open('POST', '/upload');
  xhr.upload.addEventListener('progress', e => {
    if (e.lengthComputable) {
      const p = Math.round(e.loaded / e.total * 100);
      bar.style.width = p + '%'; pct.textContent = p + '%';
    }
  });
  xhr.addEventListener('load', () => {
    pw.style.display = 'none'; bar.style.width = '0%';
    document.getElementById('fileInput').value = '';
    // SSE will update the file list — no need to call loadFiles() here
    if (xhr.status !== 200) toast('❌ Upload failed', 'err');
  });
  xhr.addEventListener('error', () => { pw.style.display='none'; toast('❌ Network error','err'); });
  xhr.send(fd);
}

/* ═══════════════════════════════════════════════════════════════
   FILE LIST
═══════════════════════════════════════════════════════════════ */
async function loadFiles() {
  const files = await fetch('/files').then(r => r.json()).catch(()=>[]);
  fileCache = files;
  renderFiles();
}

function renderFiles() {
  const list = document.getElementById('fileList');
  document.getElementById('sFiles').textContent     = fileCache.length;
  document.getElementById('countBadge').textContent = fileCache.length + ' FILE' + (fileCache.length!==1?'S':'');
  document.getElementById('sSize').textContent      = fmtSize(fileCache.reduce((s,f)=>s+(f.size||0),0));

  if (!fileCache.length) {
    list.innerHTML = '<div class="empty"><span class="empty-ico">🌌</span>No files yet.<br/>Upload something above.</div>';
    return;
  }

  const now = Date.now() / 1000;
  list.innerHTML = fileCache.map(f => {
    const hasExpiry = f.expires_at != null;
    const attrs     = hasExpiry
      ? `data-expires="${f.expires_at}" data-uploaded="${f.uploaded_at || ''}"`
      : '';
    const remain    = hasExpiry ? f.expires_at - now : 0;
    const total     = hasExpiry && f.uploaded_at ? f.expires_at - f.uploaded_at : 1;
    const pct       = hasExpiry ? Math.max(0, Math.min(100,(remain/total)*100)).toFixed(1) : '0';
    const soonClass = hasExpiry && pct < 20 ? 'expiring-soon' : '';

    return `<div class="file-item ${soonClass}" ${attrs} style="--expiry-pct:${pct}%">
      <div class="file-thumb">${fileIcon(f.name)}</div>
      <div class="file-meta">
        <div class="file-name" title="${esc(f.name)}">${esc(f.name)}</div>
        <div class="file-size">${fmtSize(f.size)}</div>
        <div class="expiry-tag">${hasExpiry ? '⏳ expires in ' + fmtRemain(remain) : ''}</div>
      </div>
      <div class="file-btns">
        <button class="icon-btn" title="Download" onclick="dlFile('${esc(f.filename)}')">⬇️</button>
        <button class="icon-btn del" title="Delete" onclick="delFile('${esc(f.id)}')">🗑️</button>
      </div>
    </div>`;
  }).join('');
}

function dlFile(fn) {
  const a = document.createElement('a');
  a.href = '/download/' + encodeURIComponent(fn); a.download = '';
  document.body.appendChild(a); a.click(); document.body.removeChild(a);
  toast('⬇️ Download started', 'ok');
}

async function delFile(id) {
  await fetch('/files/' + encodeURIComponent(id), {method:'DELETE'}).catch(()=>{});
  // SSE handles removal from UI
}

function copyUrl() {
  navigator.clipboard && serverUrl && navigator.clipboard.writeText(serverUrl)
    .then(()=>toast('📋 URL copied!','ok')).catch(()=>{});
}

/* ═══════════════════════════════════════════════════════════════
   HELPERS
═══════════════════════════════════════════════════════════════ */
function fmtSize(n) {
  if(!n) return '0 B';
  const u=['B','KB','MB','GB']; let i=0;
  while(n>=1024&&i<3){n/=1024;i++;} return(i===0?n:n.toFixed(1))+' '+u[i];
}
function fileIcon(name) {
  if(!name) return '📁';
  const ext = name.includes('.') ? name.split('.').pop().toLowerCase() : '';
  return ({pdf:'📄',txt:'📝',md:'📝',doc:'📝',docx:'📝',
    png:'🖼️',jpg:'🖼️',jpeg:'🖼️',gif:'🖼️',webp:'🖼️',svg:'🖼️',
    mp4:'🎬',mov:'🎬',avi:'🎬',mkv:'🎬',
    mp3:'🎵',wav:'🎵',flac:'🎵',m4a:'🎵',
    zip:'📦',rar:'📦','7z':'📦',tar:'📦',gz:'📦',
    exe:'⚙️',dmg:'⚙️',apk:'📱',
    xls:'📊',xlsx:'📊',csv:'📊',ppt:'📊',pptx:'📊',
    py:'🐍',js:'💻',ts:'💻',html:'🌐',css:'🎨',json:'📋'})[ext] || '📁';
}
function esc(s) {
  return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;')
    .replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}
let toastTimer;
function toast(msg, type) {
  const el = document.getElementById('toast');
  el.textContent = msg; el.className = 'show ' + (type||'');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(()=>{ el.className=''; }, 3500);
}

init();
</script>
</body>
</html>"""

# ─────────────────────────────────────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────

def main():
    global SERVER_IP, BASE_URL, MODE, EXPIRY_MIN

    parser = argparse.ArgumentParser(description="DropBeam v3")
    parser.add_argument("--hotspot", action="store_true",
                        help="PC creates its own WiFi hotspot (no router needed)")
    parser.add_argument("--port",   type=int, default=PORT)
    parser.add_argument("--expiry", type=int, default=0,
                        help="Auto-delete files after N minutes (0 = off, UI can override)")
    args = parser.parse_args()

    port       = args.port
    EXPIRY_MIN = args.expiry

    print()
    print("  ⚡  DropBeam v3 — Direct File Transfer (Python)")
    print("  ═══════════════════════════════════════════════════")
    print(f"  ✅  Real-time sync  (SSE push)")
    print(f"  ✅  File expiry     ({'ON — ' + str(EXPIRY_MIN) + ' min' if EXPIRY_MIN else 'OFF (configurable in UI)'})")
    print(f"  ✅  Folder upload   (client-side JSZip)")
    print(f"  ═══════════════════════════════════════════════════")

    if args.hotspot:
        MODE = "hotspot"
        print(f"  📡  Mode: HOTSPOT")
        ip = HOTSPOT_MGR.start()
        SERVER_IP = ip if ip else get_lan_ip()
        if not ip: MODE = "lan"
    else:
        MODE      = "lan"
        SERVER_IP = get_lan_ip()
        print(f"  🌐  Mode: LAN")

    BASE_URL = f"http://{SERVER_IP}:{port}"
    print(f"  ─────────────────────────────────────────────────────")
    print(f"  🖥️   Desktop : http://localhost:{port}")
    print(f"  📱  Mobile  : {BASE_URL}")
    if MODE == "hotspot":
        print(f"  📡  WiFi    : {HOTSPOT_SSID}  /  {HOTSPOT_PASS}")
    print(f"  ─────────────────────────────────────────────────────")
    print(f"  Ctrl+C to stop.\n")

    import atexit, signal
    def cleanup(*_):
        HOTSPOT_MGR.stop(); sys.exit(0)
    atexit.register(HOTSPOT_MGR.stop)
    signal.signal(signal.SIGINT, cleanup)
    if hasattr(signal, "SIGTERM"): signal.signal(signal.SIGTERM, cleanup)

    try:
        import webbrowser, threading
        threading.Timer(1.4, lambda: webbrowser.open(f"http://localhost:{port}")).start()
    except Exception: pass

    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False,
            threaded=True)   # threaded=True keeps SSE connections alive


if __name__ == "__main__":
    main()