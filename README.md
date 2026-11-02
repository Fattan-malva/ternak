# 🤖 DeepSeek Auto Signup Bot

Bot otomatis untuk membuat akun DeepSeek menggunakan email sementara dari [VoltMail](https://voltmail.my.id) dan Chrome Remote Debugging Protocol (CDP).

> ⚠️ **Peringatan**: Proyek ini dibuat untuk tujuan edukasi/pribadi. Penggunaan yang melanggar ToS DeepSeek menjadi tanggung jawab pengguna.

---

## ✨ Fitur

- ✅ Generate email sementara otomatis via VoltMail
- ✅ Buka Chrome dengan profil terpisah + remote debugging
- ✅ Isi form signup DeepSeek (email & password) otomatis
- ✅ Deteksi dan submit OTP otomatis
- ✅ Ambil `userToken` dari localStorage dan simpan hasil
- ✅ UI web lokal di `http://127.0.0.1:8765/`
- ✅ Aman untuk di-clone: file sensitif (`config.json`, `chrome-bot-profile/`, `output/`) tidak ikut ke GitHub

---

## 📦 Requirements

- Windows 10/11
- Python 3.10+
- Google Chrome (terinstall)
- Koneksi internet

Install dependencies:

```bash
pip install -r requirements.txt
```

---

## 🚀 Cara Menjalankan

### 1. Clone repository

```bash
git clone https://github.com/username/deepseek-signup-bot.git
cd deepseek-signup-bot
```

### 2. Jalankan bot

```bash
py app.py
```

Saat pertama kali dijalankan, script otomatis membuat:

| File/Folder | Fungsi |
|-------------|--------|
| `config.json` | Konfigurasi password, timeout, port CDP, dll. |
| `chrome-bot-profile/` | Profil Chrome khusus bot (disalin dari profil asli kamu). |
| `output/results.jsonl` | Hasil akun yang berhasil dibuat. |

### 3. Buka UI di Chrome

Chrome akan terbuka otomatis di tab `http://127.0.0.1:8765/`.

### 4. Klik "Buat akun baru"

Bot akan:
1. Membuat email VoltMail
2. Membuka tab DeepSeek Sign Up
3. Mengisi email & password
4. **Kamu klik "Send Code"** dan selesaikan reCAPTCHA
5. Bot otomatis membaca OTP dari VoltMail
6. Bot submit OTP dan mengambil `userToken`

---

## ⚙️ Konfigurasi (`config.json`)

Setelah pertama kali run, file `config.json` akan dibuat otomatis:

```json
{
  "password": "passwordAcakOtomatis",
  "otp_timeout": 180,
  "token_timeout": 600,
  "delay_min": 7,
  "delay_max": 15,
  "cdp_port": 9222,
  "chrome_profile": "Default"
}
```

| Field | Keterangan |
|-------|------------|
| `password` | Password untuk akun DeepSeek baru |
| `otp_timeout` | Batas waktu menunggu OTP (detik) |
| `token_timeout` | Batas waktu menunggu token (detik) |
| `delay_min` | Jeda minimum antar request (detik) |
| `delay_max` | Jeda maksimum antar request (detik) |
| `cdp_port` | Port remote debugging Chrome |
| `chrome_profile` | Nama profil Chrome (default: `Default`) |

---

## 🔒 Keamanan & Git

File/folder berikut **sengaja di-ignore** agar aman saat di-push ke GitHub:

```gitignore
config.json
chrome-bot-profile/
output/
```

Alasannya:
- `config.json` → berisi password & konfigurasi pribadi
- `chrome-bot-profile/` → berisi cookie, session, fingerprint browser
- `output/` → berisi `userToken` akun yang berhasil dibuat

> 💡 Saat clone di komputer lain, cukup jalankan `py app.py`, ketiga file/folder di atas akan dibuat ulang otomatis.

---

## 🛠 Troubleshooting

### Chrome CDP tidak aktif / stuck

1. Pastikan semua jendela Chrome tertutup.
2. Coba jalankan ulang:
   ```bash
   py app.py
   ```
3. Jika masih gagal, cek apakah port 9222 dipakai aplikasi lain:
   ```bash
   netstat -ano | findstr ":9222"
   ```

### Status: Gagal: Permintaan gagal (404)

Ini terjadi kalau proses Python lama masih berjalan dengan versi lama. Matikan dulu:

```bash
taskkill /F /IM python.exe /T
taskkill /F /IM chrome.exe /T
py app.py
```

### OTP tidak masuk

- Pastikan kamu sudah klik **"Send Code"** di halaman DeepSeek.
- Selesaikan reCAPTCHA kalau muncul.
- Cek apakah inbox VoltMail aktif dengan membuka email yang dibuat bot.

---

## 📁 Struktur Folder

```
deepseek-signup-bot/
├── app.py                  # Launcher UI + Chrome
├── auto_runner.py          # Logic job pembuatan akun
├── deepseek_signup.py      # Interaksi dengan Chrome/CDP
├── voltmail.py             # API VoltMail
├── config.py               # Baca config + bootstrap folder/profile
├── config.example.json     # Template config (boleh di-commit)
├── requirements.txt        # Dependencies
├── .gitignore              # Daftar file yang di-ignore
├── README.md               # File ini
├── chrome-bot-profile/     # Auto-create, tidak di-commit
└── output/                 # Auto-create, tidak di-commit
```

---

## 📄 License

MIT License — bebas dipakai dan dimodifikasi dengan risiko tanggung sendiri.
