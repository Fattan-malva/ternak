"""Automated DeepSeek signup runner (CDP, logout-first, auto OTP + token)."""

import json
import os
import threading
import time
import uuid

import config
from voltmail import VoltMail

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(HERE, "output")
OUT_FILE = os.path.join(OUT_DIR, "results.jsonl")

JOBS = {}
_LAST_JOB = None
_LOCK = threading.Lock()


def _log(job, msg):
    with _LOCK:
        job["log"].append(f"[{time.strftime('%H:%M:%S')}] {msg}")


def _set(job, **kw):
    with _LOCK:
        job.update(kw)


def append_result(rec):
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(OUT_FILE, "a", encoding="utf-8") as f:
        f.write(rec + "\n")


def load_results():
    if not os.path.exists(OUT_FILE):
        return []
    out = []
    with open(OUT_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except Exception:
                    pass
    return out


def start_job():
    with _LOCK:
        for j in JOBS.values():
            if j["status"] == "running":
                return None, "Masih ada proses yang berjalan"
        job_id = uuid.uuid4().hex[:8]
        job = {
            "id": job_id,
            "status": "running",
            "step": "email",
            "email": "",
            "password": config.PASSWORD,
            "otp": "",
            "token": "",
            "error": "",
            "log": [],
            "created_at": "",
        }
        JOBS[job_id] = job
    threading.Thread(target=_run, args=(job,), daemon=True).start()
    return job_id, None


def current_job():
    with _LOCK:
        for j in JOBS.values():
            if j["status"] == "running":
                return j
        return None


def latest_job():
    with _LOCK:
        if _LAST_JOB is not None:
            return _LAST_JOB
        if JOBS:
            return list(JOBS.values())[-1]
        return None


def _run(job):
    vm = VoltMail()
    signup = None
    page = None
    try:
        _set(job, step="email")
        _log(job, "Membuat email VoltMail...")
        email, _, _ = vm.create_inbox()
        _set(job, email=email)
        _log(job, f"Email siap: {email}")

        _set(job, step="browser")
        from deepseek_signup import DeepSeekSignup
        signup = DeepSeekSignup()
        _log(job, "Menyiapkan Chrome (CDP)...")
        signup.launch_cdp()
        _log(job, "Membuka tab signup + logout dulu...")
        page = signup.open_signup_tab()
        _log(job, "Tab signup terbuka (sudah logout).")

        _set(job, step="form")
        _log(job, "Mengisi form email & password...")
        signup.fill_form(page, email, config.PASSWORD)
        _log(job, "Form terisi. Silakan klik 'Send Code' / selesaikan reCAPTCHA di Chrome.")
        _log(job, ">>> Bot menunggu OTP (tidak auto-klik Send Code). <<<")

        _set(job, step="otp")
        _log(job, f"Menunggu OTP (maks {config.OTP_TIMEOUT}s)...")
        deadline = time.time() + config.OTP_TIMEOUT
        otp = None
        while time.time() < deadline:
            otp = vm.check_otp_once()
            if otp:
                break
            time.sleep(3)
        if not otp:
            raise RuntimeError("OTP tidak diterima")
        _set(job, otp=otp)
        _log(job, f"OTP diterima: {otp}")
        _log(job, "Mengirim OTP otomatis...")
        signup.submit_otp(page, otp)
        _log(job, "OTP dikirim, menunggu registrasi...")

        _set(job, step="token")
        deadline = time.time() + config.TOKEN_TIMEOUT
        token = ""
        while time.time() < deadline:
            token = signup.read_token(page)
            if token:
                break
            time.sleep(1)
        if not token:
            raise RuntimeError("userToken tidak muncul")
        _set(job, token=token)
        _log(job, "userToken didapat!")

        rec = json.dumps({
            "email": email,
            "password": config.PASSWORD,
            "userToken": token,
            "status": "success",
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }, ensure_ascii=False)
        append_result(rec)
        _set(job, status="success")
        _log(job, "SELESAI. Hasil tersimpan.")
    except Exception as e:
        _set(job, status="failed", error=str(e))
        _log(job, f"GAGAL: {e}")
    finally:
        try:
            if signup and page:
                signup.close_tab(page)
        except Exception:
            pass
        try:
            if signup:
                signup.close()
        except Exception:
            pass
