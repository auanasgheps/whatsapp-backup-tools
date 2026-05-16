# WA Media Archiver — Windows Companion Script

> The main archiver (`wa_media_archiver.py`) runs on Linux and macOS. This companion script handles the extraction step on Windows so you can transfer the files to your Linux or macOS machine and proceed normally.

---

## Prerequisites

- [Android Platform Tools](https://developer.android.com/tools/releases/platform-tools) (ADB) installed and in PATH
- USB Debugging enabled on your Android device, and the device authorized on this computer
- Python 3.x on Windows — only required if decrypting on Windows with `-DecryptDB`. Optional, but recommended.

---

## What the Script Does

| Step | Action |
|---|---|
| Prerequisites | Checks ADB is installed and a device is connected |
| Database | Pulls `msgstore.db.crypt15` from the phone |
| Contacts | Pulls and filters WhatsApp contacts via ADB content provider |
| Decryption | Optional — decrypts on Windows if `-DecryptDB` and `-E2EKey` are provided |
| Summary | Prints the exact `wa_media_archiver.py` command to run on Linux or macOS |

---

## Usage

```powershell
# Pull only — decrypt on Linux
.\windows_extractor_companion.ps1

# Pull to a custom output folder
.\windows_extractor_companion.ps1 -OutputDir "C:\Users\You\Desktop\wa_backup"

# Pull and decrypt on Windows
.\windows_extractor_companion.ps1 -DecryptDB -E2EKey "your_cryptographic_key"
```

> ⚠️ PowerShell uses single-dash flags: `-DecryptDB`, not `--DecryptDB`.

| Parameter | Required | Description |
|---|---|---|
| `-OutputDir` | No | Folder for pulled files. Defaults to `.\wa_pull` |
| `-DecryptDB` | No | Decrypt the pulled `.crypt15` file on Windows |
| `-E2EKey` | If `-DecryptDB` | Your cryptographic key for decryption |

---

## Next Steps

After the script completes:

1. Transfer the output folder to your Linux or macOS machine
2. Transfer your WhatsApp Media folder to Linux or macOS as well
3. Run `wa_media_archiver.py` on Linux or macOS — the script prints the exact command at the end
