
# Prerequisites

## 1. Python Environment

Ensure Python 3.10+ is installed.

For encrypted backup decryption, the script uses `wa-crypt-tools`. It is installed automatically the first time it is needed — no manual step required.

---

## 2. WhatsApp E2E Encrypted Backup

**Android users** — Before pulling the database from a non-rooted device, **End-to-end encrypted backup must be enabled** in WhatsApp. This is what generates the cryptographic key required for decryption.

On your phone, navigate to:

```
Settings → Chats → Chat Backup → End-to-end Encrypted Backup
```

Enable it and note the **cryptographic key** — a long alphanumeric string. This is **not** a password. Store it somewhere safe; you will need it every time you pull a fresh backup.

> If this step is skipped, the `.crypt15` backup file cannot be decrypted and the script will not work on a non-rooted device.

**iOS / iPadOS users** — End-to-end encrypted backup must be **disabled**. If it is enabled, the database inside the iTunes/Finder backup is encrypted in a way that cannot be decrypted by this script. Disable it in WhatsApp before creating your device backup.

---

## 3. Obtaining the WhatsApp Database

### Android

#### Recommended — Automatic Retrieval (Linux, macOS and Windows)

The simplest approach for most users. Connect your phone via USB with USB Debugging enabled, then let the script (or the companion script on Windows) handle the pull and decryption automatically.

> 💡 **ADB required on all platforms.** Install it before running `--mode adb`:
> - **Linux**: `sudo apt install adb` (Debian/Ubuntu) or `sudo dnf install android-tools` (Fedora)
> - **macOS**: `brew install android-platform-tools` (requires [Homebrew](https://brew.sh))

**On Linux and macOS** — pass `--mode adb` when running the script:

```bash
python3 wa_media_archiver.py \
  --mode adb \
  --e2e your_cryptographic_key \
  --wa_root /path/to/WhatsApp \
  --output /path/to/output
```

The script pulls the encrypted backup and contacts directly from the device and decrypts on the fly. No separate steps needed.

**On Windows** — use the companion script, then transfer to Linux or macOS:

```powershell
.\windows_extractor_companion.ps1 -DecryptDB -E2EKey "your_cryptographic_key"
```

See [windows_documentation.md](windows_documentation.md) for full details.

---

#### Alternative — Manual Pull

Use this if you prefer to extract and decrypt the database yourself, or if automatic retrieval is not an option.

**Rooted device** — pull the unencrypted database directly:

```bash
adb pull /data/data/com.whatsapp/databases/msgstore.db
```

No decryption needed. Skip to [Obtaining WhatsApp Contacts](#4-obtaining-whatsapp-contacts).

**Non-rooted device** — pull and decrypt manually:

Step 1 — Pull the encrypted backup:

```bash
adb pull /storage/emulated/0/Android/media/com.whatsapp/WhatsApp/Databases/msgstore.db.crypt15
```

Or browse to it manually at:

```
Android/media/com.whatsapp/WhatsApp/Databases/msgstore.db.crypt15
```

Step 2 — Decrypt:

```bash
wadecrypt your_key msgstore.db.crypt15 msgstore.db
```

> ⚠️ Repeat these two steps every time you want to update your archive with new media.

Alternatively, skip the manual decrypt and let the script handle it by passing the `.crypt15` file directly:

```bash
python3 wa_media_archiver.py \
  --msgstore /path/to/msgstore.db.crypt15 \
  --e2e your_cryptographic_key \
  --wa_root /path/to/WhatsApp \
  --output /path/to/output
```

> ⚠️ If `--msgstore` points to a `.crypt15` file and `--e2e` is not provided, the script will exit with an error.

---

### iOS / iPadOS ⚠️ Work in Progress

> ⚠️ **This path has not been tested with this script.** The steps below are based on the [WhatsApp-Chat-Exporter](https://github.com/KnugiHK/WhatsApp-Chat-Exporter) documentation by KnugiHK. Whether the extracted database is compatible with `wa_media_archiver.py` — and how media files should be provided — has not yet been verified. Contributions and test reports are welcome.

WhatsApp on iOS stores its database inside an iTunes (Windows) or Finder (macOS) device backup. ADB is not involved.

**Step 1 — Create a device backup**

Connect your iPhone or iPad and create an unencrypted backup:
- **Windows**: iTunes → device summary → Back Up Now. Ensure "Encrypt local backup" is **off**.
- **macOS**: Finder → device → General → Back up all data on your iPhone to this Mac. Ensure encryption is **off**.

If your backup is encrypted, you must either turn off backup encryption in iTunes/Finder, or install the `iphone_backup_decrypt` package to decrypt it in place:

```bash
pip install git+https://github.com/KnugiHK/iphone_backup_decrypt
```

**Step 2 — Locate the backup folder**

iTunes and Finder store backups in a fixed location:

- **Windows**: `C:\Users\<Username>\AppData\Roaming\Apple Computer\MobileSync\Backup\<device id>\`
- **macOS**: `~/Library/Application Support/MobileSync/Backup/<device id>/`

`<device id>` is a long hex string identifying your device. If you have multiple backups, each has its own folder.

**Step 3 — Extract the WhatsApp database**

Inside the backup folder, files are stored under hashed names rather than their original paths. The WhatsApp database is always stored as:

```
7c7fba66680ef796b916b067077cc246adacf01d
```

Copy this file and rename it to `msgstore.db`. This is the file you pass to `--msgstore`.

**Step 4 — Media files**

How WhatsApp media is stored in iTunes/Finder backups, and how to provide it to `--wa_root`, is not yet documented. This is the main open question for iOS support.

---

## 4. Obtaining WhatsApp Contacts

Contacts are optional but strongly recommended — without them, folder names will show raw phone numbers instead of contact names.

When using automatic retrieval (`--mode adb` on Linux and macOS, or the Windows companion script), contacts are pulled automatically. No extra steps needed.

For manual pull on Linux and macOS:

```bash
adb shell content query \
  --uri content://com.android.contacts/data \
  --projection display_name:data1 \
  | grep @s.whatsapp.net > wa_contacts
```

Pass the file to the script with `--contacts`.

> 💡 You only need to repeat this if your contacts have changed significantly since the last run.

---

## 5. Locating Your WhatsApp Media Folder

The script needs the root of your WhatsApp folder on disk — the folder that **contains** the `Media/` subfolder.

```
/path/to/WhatsApp/
├── Databases/
└── Media/
    ├── WhatsApp Images/
    ├── WhatsApp Video/
    └── WhatsApp Audio/
```

Pass the path to the `WhatsApp/` folder (not `Media/`) to the script via `--wa_root`.

> ⚠️ **Run the script on the same machine where the media files are physically stored.** Processing files over a network share (NFS, SMB, etc.) will be significantly slower — the script hashes and copies every file in the archive. Running locally is strongly recommended.
