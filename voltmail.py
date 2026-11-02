import re
import time
import requests

BASE = "https://voltmail.my.id"


class VoltMail:
    def __init__(self):
        self.session = requests.Session()
        self.token = None
        self.address = None

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
        return self.address, self.token, data.get("resumeCode", "")

    def _headers(self):
        return {"Authorization": f"Bearer {self.token}"}

    def check_otp_once(self):
        """One poll. Returns OTP code or None."""
        try:
            msgs = self.session.get(
                f"{BASE}/api/messages", headers=self._headers(), timeout=15
            ).json().get("messages", [])
            if not msgs:
                return None
            mid = msgs[-1]["id"]
            msg = self.session.get(
                f"{BASE}/api/messages/{mid}", headers=self._headers(), timeout=15
            ).json()["message"]
            text = msg.get("text") or re.sub(r"<[^>]+>", " ", msg.get("html") or "")
            m = re.search(r"\b(\d{4,8})\b", text)
            return m.group(1) if m else None
        except Exception:
            return None

    def wait_for_otp(self, timeout=180):
        """Poll inbox. Reads are not rate-limited. Returns 4-8 digit code or None."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                msgs = self.session.get(
                    f"{BASE}/api/messages", headers=self._headers(), timeout=30
                ).json().get("messages", [])
                if msgs:
                    mid = msgs[-1]["id"]
                    msg = self.session.get(
                        f"{BASE}/api/messages/{mid}", headers=self._headers(), timeout=30
                    ).json()["message"]
                    text = msg.get("text") or re.sub(r"<[^>]+>", " ", msg.get("html") or "")
                    m = re.search(r"\b(\d{4,8})\b", text)
                    if m:
                        return m.group(1)
            except Exception:
                pass
            time.sleep(3)
        return None