
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

**iOS / iPadOS users** — End-to-end encrypted backup must be **disabled**. If it is enabled, the database inside the iPhone backup is encrypted in a way that cannot be read by this script. Disable it in WhatsApp before creating your device backup.

---

## 3. Android Setup

### Obtaining the Database

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
# Regular WhatsApp
adb pull /data/data/com.whatsapp/databases/msgstore.db

# WhatsApp Business
adb pull /data/data/com.whatsapp.w4b/databases/msgstore.db
```

No decryption needed.

**Non-rooted device** — pull and decrypt manually:

Step 1 — Pull the encrypted backup:

```bash
# Regular WhatsApp
adb pull /storage/emulated/0/Android/media/com.whatsapp/WhatsApp/Databases/msgstore.db.crypt15

# WhatsApp Business
adb pull /storage/emulated/0/Android/media/com.whatsapp.w4b/WhatsApp\ Business/Databases/msgstore.db.crypt15
```

Or browse to the respective folder manually.

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

### Obtaining WhatsApp Contacts

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

### Locating Your WhatsApp Media Folder

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

---

## 4. iOS / iPadOS Setup

WhatsApp on iOS stores its database and media inside an iPhone backup. The script reads the backup directly via `--ios_backup` — no third-party extraction tool required.

### Step 1 — Disable WhatsApp E2E encrypted backup

If End-to-end encrypted backup is enabled in WhatsApp, the backup cannot be read. Disable it before creating the backup:

```
Settings → Chats → Chat Backup → End-to-end Encrypted Backup → Turn Off
```

### Step 2 — Create a device backup

**macOS** — Connect your iPhone or iPad and open Finder. Select your device and click "Back Up Now". Choose an **unencrypted** backup.

**Windows** — Install [Apple Devices](https://apps.microsoft.com/detail/9NP83LWLPZ9K) from the Microsoft Store. Connect your device and create an **unencrypted** backup.

> ⚠️ **Encrypted backups are not supported.** If your backup is encrypted, the script will exit immediately with instructions to disable encryption and re-create the backup.

### Step 3 — Locate the backup directory

**macOS:**
```
~/Library/Application Support/MobileSync/Backup/<UDID>/
```

**Windows (iTunes):**
```
%AppData%\Apple\MobileSync\Backup\<UDID>\
```

**Windows (Apple Devices app):**
```
C:\Users\<Username>\Apple\MobileSync\Backup\<UDID>\
```

The backup directory is the folder that contains `Manifest.db`. Pass it to `--ios_backup`.

### Step 4 — Run the script

```bash
python3 wa_media_archiver.py \
  --ios_backup ~/Library/Application\ Support/MobileSync/Backup/<UDID> \
  --output /path/to/output \
  --dry-run
```

Contacts are automatically extracted from the backup. No `--contacts` or `--wa_root` needed.
