# Android Setup

## WhatsApp E2E Encrypted Backup

Before pulling the database from a non-rooted device, **End-to-end encrypted backup must be enabled** in WhatsApp. This generates the cryptographic key required for decryption.

On your phone, navigate to:

```
Settings → Chats → Chat Backup → End-to-end Encrypted Backup → Turn On → Use 64-digit encryption key instead
```

Enable it and note the **cryptographic key** — a long alphanumeric string.
**Store it somewhere safe**; you will need it every time you pull a fresh backup. If you lose this key, you will lose access to all your backups.

When asked, create the end-to-end encrypted backup.

> If this step is skipped, the `.crypt15` backup file cannot be decrypted and the archiver will not work on a non-rooted device.

---

## Your WhatsApp Media Folder

Before running the archiver, copy the `WhatsApp/` folder (the one **containing** the `Media` subfolder) from your phone to your machine. [Syncthing](https://syncthing.net/) makes this easy.

```
/path/to/WhatsApp/
├── Databases/
└── Media/
    ├── WhatsApp Images/
    ├── WhatsApp Video/
    └── WhatsApp Audio/
```

Pass the path to the `WhatsApp/` folder (not `Media/`) to the archiver via `--wa_root`.

If your media is spread across multiple locations (e.g. an old backup folder plus your current phone's `WhatsApp/` folder), repeat `--wa_root` for each source. The archiver searches all roots and selects the best available copy of each file automatically.

> ⚠️ **Run the archiver on the same machine where the media files are physically stored.** Processing files over a network share (NFS, SMB, etc.) will be significantly slower — the archiver hashes and copies every file.

---

## Obtaining the Database

### Recommended — Automatic Retrieval (`--mode adb`)

This is the simplest approach for most users. Connect your phone via USB with USB Debugging enabled, then let the archiver handle the pull and decryption automatically.

**ADB is required.** Install it before running this mode:

- **Linux**: `sudo apt install adb` (Debian/Ubuntu) or `sudo dnf install android-tools` (Fedora)
- **macOS**: `brew install android-platform-tools` (requires [Homebrew](https://brew.sh))
- **Windows**: Install via:
    - Winget (`winget install Google.PlatformTools`)
    - Scoop (`scoop install main/adb`)
    - Manual install [Android Platform Tools](https://developer.android.com/tools/releases/platform-tools), then add the folder to your PATH

Run the archiver:

```bash
python -m wab_archiver \
  --mode adb \
  --e2e 1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a6b7c8d9e0f1a2b \
  --wa_root /path/to/WhatsApp/storage \
  --output /path/to/output
```

The archiver pulls the encrypted backup and contacts directly from the device and decrypts on the fly. No separate steps needed.

> 💡 On Windows, replace `\` with `` ` `` (PowerShell) or `^` (cmd.exe).

---

### Alternative — Manual Pull

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

**Step 1 — Pull the encrypted backup:**

```bash
# Regular WhatsApp
adb pull /storage/emulated/0/Android/media/com.whatsapp/WhatsApp/Databases/msgstore.db.crypt15

# WhatsApp Business
adb pull /storage/emulated/0/Android/media/com.whatsapp.w4b/WhatsApp\ Business/Databases/msgstore.db.crypt15
```

Or browse to the respective folder manually.

**Step 2 — Decrypt:**

```bash
wadecrypt your_key msgstore.db.crypt15 msgstore.db
```

> ⚠️ Repeat these two steps every time you want to update your archive with new media.

Alternatively, skip the manual decrypt and let the archiver handle it by passing the `.crypt15` file directly:

```bash
python -m wab_archiver \
  --msgstore /path/to/msgstore.db.crypt15 \
  --e2e 1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a6b7c8d9e0f1a2b \
  --wa_root /path/to/WhatsApp/storage \
  --output /path/to/output
```

> ⚠️ If `--msgstore` points to a `.crypt15` file and `--e2e` is not provided, the archiver will exit with an error.

---

## Obtaining WhatsApp Contacts

> This step is only required for manual pull — when using automatic retrieval (`--mode adb`), contacts are pulled automatically.

Contacts are optional but strongly recommended — without them, folder names will show raw phone numbers instead of contact names.

For manual pull:

```bash
adb shell content query \
  --uri content://com.android.contacts/data \
  --projection display_name:data1 \
  | grep @s.whatsapp.net > wa_contacts
```

Pass the file to the archiver with `--contacts`.

> 💡 You only need to repeat this if your contacts have changed significantly since the last run.
