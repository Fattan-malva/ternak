"""VoltMail API client — pembuatan inbox + pengambilan OTP yang tangguh.

Perbaikan dibanding versi lama:
- check_otp_once() memeriksa SEMUA pesan (terbaru dulu), bukan hanya pesan
  terakhir, jadi OTP tidak terlewat kalau ada email lain masuk setelahnya.
- Pesan yang sudah diperiksa dan terbukti BUKAN OTP diingat (_seen_ids)
  supaya tidak di-fetch ulang tiap polling (lebih cepat + hemat request).
- Ekstraksi OTP bertingkat:
    1. angka 4-8 digit dekat kata "code/kode/otp/verify/verification"
    2. angka 6 digit berdiri sendiri
    3. angka 4-8 digit apa pun (tahun seperti 2025/2026 diabaikan)
- "123 456" (digit dipisah spasi) otomatis digabung jadi "123456".
- Gagal fetch sementara tidak menandai pesan sebagai "seen", jadi pesan itu
  tetap dicoba lagi di polling berikutnya.
"""

import re
import time

import requests

BASE = "https://voltmail.my.id"

# 1) angka dekat kata kunci OTP
OTP_NEAR_PATTERN = re.compile(
    r"(?:code|kode|otp|verif(?:y|ication))\D{0,60}?(\d{4,8})", re.I
)
# 2) angka 6 digit berdiri sendiri (panjang OTP DeepSeek)
OTP_SIX_PATTERN = re.compile(r"(?<!\d)(\d{6})(?!\d)")
# 3) angka 4-8 digit berdiri sendiri (fallback)
OTP_ANY_PATTERN = re.compile(r"(?<!\d)(\d{4,8})(?!\d)")


def _html_to_text(html):
    """HTML mentah -> teks, plus gabungkan digit yang dipisah spasi."""
    if not html:
        return ""
    html = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.I | re.S)
    text = re.sub(r"<[^>]+>", " ", html)
    text = text.replace("&nbsp;", " ").replace("&#160;", " ")
    # "1 2 3 4 5 6" -> "123456" (hanya digit-spasi-digit yang digabung)
    text = re.sub(r"(\d)\s+(?=\d)", r"\1", text)
    return text


def extract_otp(text):
    """Ambil kode OTP dari teks email. Return string atau None."""
    if not text:
        return None

    # Prioritas 1: angka tepat setelah kata kunci (paling akurat)
    m = OTP_NEAR_PATTERN.search(text)
    if m:
        return m.group(1)

    # Prioritas 2: angka 6 digit berdiri sendiri
    m = OTP_SIX_PATTERN.search(text)
    if m:
        return m.group(1)

    # Prioritas 3: angka 4-8 digit apa pun, skip yang terlihat seperti tahun
    for m in OTP_ANY_PATTERN.finditer(text):
        digits = m.group(1)
        if len(digits) == 4 and digits[:2] in ("19", "20"):
            continue
        return digits

    return None


class VoltMail:
    def __init__(self):
        self.session = requests.Session()
        self.token = None
        self.address = None
        self._seen_ids = set()

    # ---------------------------------------------------------------- inbox
    def create_inbox(self, domain=None):
        if domain is None:
            doms = self.session.get(f"{BASE}/api/domains", timeout=30).json()["domains"]
            domain = doms[0]["domain"]

        r = self.session.post(f"{BASE}/api/generate", json={"domain": domain}, timeout=30)
        if r.status_code == 429:
            time.sleep(int(r.headers.get("Retry-After", 60)))
            return self.create_inbox(domain)

        data = r.json()
        self.address = data["account"]["address"]
        self.token = data["token"]
        self._seen_ids = set()
        return self.address, self.token, data.get("resumeCode", "")

    def _headers(self):
        return {"Authorization": f"Bearer {self.token}"}

    # ---------------------------------------------------------------- fetch
    def _messages(self, timeout=20):
        r = self.session.get(f"{BASE}/api/messages", headers=self._headers(), timeout=timeout)
        r.raise_for_status()
        return r.json().get("messages", []) or []

    def _message_text(self, mid, timeout=20):
        r = self.session.get(f"{BASE}/api/messages/{mid}", headers=self._headers(), timeout=timeout)
        r.raise_for_status()
        msg = r.json().get("message", {}) or {}
        text = msg.get("text")
        if text:
            return text
        return _html_to_text(msg.get("html") or "")

    # ---------------------------------------------------------------- otp
    def check_otp_once(self):
        """Satu putaran polling: periksa semua pesan, TERBARU dulu.

        Return kode OTP (string) atau None.
        """
        try:
            msgs = self._messages()
        except Exception:
            return None

        # Daftar pesan VoltMail: terlama -> terbaru, jadi iterasi dibalik.
        for msg in reversed(msgs):
            mid = msg.get("id")
            if mid is None:
                continue
            if mid in self._seen_ids:
                continue

            try:
                text = self._message_text(mid)
            except Exception:
                # Gagal sementara (network/server) -> JANGAN tandai seen,
                # supaya pesan ini dicoba lagi pada polling berikutnya.
                continue

            otp = extract_otp(text)
            if otp:
                return otp

            # Terbukti bukan OTP -> tandai supaya tidak di-fetch ulang.
            self._seen_ids.add(mid)

        return None

    def wait_for_otp(self, timeout=180):
        """Polling sampai OTP diterima atau timeout. Return OTP atau None."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            otp = self.check_otp_once()
            if otp:
                return otp
            time.sleep(2)
        return None
