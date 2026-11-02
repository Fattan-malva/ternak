import json
import os
import re
import subprocess
import time
import urllib.request

from playwright.sync_api import sync_playwright

import config
from voltmail import VoltMail

HERE = os.path.dirname(os.path.abspath(__file__))

STEALTH_JS = r"""
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
Object.defineProperty(navigator, 'plugins', { get: () => [1,2,3,4,5] });
if (!window.chrome) { window.chrome = {}; }
if (!window.chrome.runtime) { window.chrome.runtime = {}; }
const origQuery = window.navigator.permissions && window.navigator.permissions.query;
if (origQuery) {
  window.navigator.permissions.query = (p) => (
    p && p.name === 'notifications'
      ? Promise.resolve({ state: Notification.permission })
      : origQuery(p)
  );
}
"""


class DeepSeekSignup:
    def __init__(self, run_id=1):
        self.run_id = run_id
        self.volt = VoltMail()
        self._pw = None
        self.browser = None
        self.context = None
        self.page = None
        self.attached = False

    # ---------- chrome path ----------

    def _chrome_exe(self):
        cands = [
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            os.path.join(os.environ.get("LOCALAPPDATA", ""),
                         "Google", "Chrome", "Application", "chrome.exe"),
        ]
        for c in cands:
            if os.path.exists(c):
                return c
        return "chrome.exe"

    # ---------- real profile (cookies asli) ----------

    def _real_profile_dir(self):
        # Pastikan bot profile (non-default dir) tersedia; Chrome 136+ blokir
        # --remote-debugging-port pada default User Data dir.
        try:
            import config as _cfg_mod
            prof = _cfg_mod.ensure_bot_profile()
        except Exception:
            prof = config.REAL_CHROME_PROFILE
        if prof and os.path.isdir(prof):
            return prof
        return os.path.join(os.environ.get("LOCALAPPDATA", ""),
                            "Google", "Chrome", "User Data")

    # ---------- launch / CDP ----------

    def _kill_chrome(self):
        """Tutup semua chrome.exe agar profil tidak terkunci saat launch baru."""
        try:
            subprocess.run(
                ["taskkill", "/F", "/IM", "chrome.exe", "/T"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            pass
        time.sleep(5)

    def launch_cdp(self):
        """Attach ke Chrome yang sudah jalan (profil asli Anda).

        Kalau debug port belum aktif: tutup Chrome lama, nyalakan Chrome
        DENGAN profil asli + remote-debugging-port, lalu attach.
        Tidak ada profil bot baru.
        """
        self._pw = sync_playwright().start()
        port = int(config.CDP_PORT or 9222)

        if not self._cdp_alive(port):
            print("  Debug port belum aktif -> tutup Chrome lama, nyalakan Chrome (profil asli) dengan remote debugging...")
            self._kill_chrome()
            self._start_real_chrome(port)

        self.browser = self._pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
        self.context = (self.browser.contexts[0] if self.browser.contexts
                        else self.browser.new_context())
        self.attached = True
        print(f"  Terhubung ke Chrome (debug port {port})")

    def _start_real_chrome(self, port):
        profile = self._real_profile_dir()
        exe = self._chrome_exe()
        args = [
            exe,
            f"--user-data-dir={profile}",
            f"--remote-debugging-port={port}",
            "--no-first-run",
            "--no-default-browser-check",
        ]
        if config.CHROME_PROFILE:
            args.append(f"--profile-directory={config.CHROME_PROFILE}")
            print(f"  Profil: {config.CHROME_PROFILE}")
        print(f"  User Data: {profile}")
        subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        if not self._wait_cdp(port, timeout=30):
            raise RuntimeError(
                f"Chrome tidak membuka debug port {port} dalam 30 detik.\n"
                "Jalankan perintah ini secara manual, lalu coba 'py app.py' lagi:\n"
                f'  start chrome.exe --user-data-dir="{profile}" '
                f"--remote-debugging-port={port}"
                + (f" --profile-directory={config.CHROME_PROFILE}" if config.CHROME_PROFILE else "")
            )

    def _cdp_alive(self, port):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=2):
                return True
        except Exception:
            return False

    def _wait_cdp(self, port, timeout=60):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._cdp_alive(port):
                return True
            time.sleep(1)
        return False

    # ---------- tab / signup ----------

    def open_signup_tab(self):
        """Buka TAB BARU di Chrome yang sudah jalan, ke /sign_up."""
        page = self.context.new_page()
        try:
            self.context.add_init_script(STEALTH_JS)
        except Exception:
            pass

        page.goto("https://chat.deepseek.com/sign_up",
                  wait_until="domcontentloaded", timeout=60000)

        # bersihkan token lama di storage
        try:
            page.evaluate("""() => {
                localStorage.removeItem('userToken');
                localStorage.removeItem('__appKit_userInfo');
                sessionStorage.clear();
            }""")
        except Exception:
            pass

        # pastikan tab baru muncul di depan dan difokuskan (disebelah tab UI)
        try:
            page.wait_for_load_state("domcontentloaded", timeout=10000)
        except Exception:
            pass
        try:
            page.bring_to_front()
        except Exception:
            pass
        try:
            page.evaluate("window.focus()")
        except Exception:
            pass

        page.wait_for_timeout(600)
        return page

    def fill_form(self, page, email, password):
        p = page
        email_box = p.locator('input[type="email"], input[placeholder*="mail" i]').first
        email_box.fill(email, timeout=30000)

        pws = p.locator('input[type="password"]')
        pws.nth(0).fill(password)
        if pws.count() > 1:
            pws.nth(1).fill(password)

    def click_send_code(self, page):
        try:
            loc = page.get_by_role("button", name=re.compile(r"get code|send code|kirim kode|verification code", re.I)).first
            if loc.count() == 0:
                loc = page.get_by_text(re.compile(r"get code|send code", re.I)).first
            loc.click(timeout=15000)
            return True
        except Exception:
            return False

    def submit_otp(self, page, otp):
        box = page.locator(".ds-verify-code-form-item__verify-code-input, input[maxlength='6']").first
        box.fill(otp, timeout=30000)
        for name in (r"sign up|register|submit|continue|confirm", r"verify"):
            try:
                loc = page.get_by_role("button", name=re.compile(name, re.I)).first
                if loc.count() == 0:
                    loc = page.get_by_text(re.compile(name, re.I)).first
                loc.click(timeout=5000)
                return
            except Exception:
                continue
        page.keyboard.press("Enter")

    def read_token(self, page):
        try:
            raw = page.evaluate("() => localStorage.getItem('userToken')")
        except Exception:
            return ""
        if not raw:
            return ""
        try:
            return json.loads(raw).get("value") or ""
        except Exception:
            return raw

    def close_tab(self, page):
        try:
            page.close()
        except Exception:
            pass

    def close(self):
        # Jangan tutup browser Chrome asli Anda; hanya putuskan sambungan playwright.
        try:
            if self._pw:
                self._pw.stop()
        except Exception:
            pass


def ensure_debug_chrome(port=None):
    """Pastikan Chrome (profil asli) sudah berjalan dengan remote debugging.

    Kalau port belum aktif: nyalakan Chrome dengan profil asli + debug port.
    Mengembalikan port yang aktif. Dipakai app.py sebelum membuka web UI
    agar tidak ada konflik profil dan tidak ada profil bot baru.
    """
    import time as _time
    import urllib.request

    if port is None:
        port = int(config.CDP_PORT or 9222)

    def alive():
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=2):
                return True
        except Exception:
            return False

    if not alive():
        _signup = DeepSeekSignup()
        _signup._kill_chrome()
        _signup._start_real_chrome(port)

    deadline = _time.time() + 60
    while _time.time() < deadline:
        if alive():
            _time.sleep(3)  # tunggu Chrome siap sepenuhnya setelah debug port hidup
            return port
        _time.sleep(1)
    raise RuntimeError(f"Chrome debug port {port} tidak aktif setelah 60 detik.")


def open_url_in_chrome(url, port=None):
    """Buka URL di Chrome yang sudah berjalan dengan remote debugging.

    Digunakan supaya UI web (http://127.0.0.1:8765/) terbuka di Chrome
    yang sama (satu jendela), bukan jendela browser terpisah.
    """
    import time as _time
    import urllib.request

    from playwright.sync_api import sync_playwright

    if port is None:
        port = int(config.CDP_PORT or 9222)

    def alive():
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=2):
                return True
        except Exception:
            return False

    if not alive():
        raise RuntimeError(f"Chrome debug port {port} belum aktif.")

    pw = sync_playwright().start()
    try:
        browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
        context = browser.contexts[0] if browser.contexts else browser.new_context()

        # retry navigasi sampai berhasil (Chrome mungkin masih initializing)
        last_err = None
        for _ in range(5):
            try:
                if context.pages:
                    page = context.pages[0]
                    page.goto(url, wait_until="domcontentloaded", timeout=30000)
                else:
                    page = context.new_page()
                    page.goto(url, wait_until="domcontentloaded", timeout=30000)
                break
            except Exception as e:
                last_err = e
                _time.sleep(2)
        else:
            raise last_err

        # tunggu sebentar supaya halaman benar-benar render sebelum putus
        _time.sleep(2)
        try:
            page.bring_to_front()
        except Exception:
            pass
    finally:
        try:
            pw.stop()
        except Exception:
            pass
