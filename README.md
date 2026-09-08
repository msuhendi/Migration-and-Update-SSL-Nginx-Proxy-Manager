# NPM Infrastructure Migration

Tooling Python untuk memigrasikan konfigurasi **Proxy Hosts** dari satu
instance [Nginx Proxy Manager (NPM)](https://nginxproxymanager.com/) ke
instance lain, lalu memasang sertifikat SSL pada domain yang dipilih.

## Fitur

- Secret dibaca dari environment variable atau argumen CLI, bukan dari source
  code.
- `requests.Session` dengan timeout dan retry untuk error transient.
- Mode `--dry-run` sebelum perubahan diterapkan.
- Migrasi idempotent dengan mode `skip`, `update`, dan `overwrite`.
- Backup JSON Proxy Hosts dari server sumber.
- Pencocokan domain exact atau subdomain yang ketat, bukan substring bebas.
- Migrasi best-effort Access Lists dan Certificates dengan pemetaan ID.
- Logging JSON ke console dan file, ringkasan hasil, serta exit code non-zero
  saat ada kegagalan.
- Audit report JSON untuk setiap eksekusi, termasuk hasil per-host dan error.
- Test unit offline tanpa perlu menjalankan NPM.

## Struktur Project

| File | Fungsi |
| --- | --- |
| `npm_migration.py` | Client API, payload builder, migrasi, backup, matching domain, dan logging. |
| `migrate_npm.py` | CLI migrasi Proxy Hosts dari Server A ke Server B. |
| `update_ssl_server_b.py` | CLI pemasangan sertifikat SSL ke host yang cocok. |
| `tests/test_npm_migration.py` | Test unit untuk perilaku penting tanpa koneksi jaringan. |
| `.env.example` | Template konfigurasi; salin menjadi `.env` atau export manual. |
| `req.txt` | Dependensi runtime Python. |
| `LICENSE` | Lisensi MIT project. |

## Prasyarat

- Python 3.9 atau lebih baru.
- Dua instance NPM yang dapat diakses dari mesin eksekusi.
- Akun yang dapat membaca resource di Server A dan membuat atau memperbarui
  resource di Server B.
- Sertifikat wildcard sudah tersedia di Server B jika akan menjalankan update
  SSL.

Port administrasi NPM biasanya `81`, tetapi sesuaikan URL dengan instalasi Anda.
HTTPS lebih disarankan. Opsi `--insecure` hanya untuk lingkungan internal yang
memang menggunakan sertifikat tidak trusted.

## Instalasi

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r req.txt
```

Salin template konfigurasi, lalu isi nilainya:

```bash
cp .env.example .env
set -a
source .env
set +a
```

File `.env` sudah dikecualikan oleh `.gitignore`. Jangan memasukkan password
atau token asli ke repository.

## Audit Report

Setiap proses membuat satu report JSON secara otomatis:

- migrasi: `reports/migration-<timestamp>.json`;
- update SSL: `reports/ssl-update-<timestamp>.json`.

Lokasi dapat diubah dengan `NPM_REPORT_DIR` atau file tertentu dapat dipakai
dengan `--report-file`:

```bash
python migrate_npm.py --dry-run --report-file reports/audit-migrasi-001.json
python update_ssl_server_b.py --dry-run --report-file reports/audit-ssl-001.json
```

Report berisi `schema_version`, waktu mulai dan selesai, status (`success`,
`failed`, atau `dry-run-*`), context operasi, ringkasan jumlah proses, detail
per-host pada `items`, backup path, dan daftar error. Password/token tidak
ditulis ke report. Report tetap dibuat jika login atau request awal gagal,
sehingga setiap percobaan memiliki jejak audit.

## Migrasi Proxy Hosts

### Dry-run terlebih dahulu

```bash
python migrate_npm.py \
  --source-url "$NPM_SOURCE_URL" \
  --source-user "$NPM_SOURCE_USER" \
  --source-password "$NPM_SOURCE_PASSWORD" \
  --destination-url "$NPM_DESTINATION_URL" \
  --destination-user "$NPM_DESTINATION_USER" \
  --destination-password "$NPM_DESTINATION_PASSWORD" \
  --dry-run
```

Environment variable dapat digunakan sebagai pengganti argumen:

```bash
python migrate_npm.py --dry-run
```

Dry-run tetap mengambil data dan membuat backup JSON, tetapi tidak melakukan
POST, PUT, atau DELETE ke Server B.

### Mode idempotensi

```bash
python migrate_npm.py --mode skip
python migrate_npm.py --mode update
python migrate_npm.py --mode overwrite
```

- `skip`: lewati host yang domain set-nya sudah ada di Server B. Ini default dan
  paling aman untuk percobaan pertama.
- `update`: perbarui host yang sudah ada dengan payload dari Server A.
- `overwrite`: hapus host tujuan yang cocok lalu buat ulang. Gunakan hanya jika
  penghapusan resource memang diinginkan.

Setiap eksekusi menyimpan backup ke `backups/server-a-<timestamp>.json` atau
folder yang diberikan melalui `--backup-dir`.

### Migrasi resource terkait

Tambahkan opsi berikut untuk mencoba memigrasikan Access Lists dan Certificates
sebelum Proxy Hosts:

```bash
python migrate_npm.py --include-resources --mode update
```

Tool memetakan ID lama ke ID baru berdasarkan `nice_name` atau domain. API NPM
tidak selalu mengembalikan material private key sertifikat, sehingga migrasi
Certificate dapat gagal atau hanya berlaku pada resource yang didukung versi
NPM Anda. Error dicatat tanpa menghentikan host lain.

## Update SSL

Setelah Proxy Hosts tersedia dan ID sertifikat diketahui di Server B:

```bash
python update_ssl_server_b.py \
  --server-url "$NPM_SERVER_URL" \
  --user "$NPM_USER" \
  --password "$NPM_PASSWORD" \
  --certificate-id "$NPM_SSL_CERTIFICATE_ID" \
  --target-domain "$NPM_TARGET_DOMAIN" \
  --dry-run
```

Hapus `--dry-run` setelah hasil target diverifikasi. Secara default, target
mencakup domain persis dan subdomainnya, misalnya `example.com` serta
`app.example.com`, tetapi tidak `notexample.com`. Gunakan `--exact-only` untuk
hanya memperbarui domain persis.

### Pilihan HTTP/2 dan `ssl_forced`

Keduanya aktif secara default agar sesuai dengan perilaku sebelumnya. Atur
masing-masing secara independen melalui CLI:

```bash
# Aktifkan keduanya
python update_ssl_server_b.py --http2 --ssl-forced

# Matikan HTTP/2, tetap paksa HTTPS
python update_ssl_server_b.py --no-http2 --ssl-forced

# Aktifkan HTTP/2, tetapi jangan redirect HTTP ke HTTPS
python update_ssl_server_b.py --http2 --no-ssl-forced

# Matikan keduanya
python update_ssl_server_b.py --no-http2 --no-ssl-forced
```

Pilihan yang sama dapat disimpan di environment:

```bash
NPM_ENABLE_HTTP2=false
NPM_SSL_FORCED=false
```

Nilai environment yang diterima: `true`, `false`, `1`, `0`, `yes`, `no`,
`on`, dan `off`. Flag CLI memiliki prioritas di atas nilai environment.

Perubahan SSL yang diterapkan:

- memasang `certificate_id` yang diberikan;
- mengaktifkan atau menonaktifkan `ssl_forced` sesuai opsi;
- mengaktifkan atau menonaktifkan HTTP/2 sesuai opsi;
- mempertahankan konfigurasi forwarding dan opsi host lain yang didukung.

## Opsi CLI Penting

| Opsi | Script | Keterangan |
| --- | --- | --- |
| `--dry-run` | Keduanya | Simulasikan perubahan tanpa POST, PUT, atau DELETE. |
| `--timeout SECONDS` | Keduanya | Batas waktu setiap request; default `30`. |
| `--retries COUNT` | Keduanya | Retry untuk 429, 5xx, dan error koneksi; default `3`. |
| `--insecure` | Keduanya | Matikan verifikasi TLS; gunakan hanya bila perlu. |
| `--log-file PATH` | Keduanya | Simpan log JSONL ke file. |
| `--include-resources` | Migrasi | Migrasikan Access Lists dan Certificates. |
| `--mode MODE` | Migrasi | `skip`, `update`, atau `overwrite`. |
| `--exact-only` | SSL | Jangan sertakan subdomain. |

Kedua CLI mengembalikan exit code `1` jika login, request, atau salah satu item
gagal. Ini membuatnya dapat digunakan dalam cron, CI, atau pipeline deployment.

## Verifikasi

Jalankan test offline dan pemeriksaan sintaks:

```bash
python -m unittest discover -s tests -v
python -m py_compile npm_migration.py migrate_npm.py update_ssl_server_b.py
```

Setelah migrasi nyata, periksa jumlah host, domain forwarding, duplikasi,
sertifikat, redirect HTTPS, WebSocket, HTTP/2, access list, serta log NPM.

Contoh pemeriksaan endpoint publik:

```bash
curl -I http://example.com
curl -I https://example.com
```

## Resume, Rollback, dan Operasi Produksi

Tool ini mendukung resume secara praktis melalui mode idempotent: jalankan ulang
dengan `--mode skip` untuk melewati host yang sudah ada, atau `--mode update`
untuk menyamakan konfigurasi yang belum selesai. Backup JSON dapat dipakai
sebagai sumber audit atau input untuk tool pemulihan lanjutan.

Rollback otomatis belum dilakukan karena penghapusan dan pembuatan ulang
resource NPM bersifat version-dependent. Untuk operasi besar:

1. gunakan staging NPM terlebih dahulu;
2. simpan backup JSON dan snapshot/backup database NPM;
3. mulai dari `--dry-run` dan `--mode skip`;
4. simpan log file dari setiap eksekusi;
5. verifikasi sebagian host sebelum melanjutkan seluruh migrasi.

## Batasan dan Catatan Keamanan

- Endpoint Certificate NPM dapat membutuhkan data file certificate/private key
  yang tidak tersedia dari response list API; fitur resource migration bersifat
  best-effort.
- Resource yang tidak berhasil dibuat dicatat sebagai error dan menyebabkan
  exit code non-zero.
- `--insecure` menonaktifkan validasi TLS dan tidak boleh digunakan pada jalur
  jaringan yang tidak tepercaya.
- `overwrite` melakukan DELETE pada host tujuan sebelum POST ulang.
- Jangan menampilkan password pada command history jika shell Anda menyimpan
  history; environment variable atau secret manager lebih aman.

## Lisensi

Project ini dirilis di bawah [MIT License](LICENSE). Copyright (c) 2026
msuhendi.