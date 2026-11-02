"""DeepSeek signup driver via raw Chrome CDP WebSocket (tanpa Playwright).

KENAPA VERSI PLAYWRIGHT GAGAL ("try again" terus + halaman putih):

1. Playwright connect_over_cdp() MENEMPEL ke semua tab (Target.setAutoAttach)
   dan memanggil Runtime.enable + Page.enable + Network.enable per tab.
   reCAPTCHA mendeteksi kehadiran DevTools/CDP lewat efek Runtime.enable
   (console.debug / stack-trace trick) -> "try again" meski challenge sudah
   dikerjakan benar.
2. Playwright juga menerapkan emulasi media default (colorScheme='light')
   ke halaman yang dibungkusnya -> itulah kenapa tab yang dibuka bot tampak
   PUTIH, sedangkan tab yang kamu buka manual DARK MODE.
3. Solusi di sini: JANGAN pakai Playwright. Pakai raw CDP WebSocket per-tab,
   hanya memanggil domain yang benar-benar perlu (Input, Runtime.evaluate
   sekali-pakai, Page.navigate dengan transitionType 'typed'). Tanpa
   Runtime.enable, tanpa auto-attach, tanpa emulasi -> halaman tetap dark
   mode & reCAPTCHA tidak melihat CDP.

Navigasi dilakukan dengan transitionType 'typed' (sinyal Chrome bahwa URL
diketik user di address bar) — setara dengan mengetik di form URL browser,
tanpa perlu fokus omnibox (CDP menarget renderer, tidak bisa fokus omnibox
secara langsung).
"""

import base64
import json
import os
import random
import re
import subprocess
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import quote

try:
    import websocket  # websocket-client
except ImportError as _err:
    raise SystemExit(
        "Modul 'websocket-client' belum terpasang. Jalankan: pip install websocket-client"
    ) from _err

import config

HERE = os.path.dirname(os.path.abspath(__file__))

HOME_URL = "https://chat.deepseek.com/"
SIGNUP_URL = "https://chat.deepseek.com/sign_up"

RECAPTCHA_ANCHOR_IFRAME = (
    'iframe[title="reCAPTCHA"], '
    'iframe[src*="recaptcha/api2/anchor"], '
    'iframe[src*="recaptcha/enterprise/anchor"]'
)
RECAPTCHA_CHALLENGE_IFRAME = (
    'iframe[title*="recaptcha challenge"], '
    'iframe[src*="recaptcha/api2/bframe"], '
    'iframe[src*="recaptcha/enterprise/bframe"]'
)

SEND_CODE_PATTERN = r"get\s*code|send\s*code|kirim\s*kode|verification\s*code"
SUBMIT_PATTERN = r"sign\s*up|register|submit|continue|confirm|verify|daftar"

# =====================================================================
# CDP HTTP helpers (tanpa Playwright)
# =====================================================================


def _cdp_request(port, path, method="GET", timeout=5):
    url = f"http://127.0.0.1:{port}{path}"
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


def _cdp_alive(port):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=2):
            return True
    except Exception:
        return False


def _cdp_list(port):
    try:
        targets = _cdp_request(port, "/json/list", timeout=3)
    except Exception:
        return []
    return targets if isinstance(targets, list) else []


# =====================================================================
# CDPPage: raw WebSocket ke satu target (tab) — tanpa Runtime.enable
# =====================================================================


class CDPPage:
    def __init__(self, ws_url, target_id=None, timeout=30):
        self.ws_url = ws_url
        self.target_id = target_id
        self.timeout = timeout
        self.ws = websocket.create_connection(ws_url, timeout=timeout, enable_multithread=True)
        self._id = 0
        self._lock = threading.Lock()
        self._last_mouse = None

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass

    def cmd(self, method, params=None, timeout=None):
        timeout = timeout or self.timeout
        with self._lock:
            self._id += 1
            mid = self._id
            self.ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        deadline = time.time() + timeout
        while time.time() < deadline:
            self.ws.settimeout(max(0.2, deadline - time.time()))
            try:
                raw = self.ws.recv()
            except websocket.WebSocketTimeoutException:
                break
            except Exception:
                break
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError(f"CDP {method}: {msg['error'].get('message')}")
                return msg.get("result", {})
        raise TimeoutError(f"CDP {method} timeout {timeout}s")

    def evaluate(self, js, timeout=30, await_promise=True):
        res = self.cmd(
            "Runtime.evaluate",
            {"expression": js, "returnByValue": True, "awaitPromise": await_promise},
            timeout=timeout,
        )
        if "exceptionDetails" in res:
            detail = res["exceptionDetails"]
            text = detail.get("text") or detail.get("exception", {}).get("description") or "JS error"
            raise RuntimeError(text)
        return res.get("result", {}).get("value")

    def type_text(self, text, min_delay=0.045, max_delay=0.15):
        for ch in text:
            if ch == "\n":
                self.press_enter()
                continue
            self.cmd(
                "Input.dispatchKeyEvent",
                {"type": "keyDown", "key": ch, "text": ch, "unmodifiedText": ch},
                timeout=5,
            )
            self.cmd("Input.dispatchKeyEvent", {"type": "keyUp", "key": ch}, timeout=5)
            time.sleep(random.uniform(min_delay, max_delay))

    def press_enter(self):
        self.cmd(
            "Input.dispatchKeyEvent",
            {"type": "rawKeyDown", "key": "Enter", "code": "Enter", "windowsVirtualKeyCode": 13, "nativeVirtualKeyCode": 13},
            timeout=5,
        )
        self.cmd(
            "Input.dispatchKeyEvent",
            {"type": "keyUp", "key": "Enter", "code": "Enter", "windowsVirtualKeyCode": 13, "nativeVirtualKeyCode": 13},
            timeout=5,
        )

    def move_to(self, x, y):
        steps = random.randint(8, 22)
        if self._last_mouse:
            x0, y0 = self._last_mouse
        else:
            x0, y0 = x + random.randint(-80, 80), y + random.randint(-60, 60)
        for i in range(1, steps + 1):
            xi = x0 + (x - x0) * i / steps
            yi = y0 + (y - y0) * i / steps
            self.cmd(
                "Input.dispatchMouseEvent",
                {"type": "mouseMoved", "x": round(xi, 1), "y": round(yi, 1), "button": "none", "pointerType": "mouse"},
                timeout=5,
            )
            time.sleep(random.uniform(0.005, 0.018))
        for _ in range(random.randint(1, 3)):
            self.cmd(
                "Input.dispatchMouseEvent",
                {"type": "mouseMoved", "x": x + random.randint(-3, 3), "y": y + random.randint(-3, 3), "button": "none", "pointerType": "mouse"},
                timeout=5,
            )
            time.sleep(random.uniform(0.03, 0.1))
        self._last_mouse = (x, y)

    def click(self, x, y):
        self.move_to(x, y)
        self.cmd(
            "Input.dispatchMouseEvent",
            {"type": "mousePressed", "x": x, "y": y, "button": "left", "clickCount": 1, "pointerType": "mouse"},
            timeout=5,
        )
        time.sleep(random.uniform(0.04, 0.09))
        self.cmd(
            "Input.dispatchMouseEvent",
            {"type": "mouseReleased", "x": x, "y": y, "button": "left", "clickCount": 1, "pointerType": "mouse"},
            timeout=5,
        )

    def scroll(self, dy):
        x, y = self._last_mouse if self._last_mouse else (400, 300)
        self.cmd(
            "Input.dispatchMouseEvent",
            {"type": "mouseWheel", "x": x, "y": y, "deltaX": 0, "deltaY": dy, "pointerType": "mouse"},
            timeout=5,
        )


# =====================================================================
# DeepSeekSignup
# =====================================================================


class DeepSeekSignup:
    def __init__(self, run_id=1, human=True):
        self.run_id = run_id
        self.human = human
        self.port = None
        self._page = None
        self._rng = random.Random()

    # ---------------------------------------------------------------- util
    def _pause(self, lo=0.5, hi=1.6):
        if self.human:
            time.sleep(self._rng.uniform(lo, hi))

    def _chrome_exe(self):
        cands = [
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            os.path.join(os.environ.get("LOCALAPPDATA", ""), "Google", "Chrome", "Application", "chrome.exe"),
        ]
        for cand in cands:
            if os.path.exists(cand):
                return cand
        return "chrome.exe"

    def _real_profile_dir(self):
        try:
            return config.ensure_bot_profile()
        except Exception:
            prof = getattr(config, "REAL_CHROME_PROFILE", None)
            if prof and os.path.isdir(prof):
                return prof
        return os.path.join(os.environ.get("LOCALAPPDATA", ""), "Google", "Chrome", "User Data")

    def _kill_chrome(self):
        try:
            subprocess.run(["taskkill", "/F", "/IM", "chrome.exe", "/T"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass
        time.sleep(5)

    def _start_real_chrome(self, port):
        profile = self._real_profile_dir()
        exe = self._chrome_exe()
        args = [
            exe,
            f"--user-data-dir={profile}",
            f"--remote-debugging-port={port}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-blink-features=AutomationControlled",
            "--lang=en-US",
        ]
        if config.CHROME_PROFILE:
            args.append(f"--profile-directory={config.CHROME_PROFILE}")
            print(f"  [CDP] Profil: {config.CHROME_PROFILE}")
        print(f"  [CDP] User Data: {profile}")
        subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if not self._wait_cdp(port, timeout=45):
            raise RuntimeError(f"Chrome tidak membuka debug port {port} dalam 45 detik.")

    def _wait_cdp(self, port, timeout=45):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if _cdp_alive(port):
                return True
            time.sleep(1)
        return False

    def _cdp_new_tab(self, port, url):
        before = {t.get("id") for t in _cdp_list(port)}
        target = None
        encoded = quote(url, safe="")
        for method in ("PUT", "GET"):
            try:
                target = _cdp_request(port, f"/json/new?{encoded}", method=method, timeout=10)
                if isinstance(target, dict):
                    break
            except Exception:
                target = None
        tid = target.get("id") if isinstance(target, dict) else None
        ws_url = target.get("webSocketDebuggerUrl") if isinstance(target, dict) else None
        if not ws_url:
            deadline = time.time() + 10
            while time.time() < deadline and not ws_url:
                for t in _cdp_list(port):
                    if t.get("id") not in before and t.get("type") == "page" and t.get("webSocketDebuggerUrl"):
                        tid = t.get("id")
                        ws_url = t.get("webSocketDebuggerUrl")
                        break
                if not ws_url:
                    time.sleep(0.5)
        if tid:
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/json/activate/{tid}", timeout=3)
            except Exception:
                pass
        return {"id": tid, "webSocketDebuggerUrl": ws_url, "url": url}

    def _cdp_close(self, port, tid):
        if not tid:
            return
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/json/close/{tid}", timeout=5)
        except Exception:
            pass

    def _typed_navigate(self, page, url, timeout=45):
        try:
            page.cmd("Page.bringToFront", timeout=5)
        except Exception:
            pass
        try:
            page.cmd("Page.navigate", {"url": url, "transitionType": "typed"}, timeout=15)
        except Exception:
            try:
                page.cmd("Page.navigate", {"url": url}, timeout=15)
            except Exception:
                pass
        self._wait_ready(page, timeout=timeout)
        self._pause(1.2, 2.4)

    def _wait_ready(self, page, timeout=45):
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                state = page.evaluate("document.readyState")
                if state == "complete":
                    has = page.evaluate("document.body && document.body.innerText.trim().length > 0")
                    if has:
                        return True
            except Exception:
                pass
            time.sleep(1)
        return False

    def _find_js(self, selectors):
        return """(() => {
          const sels = %s;
          for (const s of sels) {
            try {
              const els = document.querySelectorAll(s);
              const rects = [];
              for (const el of els) { const r = el.getBoundingClientRect(); if (r.width > 0 && r.height > 0) rects.push({x: r.x + r.width/2, y: r.y + r.height/2, w: r.width, h: r.height}); }
              if (rects.length) return {ok: true, selector: s, rects: rects.slice(0, 8)};
            } catch (e) {}
          }
          return {ok: false, rects: []};
        })()""" % json.dumps(selectors)

    def _find_rect(self, page, selectors):
        try:
            info = page.evaluate(self._find_js(selectors))
        except Exception:
            return None
        if not info or not info.get("ok"):
            return None
        rects = info.get("rects") or []
        return rects[0] if rects else None

    def _find_all_rects(self, page, selector):
        js = """(() => {
          const els = document.querySelectorAll(%s);
          const out = [];
          for (const el of els) { const r = el.getBoundingClientRect(); if (r.width > 0 && r.height > 0) out.push({x: r.x + r.width/2, y: r.y + r.height/2, w: r.width, h: r.height}); }
          return {ok: out.length > 0, rects: out};
        })()""" % json.dumps(selector)
        try:
            info = page.evaluate(js)
        except Exception:
            return []
        if not info or not info.get("ok"):
            return []
        return info.get("rects") or []

    def _find_button_rect(self, page, pattern):
        js = """(() => {
          const re = new RegExp(%s, 'i');
          const cands = Array.from(document.querySelectorAll('button, [role="button"], a, div[role="button"]'));
          for (const el of cands) {
            const t = (el.innerText || el.textContent || '').trim();
            if (re.test(t)) {
              const r = el.getBoundingClientRect();
              if (r.width > 0 && r.height > 0) return {ok: true, x: r.x + r.width/2, y: r.y + r.height/2, w: r.width, h: r.height, text: t};
            }
          }
          return {ok: false};
        })()""" % json.dumps(pattern)
        try:
            info = page.evaluate(js)
        except Exception:
            return None
        if info and info.get("ok"):
            return info
        return None

    def _human_mouse_to(self, page, x, y):
        if not self.human:
            return
        steps = self._rng.randint(8, 22)
        if self._page and getattr(self._page, "_last_mouse", None):
            x0, y0 = self._page._last_mouse
        else:
            x0, y0 = x + self._rng.randint(-80, 80), y + self._rng.randint(-60, 60)
        for i in range(1, steps + 1):
            xi = x0 + (x - x0) * i / steps
            yi = y0 + (y - y0) * i / steps
            page.cmd("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": round(xi, 1), "y": round(yi, 1), "button": "none", "pointerType": "mouse"}, timeout=5)
            time.sleep(self._rng.uniform(0.005, 0.018))
        for _ in range(self._rng.randint(1, 3)):
            page.cmd("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x + self._rng.randint(-3, 3), "y": y + self._rng.randint(-3, 3), "button": "none", "pointerType": "mouse"}, timeout=5)
            time.sleep(self._rng.uniform(0.03, 0.1))
        page._last_mouse = (x, y)

    def _human_warmup(self, page):
        if not self.human:
            return
        vp_w, vp_h = 1280, 800
        try:
            size = page.evaluate("({w: window.innerWidth, h: window.innerHeight})")
            if isinstance(size, dict):
                vp_w = size.get("w") or vp_w
                vp_h = size.get("h") or vp_h
        except Exception:
            pass
        for _ in range(self._rng.randint(2, 4)):
            x = self._rng.randint(40, max(60, vp_w - 60))
            y = self._rng.randint(40, max(60, vp_h - 60))
            self._human_mouse_to(page, x, y)
            time.sleep(self._rng.uniform(0.1, 0.3))
        for _ in range(self._rng.randint(1, 2)):
            page.scroll(self._rng.randint(80, 320))
            time.sleep(self._rng.uniform(0.25, 0.8))
        self._pause(0.8, 1.6)

    def _screenshot(self, page, path):
        try:
            res = page.cmd("Page.captureScreenshot", {"format": "png"}, timeout=15)
            data = res.get("data")
            if data:
                with open(path, "wb") as f:
                    f.write(base64.b64decode(data))
                print(f"  [tab] Screenshot debug: {path}")
        except Exception as error:
            print(f"  [tab] Gagal screenshot: {error}")

    # ---------------------------------------------------------------- launch
    def launch_cdp(self):
        port = int(config.CDP_PORT or 9222)
        self.port = port
        if not _cdp_alive(port):
            print("  [CDP] Port belum aktif -> start Chrome...")
            self._kill_chrome()
            self._start_real_chrome(port)
        if not self._wait_cdp(port, timeout=45):
            raise RuntimeError(
                f"Chrome tidak membuka debug port {port} dalam 45 detik. "
                "Tutup semua Chrome lalu jalankan ulang py app.py."
            )
        print(f"  [CDP] Chrome siap (port {port})")

    # ---------------------------------------------------------------- tab
    def open_signup_tab(self):
        port = self.port
        print("  [tab] Membuat tab kosong (about:blank) via CDP...")
        target = self._cdp_new_tab(port, "about:blank")
        tid = target.get("id")
        ws_url = target.get("webSocketDebuggerUrl")
        if not ws_url:
            raise RuntimeError("Chrome tidak mengembalikan webSocketDebuggerUrl untuk tab baru.")
        page = CDPPage(ws_url, target_id=tid)
        self._page = page

        print("  [tab] Navigasi ke signup dengan transitionType 'typed' (seperti user mengetik URL)...")
        self._typed_navigate(page, SIGNUP_URL)

        # Jika redirect (profil sudah login DeepSeek), bersihkan storage lalu ulang.
        try:
            url = page.evaluate("location.href") or ""
        except Exception:
            url = ""
        if "sign_up" not in url:
            print("  [tab] Terdeteksi redirect (profil mungkin sudah login). Membersihkan storage...")
            try:
                page.evaluate(
                    """() => { try { localStorage.removeItem('userToken'); localStorage.removeItem('__appKit_userInfo'); sessionStorage.clear(); } catch(e) {} }"""
                )
            except Exception:
                pass
            self._typed_navigate(page, SIGNUP_URL)

        try:
            page.cmd("Page.bringToFront", timeout=5)
        except Exception:
            pass

        self._human_warmup(page)
        self._screenshot(page, os.path.join(HERE, "debug_signup.png"))

        # Bersihkan token lama (hanya key spesifik) setelah render.
        try:
            page.evaluate(
                """() => { try { localStorage.removeItem('userToken'); localStorage.removeItem('__appKit_userInfo'); } catch(e) {} }"""
            )
        except Exception:
            pass

        return page

    # ---------------------------------------------------------------- form
    def fill_form(self, page, email, password):
        email_rect = self._find_rect(page, [
            'input[type="email"]',
            'input[name="email"]',
            'input[placeholder*="mail" i]',
            'input[autocomplete="email"]',
        ])
        if not email_rect:
            raise RuntimeError("Input email tidak ditemukan di halaman signup.")
        self._human_mouse_to(page, email_rect["x"], email_rect["y"])
        page.click(email_rect["x"], email_rect["y"])
        self._pause(0.2, 0.5)
        page.type_text(email)
        self._pause(0.5, 1.2)

        pw_rects = self._find_all_rects(page, 'input[type="password"]')
        if not pw_rects:
            raise RuntimeError("Input password tidak ditemukan di halaman signup.")
        self._human_mouse_to(page, pw_rects[0]["x"], pw_rects[0]["y"])
        page.click(pw_rects[0]["x"], pw_rects[0]["y"])
        self._pause(0.2, 0.5)
        page.type_text(password)
        if len(pw_rects) > 1:
            self._pause(0.4, 0.9)
            self._human_mouse_to(page, pw_rects[1]["x"], pw_rects[1]["y"])
            page.click(pw_rects[1]["x"], pw_rects[1]["y"])
            self._pause(0.2, 0.5)
            page.type_text(password)
        self._pause(0.6, 1.4)

    # ---------------------------------------------------------------- recaptcha
    def recaptcha_present(self, page):
        try:
            return bool(page.evaluate("document.querySelectorAll('iframe[src*=\"recaptcha\"]').length > 0"))
        except Exception:
            return False

    def _recaptcha_response_value(self, page):
        try:
            v = page.evaluate(
                """(() => {
                  const t = document.querySelector('#g-recaptcha-response, [name="g-recaptcha-response"], textarea[name="g-recaptcha-response"]');
                  return t ? (t.value || '') : '';
                })()"""
            )
            return v or ""
        except Exception:
            return ""

    def recaptcha_solved(self, page):
        return len(self._recaptcha_response_value(page).strip()) > 0

    def recaptcha_failed(self, page):
        return False

    def solve_recaptcha_checkbox(self, page, attempts=2):
        if not self.recaptcha_present(page):
            return "absent"
        if self.recaptcha_solved(page):
            return "solved"

        anchor_rects = self._find_all_rects(
            page,
            'iframe[src*="recaptcha/api2/anchor"], iframe[src*="recaptcha/enterprise/anchor"], iframe[title="reCAPTCHA"]',
        )

        for attempt in range(1, attempts + 1):
            print(f"  [reCAPTCHA] Percobaan {attempt}/{attempts}...")
            if anchor_rects:
                r = anchor_rects[0]
                cx = r["x"] - r["w"] / 2 + 30
                cy = r["y"]
                self._human_mouse_to(page, cx, cy)
                page.click(cx, cy)

            deadline = time.time() + 20
            while time.time() < deadline:
                if self.recaptcha_solved(page):
                    print("  [reCAPTCHA] Checkbox tercentang.")
                    self._pause(0.6, 1.4)
                    return "solved"
                try:
                    challenge = page.evaluate(
                        "document.querySelectorAll('iframe[src*=\"recaptcha/api2/bframe\"], iframe[src*=\"recaptcha/enterprise/bframe\"], iframe[title*=\"challenge\"]').length > 0"
                    )
                except Exception:
                    challenge = False
                if challenge:
                    print("  [reCAPTCHA] Muncul tantangan gambar. Selesaikan manual di Chrome.")
                    return "challenge"
                time.sleep(0.5)

            wait_seconds = self._rng.randint(20, 40) * attempt
            print(f"  [reCAPTCHA] Gagal. Menunggu {wait_seconds}s sebelum mencoba lagi...")
            time.sleep(wait_seconds)

        return "failed"

    def wait_recaptcha_manual(self, page, timeout=180):
        if not self.recaptcha_present(page):
            return True
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.recaptcha_solved(page):
                return True
            time.sleep(2)
        return False

    # ---------------------------------------------------------------- send code / otp
    def click_send_code(self, page):
        btn = self._find_button_rect(page, SEND_CODE_PATTERN)
        if not btn:
            return False
        self._human_mouse_to(page, btn["x"], btn["y"])
        self._pause(0.3, 0.8)
        page.click(btn["x"], btn["y"])
        return True

    def submit_otp(self, page, otp):
        digits = re.sub(r"\D", "", otp)
        if not digits:
            raise RuntimeError(f"OTP tidak valid: {otp!r}")

        boxes = self._find_all_rects(page, 'input[maxlength="1"]')
        if len(boxes) >= len(digits):
            for index, digit in enumerate(digits):
                r = boxes[index]
                self._human_mouse_to(page, r["x"], r["y"])
                page.click(r["x"], r["y"])
                page.type_text(digit, min_delay=0.06, max_delay=0.18)
                time.sleep(self._rng.uniform(0.07, 0.18))
        else:
            single = self._find_rect(page, [
                ".ds-verify-code-form-item__verify-code-input",
                'input[maxlength="6"]',
                'input[autocomplete="one-time-code"]',
                'input[inputmode="numeric"]',
            ])
            if not single:
                raise RuntimeError("Input OTP tidak ditemukan.")
            self._human_mouse_to(page, single["x"], single["y"])
            page.click(single["x"], single["y"])
            self._pause(0.2, 0.5)
            page.type_text(digits)

        self._pause(0.5, 1.2)

        btn = self._find_button_rect(page, SUBMIT_PATTERN)
        if btn:
            self._human_mouse_to(page, btn["x"], btn["y"])
            page.click(btn["x"], btn["y"])
        else:
            page.press_enter()

    # ---------------------------------------------------------------- token
    def read_token(self, page):
        try:
            token = page.evaluate(
                """(() => {
                  try {
                    const raw = localStorage.getItem('userToken');
                    if (raw) {
                      try { const parsed = JSON.parse(raw); return (parsed && parsed.value) || raw; }
                      catch (e) { return raw; }
                    }
                  } catch (e) {}
                  try {
                    const info = localStorage.getItem('__appKit_userInfo');
                    if (info) { const parsed = JSON.parse(info); return parsed && parsed.token; }
                  } catch (e) {}
                  return '';
                })()"""
            )
        except Exception:
            return ""
        if not token or not isinstance(token, str):
            return ""
        return token.strip()

    def wait_for_token(self, page, timeout=120):
        deadline = time.time() + timeout
        while time.time() < deadline:
            token = self.read_token(page)
            if token:
                return token
            time.sleep(1)
        return ""

    # ---------------------------------------------------------------- cleanup
    def close_tab(self, page):
        try:
            if getattr(page, "target_id", None):
                self._cdp_close(self.port, page.target_id)
        except Exception:
            pass
        try:
            page.close()
        except Exception:
            pass

    def close(self):
        try:
            if self._page is not None:
                self._page.close()
        except Exception:
            pass


# =====================================================================
# Module-level helpers (kompatibilitas)
# =====================================================================


def ensure_debug_chrome(port=None):
    if port is None:
        port = int(config.CDP_PORT or 9222)

    def alive():
        return _cdp_alive(port)

    if not alive():
        signup = DeepSeekSignup()
        signup._kill_chrome()
        signup._start_real_chrome(port)

    deadline = time.time() + 60
    while time.time() < deadline:
        if alive():
            time.sleep(3)
            return port
        time.sleep(1)
    raise RuntimeError(f"Chrome debug port {port} tidak aktif setelah 60 detik.")
