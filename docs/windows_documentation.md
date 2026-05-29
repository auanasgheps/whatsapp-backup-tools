# WA Media Archiver — Windows Guide

> `wa_media_archiver.py` runs natively on Windows. No companion script is needed.

---

## Prerequisites

- Python 3.10+
- [Android Platform Tools](https://developer.android.com/tools/releases/platform-tools) (ADB) installed and in PATH
- USB Debugging enabled on your Android device, and the device authorized on this computer

---

## Running on Windows

Use the same `--mode adb` workflow as on Linux or macOS. The script handles pulling the database and contacts from the device, decrypting, and archiving in one step:

```powershell
python wa_media_archiver.py --mode adb --e2e_key "your_key" --wa_root "C:\path\to\WhatsApp" --output "C:\path\to\archive"
```

For WhatsApp Business:

```powershell
python wa_media_archiver.py --mode adb --business --e2e_key "your_key" --wa_root "C:\path\to\WhatsApp Business" --output "C:\path\to\archive"
```

The `--wa_root` folder is your WhatsApp media folder (the one containing the `Media/` subfolder). You can copy this from the device in advance, or mount it directly if your device is accessible as a drive.

---

## What happens when you run `--mode adb`

| Step | Action |
|---|---|
| ADB check | Verifies `adb` is on PATH and a device is connected |
| Database | Pulls `msgstore.db.crypt15` from the phone into a temp folder |
| Contacts | Pulls and filters WhatsApp contacts via ADB content provider |
| Decryption | Decrypts the database using the provided `--e2e_key` |
| Archive | Copies media into the structured output folder |

---

## Network shares

If your output folder or `--wa_root` is on a network share (e.g. `\\server\share\...`), the script will warn you. Network shares may cause slower performance and may not preserve file timestamps correctly. A local path is recommended when possible.
