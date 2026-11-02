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


def _msg_time(msg):
    """Best-effort epoch (float) dari berbagai field timestamp VoltMail. None jika tidak ada."""
    if not isinstance(msg, dict):
        return None
    for key in ("createdAt", "created_at", "receivedAt", "received_at", "date", "timestamp", "time", "updatedAt", "updated_at"):
        v = msg.get(key)
        if v is None:
            continue
        if isinstance(v, (int, float)):
            # miliseconds -> seconds
            return v / 1000.0 if v > 1e12 else float(v)
        if isinstance(v, str):
            v = v.strip()
            try:
                # ISO 8601
                from datetime import datetime
                # handle trailing Z
                txt = v.replace("Z", "+00:00")
                return datetime.fromisoformat(txt).timestamp()
            except Exception:
                try:
                    return float(v)
                except Exception:
                    continue
    return None



class VoltMail:
    def __init__(self):
        self.session = requests.Session()
        self.token = None
        self.address = None
        self._seen_ids = set()
        self._started_at = None

    # ---------------------------------------------------------------- inbox
    def create_inbox(self, domain=None):
        if domain is None:
            doms = self.session.get(f"{BASE}/api/domains", timeout=10).json()["domains"]
            domain = doms[0]["domain"]

        r = self.session.post(f"{BASE}/api/generate", json={"domain": domain}, timeout=10)
        if r.status_code == 429:
            time.sleep(int(r.headers.get("Retry-After", 60)))
            return self.create_inbox(domain)

        data = r.json()
        self.address = data["account"]["address"]
        self.token = data["token"]
        self._seen_ids = set()
        self._started_at = None
        return self.address, self.token, data.get("resumeCode", "")

    def _headers(self):
        return {"Authorization": f"Bearer {self.token}"}

    # ---------------------------------------------------------------- fetch
    def _messages(self, timeout=8):
        r = self.session.get(f"{BASE}/api/messages", headers=self._headers(), timeout=timeout)
        r.raise_for_status()
        return r.json().get("messages", []) or []

    def _message_text(self, mid, timeout=8):
        r = self.session.get(f"{BASE}/api/messages/{mid}", headers=self._headers(), timeout=timeout)
        r.raise_for_status()
        msg = r.json().get("message", {}) or {}
        text = msg.get("text")
        if text:
            return text
        return _html_to_text(msg.get("html") or "")

    # ---------------------------------------------------------------- otp
    def mark_start(self):
        """Tandai waktu mulai menunggu OTP (untuk filter pesan lama)."""
        import time
        self._started_at = time.time()
        self._seen_ids = set()

    def check_otp_once(self):
        """Satu putaran polling: ambil OTP dari pesan TERBARU.

        Strategi:
        - Ambil semua pesan.
        - Abaikan pesan yang sudah terbukti bukan OTP (_seen_ids).
        - Abaikan pesan dengan timestamp < _started_at (pesan lama/stale),
          supaya OTP lama tidak disalahgunakan.
        - Urutkan kandidat berdasarkan timestamp (terbaru dulu); jika tidak
          ada timestamp, pertahankan urutan API.
        - Kembalikan OTP dari pesan terbaru yang mengandung OTP.
        Return kode OTP (string) atau None.
        """
        try:
            msgs = self._messages()
        except Exception:
            return None

        # Jika API mengembalikan terbaru->terlama, balikkan agar terlama->terbaru
        # lalu kita sort eksplisit. Kita tidak berasumsi; sort by timestamp.
        candidates = []
        for msg in msgs:
            mid = msg.get("id")
            if mid is None:
                continue
            if mid in self._seen_ids:
                continue
            t = _msg_time(msg)
            if self._started_at is not None and t is not None and t < self._started_at - 5:
                # pesan lama (stale) -> tandai seen supaya tidak di-fetch ulang
                self._seen_ids.add(mid)
                continue
            candidates.append((t, mid, msg))

        # Sort: timestamp desc; None timestamps dianggap paling lama (di akhir)
        def key(x):
            t = x[0]
            return (0, -t) if t is not None else (1, 0)
        candidates.sort(key=key)

        for t, mid, msg in candidates:
            try:
                text = self._message_text(mid)
            except Exception:
                # Gagal sementara -> jangan tandai seen, coba lagi nanti.
                continue
            otp = extract_otp(text)
            print(f"  [VoltMail] msg id={mid} ts={t} -> otp={otp}")
            print(f"  [VoltMail] text snippet: {text[:400].replace(chr(10),' ')}")
            if otp:
                print(f"  [VoltMail] OTP dipilih: {otp}")
                return otp
            # Terbukti bukan OTP
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
