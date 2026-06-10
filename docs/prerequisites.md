
# Prerequisites

## 1. Python Environment

Ensure Python 3.11+ is installed.

This script relies on some packages for Android and iOS.

Install the relevant package before your first run. If you miss any of these, the script will notify you.



**Android encrypted backup decryption** 

The script uses `wa-crypt-tools`. You are going to use it if you don't have a rooted device.

```bash
pip install wa-crypt-tools
```

**iOS encrypted backup decryption** 

The script uses `iphone-backup-decrypt`. You are going to use it if you create password protected iOS backups.

```bash
pip install iphone-backup-decrypt
```

> 💡 Windows Users: your Python environment may not have pip in the PATH. If `pip install` doesn't work for you, `use py -m pip install`.


**Timezone feature for Windows**

If you plan to use the `--timezone` argument, Windows requires one additional package. macOS and Linux ship the IANA timezone database as part of the OS; Windows does not.
> ```
> pip install tzdata
> ```
> This is only needed when `--timezone` is used. Normal runs without `--timezone` work on Windows without it.


---

## 2. WhatsApp E2E Encrypted Backup

**Android users** — Before pulling the database from a non-rooted device, **End-to-end encrypted backup must be enabled** in WhatsApp. This is what generates the cryptographic key required for decryption.

On your phone, navigate to:

```
Settings → Chats → Chat Backup → End-to-end Encrypted Backup → Turn On → Use 64-digit encryption key instead
```

Enable it and note the **cryptographic key** — a long alphanumeric string. 
**Store it somewhere safe**; you will need it every time you pull a fresh backup. If you lose this key, you will lose access to all your backups.

When asked, create the end-to-end ecrypted backup.

> If this step is skipped, the `.crypt15` backup file cannot be decrypted and the script will not work on a non-rooted device.

**iOS / iPadOS users** — End-to-end encrypted backup must be **disabled**. If it is enabled, the database inside the iPhone backup is encrypted in a way that cannot be read by this script. Disable it in WhatsApp before creating your device backup.

---

## 3. Android Setup


### Your WhatsApp Media Folder

Before you run the script, you need your WhatsApp (Media) folder on disk - actually, the folder called `WhatsApp` that **contains** the `Media` subfolder. 
You need to copy this folder from your phone: you can use [Syncthing](https://syncthing.net/) to smooth the process.

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

### Obtaining the Database

#### Recommended — Automatic Retrieval (`--mode adb`)

This is the simplest approach for most users. Connect your phone via USB with USB Debugging enabled, then let the script handle the pull and decryption automatically.

> 💡 **ADB required.** Install it before running `--mode adb`:
> - **Linux**: `sudo apt install adb` (Debian/Ubuntu) or `sudo dnf install android-tools` (Fedora)
> - **macOS**: `brew install android-platform-tools` (requires [Homebrew](https://brew.sh))
> - **Windows**: Download [Android Platform Tools](https://developer.android.com/tools/releases/platform-tools) and add the folder to your PATH

Run the script with `--mode adb`:

```bash
python3 wa_media_archiver.py \
  --mode adb \
  --e2e 1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a6b7c8d9e0f1a2b \
  --wa_root /path/to/WhatsApp/storage \
  --output /path/to/output
```
> 💡 On Windows, replace `\` with `` ` `` (PowerShell) or `^` (cmd.exe).

The script pulls the encrypted backup and contacts directly from the device and decrypts on the fly. No separate steps needed.

> Please note that this is just a simple example: you can add many options to customize the run, please see [docs/running-the-script.md](docs/running-the-script.md).

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
  --e2e 1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a6b7c8d9e0f1a2b \
  --wa_root /path/to/WhatsApp/storage \
  --output /path/to/output
```

> ⚠️ If `--msgstore` points to a `.crypt15` file and `--e2e` is not provided, the script will exit with an error.

#### Obtaining WhatsApp Contacts

> This step is only required for manual pull - When using automatic retrieval (`--mode adb`), contacts are pulled automatically.

Contacts are optional but strongly recommended — without them, folder names will show raw phone numbers instead of contact names.

For manual pull:

```bash
adb shell content query \
  --uri content://com.android.contacts/data \
  --projection display_name:data1 \
  | grep @s.whatsapp.net > wa_contacts
```

Pass the file to the script with `--contacts`.

> 💡 You only need to repeat this if your contacts have changed significantly since the last run.


---

## 4. iOS / iPadOS Setup

WhatsApp on iOS stores its database and media inside an iPhone backup. The script reads the backup directly via `--ios_backup`.

### Encrypted vs. unencrypted backups

Both encrypted and unencrypted iPhone backups are supported.

- **Unencrypted backup** — no extra argument needed.
- **Encrypted backup** — pass `--ios_password <password>` (the password you set in iTunes/Finder/Apple Devices when enabling backup encryption). Make sure `iphone-backup-decrypt` is installed first (`pip install iphone-backup-decrypt`).

> **WhatsApp E2E encrypted backup** (a separate WhatsApp setting) must still be **disabled** before creating the iPhone backup. This is a WhatsApp-level encryption applied on top of the database — the script cannot bypass it.
> Disable it in WhatsApp: `Settings → Chats → Chat Backup → End-to-end Encrypted Backup → Turn Off`

### Step 1 — Create a device backup

**macOS** — Connect your iPhone or iPad and open Finder. Select your device and click "Back Up Now". You may choose encrypted or unencrypted.

**Windows** — Install [Apple Devices](https://apps.microsoft.com/detail/9NP83LWLPZ9K) from the Microsoft Store. Connect your device and create a backup.

### Step 2 — Locate the backup directory

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

### Step 3 — Run the script

Unencrypted backup:

```bash
python3 wa_media_archiver.py \
  --ios_backup ~/Library/Application\ Support/MobileSync/Backup/<UDID> \
  --output /path/to/output \
  --dry-run
```
> 💡 On Windows, replace `\` with `` ` `` (PowerShell) or `^` (cmd.exe).

Encrypted backup:

```bash
python3 wa_media_archiver.py \
  --ios_backup ~/Library/Application\ Support/MobileSync/Backup/<UDID> \
  --ios_password your_backup_password \
  --output /path/to/output \
  --dry-run
```
> 💡 On Windows, replace `\` with `` ` `` (PowerShell) or `^` (cmd.exe).

Contacts are automatically extracted from the backup.