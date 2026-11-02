"""Konfigurasi & bootstrap otomatis.

Semua file/folder sensitif (config.json, chrome-bot-profile/, output/) TIDAK
di-commit ke git. Saat `py app.py` pertama kali dijalankan, modul ini otomatis:

1. Membuat `config.json` (password di-generate acak kalau belum ada).
2. Membuat folder `output/`.
3. Membuat folder `chrome-bot-profile/` + menyalin profil Chrome asli
   (cookies/fingerprint) sekali saja, karena Chrome 136+ menolak
   `--remote-debugging-port` pada User Data directory default.
"""

import json
import os
import secrets
import shutil

HERE = os.path.dirname(os.path.abspath(__file__))
CFG_PATH = os.path.join(HERE, "config.json")

# Dedicated (non-default) Chrome profile dir untuk bot.
BOT_PROFILE_DIR = os.path.join(HERE, "chrome-bot-profile")
OUTPUT_DIR = os.path.join(HERE, "output")

# Folder cache berat yang tidak perlu ikut disalin dari profil asli.
_SKIP_COPY = (
    "Cache", "Code Cache", "GPUCache", "Service Worker",
    "Session Storage", "blob_storage", "File System",
    "DawnGraphiteCache", "DawnWebGPUCache", "GraphiteDawnCache",
)


def default_config():
    """Nilai default untuk config.json baru."""
    return {
        "password": secrets.token_urlsafe(12),
        "otp_timeout": 180,
        "token_timeout": 600,
        "delay_min": 7,
        "delay_max": 15,
        "cdp_port": 9222,
        "chrome_profile": "Default",
    }


def _write_config(cfg):
    os.makedirs(HERE, exist_ok=True)
    with open(CFG_PATH, "w", encoding="utf-8") as file:
        json.dump(cfg, file, indent=2, ensure_ascii=False)
        file.write("\n")


def _load_raw_config():
    """Baca config.json. Kalau rusak, backup lalu kembalikan None."""
    try:
        with open(CFG_PATH, encoding="utf-8") as file:
            data = json.load(file)
        if not isinstance(data, dict):
            raise ValueError("isi config.json bukan object JSON")
        return data
    except FileNotFoundError:
        return None
    except Exception as error:
        print(f"[config] config.json tidak valid ({error}) -> dibuat ulang.")
        try:
            os.replace(CFG_PATH, CFG_PATH + ".bak")
        except Exception:
            pass
        return None


def ensure_config_file():
    """Buat config.json otomatis kalau belum ada. Kembalikan path-nya."""
    data = _load_raw_config()
    if data is None:
        data = default_config()
        _write_config(data)
        print(f"[config] config.json dibuat otomatis: {CFG_PATH}")
        print(f"[config] Password default (acak): {data['password']}")
        print("[config] Ubah password di config.json kalau perlu.")
    return CFG_PATH


def ensure_output_dir():
    """Buat folder output/ kalau belum ada."""
    if not os.path.isdir(OUTPUT_DIR):
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        print(f"[config] Folder output dibuat: {OUTPUT_DIR}")
    return OUTPUT_DIR


def ensure_bot_profile():
    """Pastikan chrome-bot-profile/ ada; salin profil Chrome asli sekali saja."""
    has_default = os.path.isdir(os.path.join(BOT_PROFILE_DIR, "Default"))
    if has_default:
        return BOT_PROFILE_DIR

    os.makedirs(BOT_PROFILE_DIR, exist_ok=True)
    print(f"[config] Menyiapkan chrome-bot-profile: {BOT_PROFILE_DIR}")

    real_dir = os.path.join(
        os.environ.get("LOCALAPPDATA", ""), "Google", "Chrome", "User Data"
    )
    if not os.path.isdir(real_dir):
        print("[config] Profil Chrome asli tidak ditemukan -> pakai profil baru.")
        return BOT_PROFILE_DIR

    # 1) Salin "Local State" (berisi kunci enkripsi cookie profil).
    local_state = os.path.join(real_dir, "Local State")
    if os.path.isfile(local_state):
        try:
            shutil.copy2(local_state, os.path.join(BOT_PROFILE_DIR, "Local State"))
        except Exception as error:
            print(f"[config] Gagal salin Local State: {error}")

    # 2) Salin folder "Default" (lewati cache berat).
    real_default = os.path.join(real_dir, "Default")
    bot_default = os.path.join(BOT_PROFILE_DIR, "Default")
    if os.path.isdir(real_default) and not os.path.isdir(bot_default):
        try:
            shutil.copytree(
                real_default,
                bot_default,
                dirs_exist_ok=True,
                ignore=shutil.ignore_patterns(*_SKIP_COPY),
            )
            print("[config] Profil Chrome asli berhasil disalin (cookies ikut).")
        except Exception as error:
            print(f"[config] Gagal salin profil asli: {error}")
    return BOT_PROFILE_DIR


# ---------------------------------------------------------------------------
# Bootstrap saat modul di-import (mis. dari `py app.py`)
# ---------------------------------------------------------------------------

ensure_config_file()
ensure_output_dir()

_cfg = dict(default_config())
_cfg.update(_load_raw_config() or {})

PASSWORD = _cfg["password"]
OTP_TIMEOUT = int(_cfg["otp_timeout"])
TOKEN_TIMEOUT = int(_cfg["token_timeout"])
DELAY_MIN = float(_cfg["delay_min"])
DELAY_MAX = float(_cfg["delay_max"])

# Folder profil Chrome yang dipakai bot (auto-create saat launch).
REAL_CHROME_PROFILE = _cfg.get("real_chrome_profile") or BOT_PROFILE_DIR

# Nama profil di dalam User Data (contoh: "Default", "Profile 1").
CHROME_PROFILE = _cfg.get("chrome_profile") or None
CDP_PORT = _cfg.get("cdp_port")
