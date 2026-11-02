"""
Local UI launcher.

Run:
    py app.py

Starts the local UI server first, then starts Chrome with the configured
profile + CDP port and opens the local UI in a tab of that same Chrome.

Catatan penting:
- Server HTTP dinyalakan (serve_forever) SEBELUM Chrome dibuka, supaya
  permintaan dari Chrome tidak menggantung di backlog socket.
- Tab dibuka lewat CDP HTTP endpoint (/json/new), bukan Playwright, supaya
  tidak ada proses node/driver yang bisa menggantung saat startup.
"""

import json
import os
import subprocess
import threading
import time
import traceback
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote, urlparse

import auto_runner
import config as cfg_mod

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = 8765
UI_URL = f"http://127.0.0.1:{PORT}/"
SIGNUP_URL = "https://chat.deepseek.com/sign_up"

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# config.py sudah auto-create config.json, output/, chrome-bot-profile/
# saat di-import. Pastikan itu terjadi sebelum app.py mulai.
CONFIG_FILE = cfg_mod.CFG_PATH


def load_config():
    with open(CONFIG_FILE, "r", encoding="utf-8") as file:
        return json.load(file)


# ---------------------------------------------------------------------------
# CDP helpers (pure HTTP, tanpa Playwright)
# ---------------------------------------------------------------------------

def cdp_alive(cdp_port, timeout=2):
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{cdp_port}/json/version", timeout=timeout
        ) as response:
            return response.status == 200
    except Exception:
        return False


def cdp_request(cdp_port, path, method="GET", timeout=5):
    url = f"http://127.0.0.1:{cdp_port}{path}"
    request = urllib.request.Request(url, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", "replace")[:500]
        raise RuntimeError(f"CDP HTTP {error.code} untuk {path}: {body}") from error
    except Exception as error:
        raise RuntimeError(f"CDP request gagal untuk {path}: {error}") from error

    raw = raw.strip()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"CDP response bukan JSON untuk {path}: {raw[:300]}") from error


def cdp_list_tabs(cdp_port):
    try:
        targets = cdp_request(cdp_port, "/json/list", timeout=3)
    except Exception:
        return []
    return targets if isinstance(targets, list) else []


def cdp_activate(cdp_port, target_id):
    if not target_id:
        return
    try:
        cdp_request(cdp_port, f"/json/activate/{target_id}", timeout=3)
    except Exception as error:
        print(f"  [CDP] Gagal activate tab {target_id}: {error}")


def cdp_new_tab(cdp_port, url):
    # Endpoint Chrome: /json/new?<url>. URL harus di-encode agar aman.
    encoded_url = quote(url, safe="")
    path = f"/json/new?{encoded_url}"
    print(f"  [CDP] Membuat tab baru: {url}")
    try:
        # Chrome 111+ wajib PUT.
        target = cdp_request(cdp_port, path, method="PUT", timeout=10)
    except RuntimeError:
        print("  [CDP] PUT gagal, mencoba GET...")
        target = cdp_request(cdp_port, path, method="GET", timeout=10)
    if not isinstance(target, dict):
        raise RuntimeError("Chrome tidak mengembalikan info tab baru.")
    return target


def open_tab(url, cdp_port, reuse_prefix=None):
    """Buka URL di tab Chrome yang sudah berjalan (tanpa Playwright)."""
    print(f"[open_tab] url={url} cdp_port={cdp_port}")
    if reuse_prefix:
        for target in cdp_list_tabs(cdp_port):
            target_url = str(target.get("url", ""))
            target_id = target.get("id")
            target_type = target.get("type")
            print(f"  [CDP] tab existing: type={target_type} url={target_url[:80]}")
            if target_type == "page" and target_url.startswith(reuse_prefix):
                print("  [CDP] Reuse tab yang sudah ada.")
                cdp_activate(cdp_port, target_id)
                return {"id": target_id, "reused": True}

    target = cdp_new_tab(cdp_port, url)
    print(f"  [CDP] Tab baru: id={target.get('id')} url={target.get('url', '')[:80]}")
    cdp_activate(cdp_port, target.get("id"))
    return target


# ---------------------------------------------------------------------------
# Chrome bootstrap
# ---------------------------------------------------------------------------

def find_chrome_exe():
    """Cari lokasi chrome.exe di lokasi-lokasi umum Windows."""
    candidates = [
        os.path.join(os.environ.get("PROGRAMFILES", ""), "Google", "Chrome", "Application", "chrome.exe"),
        os.path.join(os.environ.get("PROGRAMFILES(X86)", ""), "Google", "Chrome", "Application", "chrome.exe"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Google", "Chrome", "Application", "chrome.exe"),
    ]
    for candidate in candidates:
        if candidate and os.path.isfile(candidate):
            return candidate
    # Fallback: cari lewat PATH / App Paths registry.
    try:
        completed = subprocess.run(
            ["where.exe", "chrome.exe"],
            capture_output=True,
            text=True,
            timeout=5,
            creationflags=CREATE_NO_WINDOW,
        )
        for line in (completed.stdout or "").splitlines():
            line = line.strip()
            if line and os.path.isfile(line):
                return line
    except Exception:
        pass
    return None


def chrome_process_running():
    try:
        completed = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq chrome.exe", "/NH"],
            capture_output=True,
            text=True,
            timeout=10,
            creationflags=CREATE_NO_WINDOW,
        )
        return "chrome.exe" in (completed.stdout or "").lower()
    except Exception:
        return False


def start_chrome():
    cfg = load_config()
    cdp_port = int(cfg.get("cdp_port", 9222))
    chrome_profile = str(cfg.get("chrome_profile", "Default"))

    # Pastikan bot profile (non-default dir) tersedia. Chrome 136+ blokir
    # --remote-debugging-port pada default User Data dir.
    user_data_dir = cfg_mod.ensure_bot_profile()
    print(f"[Chrome] User data dir: {user_data_dir}")

    if cdp_alive(cdp_port):
        print(f"[Chrome] CDP sudah aktif di port {cdp_port}.")
        return cdp_port

    already_running = chrome_process_running()

    if already_running and not cdp_alive(cdp_port):
        print("[Chrome] Chrome sudah berjalan (tanpa CDP). Menutup semua chrome.exe...")
        print("[Chrome] (tab-tab sebelumnya akan di-restore saat Chrome dibuka ulang)")
        try:
            subprocess.run(
                ["taskkill", "/F", "/IM", "chrome.exe", "/T"],
                capture_output=True,
                timeout=20,
                creationflags=CREATE_NO_WINDOW,
            )
        except Exception as error:
            print(f"[Chrome] Gagal taskkill: {error}")
        # Tunggu Chrome benar-benar mati (maks 15 detik)
        for i in range(30):
            if not chrome_process_running():
                print(f"[Chrome] Semua proses Chrome mati ({i * 0.5:.0f} detik).")
                break
            time.sleep(0.5)
        else:
            raise RuntimeError(
                "Chrome masih berjalan setelah taskkill. "
                "Tutup Chrome manual dari Task Manager, lalu jalankan py app.py lagi."
            )
        # Jeda ekstra supaya file lock profil dilepas
        time.sleep(2)
        print("[Chrome] Chrome sudah ditutup, membuka ulang dengan CDP...")

    # Panggil chrome.exe LANGSUNG lewat subprocess.Popen dengan list.
    chrome_exe = find_chrome_exe()
    if chrome_exe:
        cmd = [
            chrome_exe,
            f"--user-data-dir={user_data_dir}",
            f"--remote-debugging-port={cdp_port}",
            f"--profile-directory={chrome_profile}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-blink-features=AutomationControlled",
            UI_URL,
        ]
        print(f"[Chrome] Menjalankan langsung: {chrome_exe}")
        subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=CREATE_NO_WINDOW,
        )
    else:
        # Fallback PowerShell: pakai SATU string argumen dengan quote internal.
        ps_args = (
            f'--user-data-dir="{user_data_dir}" '
            f'--remote-debugging-port={cdp_port} '
            f'--profile-directory={chrome_profile} '
            f'--no-first-run '
            f'--no-default-browser-check '
            f'{UI_URL}'
        )
        ps_command = f"Start-Process chrome.exe -ArgumentList '{ps_args}'"
        print(f"[Chrome] chrome.exe tidak ditemukan di lokasi umum, pakai PowerShell:")
        print(f"[Chrome] {ps_command}")
        subprocess.Popen(
            ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps_command],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=CREATE_NO_WINDOW,
        )

    for i in range(30):
        if cdp_alive(cdp_port):
            print(f"[Chrome] CDP aktif di port {cdp_port} (dicoba {i+1} detik).")
            return cdp_port
        time.sleep(1)

    raise RuntimeError(
        f"Chrome tidak membuka CDP pada port {cdp_port}. "
        "Pastikan Chrome tidak diblokir antivirus / firewall, lalu jalankan py app.py lagi."
    )


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

class QuietThreadingHTTPServer(ThreadingHTTPServer):
    """ThreadingHTTPServer yang tidak memuntahkan traceback untuk
    koneksi yang ditutup klien (WinError 10054 / ConnectionResetError),
    sehingga terminal tetap bersih dan hanya menampilkan log bot.
    """

    def handle_error(self, request, client_address):
        import sys
        exc = sys.exc_info()[1]
        # Abaikan: koneksi ditutup klien, koneksi di-reset, pipe putus.
        if isinstance(exc, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)):
            return
        super().handle_error(request, client_address)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def send_body(self, status, content_type, body):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_body(status, "application/json; charset=utf-8", body)

    def do_GET(self):
        path = urlparse(self.path).path

        if path in ("/", "/index.html"):
            try:
                with open(os.path.join(HERE, "index.html"), "rb") as file:
                    body = file.read()
                self.send_body(200, "text/html; charset=utf-8", body)
            except OSError as error:
                self.send_json(500, {"error": str(error)})
            return

        if path == "/api/config":
            cfg = load_config()
            self.send_json(200, {
                "password": cfg.get("password", ""),
                "cdp_port": int(cfg.get("cdp_port", 9222)),
                "chrome_profile": cfg.get("chrome_profile", "Default"),
            })
            return

        if path == "/api/results":
            self.send_json(200, {"results": auto_runner.load_results()})
            return

        if path == "/api/status":
            self.send_json(200, {"job": auto_runner.current_job()})
            return

        self.send_json(404, {"error": "not found"})

    def do_POST(self):
        path = urlparse(self.path).path

        if path == "/api/delete":
            try:
                length = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
                rows = auto_runner.load_results()
                if payload.get("all"):
                    rows = []
                else:
                    index = payload.get("index")
                    if not isinstance(index, int) or index < 0 or index >= len(rows):
                        self.send_json(400, {"ok": False})
                        return
                    rows.pop(index)
                os.makedirs(os.path.dirname(auto_runner.OUT_FILE), exist_ok=True)
                with open(auto_runner.OUT_FILE, "w", encoding="utf-8") as file:
                    for row in rows:
                        file.write(json.dumps(row, ensure_ascii=False) + "\n")
                self.send_json(200, {"ok": True})
            except Exception as error:
                self.send_json(500, {"error": str(error), "trace": traceback.format_exc()})
            return

        if path == "/api/start":
            try:
                job_id, error = auto_runner.start_job()
                if error:
                    self.send_json(400, {"ok": False, "error": error})
                    return
                print(f"[API /start] Job dimulai: {job_id}")
                self.send_json(200, {"ok": True, "job_id": job_id})
            except Exception as error:
                print(f"[API /start] ERROR: {error}")
                traceback.print_exc()
                self.send_json(500, {"error": str(error), "trace": traceback.format_exc()})
            return

        if path == "/api/refresh-otp":
            try:
                length = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
                job_id = payload.get("job_id")
                otp, error = auto_runner.refresh_otp_now(job_id)
                if error:
                    self.send_json(400, {"ok": False, "error": error})
                    return
                self.send_json(200, {"ok": True, "otp": otp})
            except Exception as error:
                self.send_json(500, {"error": str(error), "trace": traceback.format_exc()})
            return

        if path == "/api/open-signup":
            try:
                cfg = load_config()
                cdp_port = int(cfg.get("cdp_port", 9222))
                print(f"[API /open-signup] cdp_port={cdp_port}")
                if not cdp_alive(cdp_port):
                    raise RuntimeError(
                        f"Chrome CDP tidak aktif di port {cdp_port}. "
                        "Jalankan ulang py app.py."
                    )
                open_tab(SIGNUP_URL, cdp_port)
                self.send_json(200, {"ok": True, "url": SIGNUP_URL})
            except Exception as error:
                print(f"[API /open-signup] ERROR: {error}")
                traceback.print_exc()
                self.send_json(500, {"error": str(error), "trace": traceback.format_exc()})
            return

        self.send_json(404, {"error": "not found"})

    def log_message(self, *_args):
        pass


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    try:
        server = QuietThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError as error:
        print(f"Port {PORT} tidak bisa dipakai: {error}")
        print("Kemungkinan masih ada 'py app.py' lain yang berjalan.")
        print("Tutup proses lama itu (Task Manager -> python.exe) lalu coba lagi.")
        return

    server.daemon_threads = True

    serving = threading.Thread(target=server.serve_forever, name="http-server", daemon=True)
    serving.start()
    print(f"[Server] Berjalan di {UI_URL}")

    def setup_chrome():
        try:
            cdp_port = start_chrome()
            time.sleep(1)  # beri jeda sebentar agar window Chrome siap
            open_tab(UI_URL, cdp_port, reuse_prefix=UI_URL)
            print(f"[Chrome] UI dibuka di Chrome: {UI_URL}")
            print(f"[Chrome] CDP: 127.0.0.1:{cdp_port}")
        except Exception as error:
            print(f"[Chrome] Gagal menyiapkan Chrome otomatis: {error}")
            traceback.print_exc()
            print(f"[Chrome] Buka UI secara manual di browser: {UI_URL}")

    threading.Thread(target=setup_chrome, name="chrome-setup", daemon=True).start()

    print("[Server] Klik 'Buat akun baru' untuk membuka halaman signup di tab Chrome yang sama.")

    try:
        while serving.is_alive():
            time.sleep(0.4)
    except KeyboardInterrupt:
        print("\nServer dihentikan.")
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
