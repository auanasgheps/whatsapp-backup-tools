[← Back to Archiver Overview](index.md) • [Main Readme](../../README.md)

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

You need a local copy of your `WhatsApp/` folder (the one **containing** the `Media` subfolder) to run the archiver. There are two ways to get it.

### Method 1: Syncthing (Recommended)

[Syncthing](https://syncthing.net/) is a free, open-source tool that continuously syncs your WhatsApp media from your phone to your computer over your local Wi-Fi. Once set up, it keeps your media backup up to date automatically in the background.

1. **Install Syncthing on your computer**: Download it from [syncthing.net](https://syncthing.net/).
2. **Install Syncthing on your Android phone**: Install [Syncthing for Android (Google Play)](https://play.google.com/store/apps/details?id=com.github.catfriend1.syncthingandroid).
3. **Connect your devices**: Open Syncthing on both devices and pair them (e.g. by scanning the computer's pairing QR code from the phone app).
4. **Share your WhatsApp folder**: In the phone app, add a new folder, browse to:
   ```
   Android/media/com.whatsapp/WhatsApp
   ```
   and share it with your computer.
5. **Accept on your computer**: Accept the shared folder on your computer.

The synchronized folder on your computer will look like this:
```
/path/to/WhatsApp/
├── Databases/
└── Media/
    ├── WhatsApp Images/
    ├── WhatsApp Video/
    └── WhatsApp Audio/
```

Pass this synced `WhatsApp/` path (the folder containing `Media/`) to the archiver using `--wa-root`.

### ADB Pull Media (alternative)

If you prefer not to set up Syncthing, the archiver can pull your media files directly from the device over USB. This is significantly **slower and less reliable** — ADB overhead per file makes it impractical for large collections, and connections can drop mid-transfer.

> 💡 **Syncthing is the recommended approach.** Use ADB pull only if you are unfamiliar with it or need a one-time transfer without installing additional software.

**How it works:** The archiver tracks every file it pulls in a state table inside the archive's `.wa_media_archiver.db`. On subsequent runs:

- Files already in the archive are **skipped without re-transferring** — detected via filename and file size, with remote MD5 verification for matching sizes
- When combined with `--since <DATE>`, only media referenced by messages on or after that date is transferred, significantly speeding up partial or catch-up transfers
- Interrupted transfers are **resumed automatically** — partial files are cleaned up at the start of each run and re-pulled
- Files with the same name but different content on device vs archive are **logged to `adb_conflicts_report.csv`** and skipped — resolve these manually

#### Understanding the Staging Folder (`--staging`)

When you run with `--pull-media`, the `--staging <path>` argument is required. It is important to understand how this folder operates:

- **Permanent phone backup:** The staging folder replicates the phone's native WhatsApp structure (`Media/WhatsApp Images`, `Media/WhatsApp Video`, etc.). It is a permanent 1:1 copy of your phone storage — effectively serving as your phone's media backup on your computer.
- **Treated as `--wa-root`:** After pulling media from the device, the archiver treats the staging folder as its media source root (`wa_root`).
- **Files are not deleted:** Because the staging folder is the media source root, files inside it are **never deleted, purged, or moved** after archiving finishes. **Do not use a temporary folder** (like `/tmp`) and do not delete this folder between runs.
- **Media is copied to the output folder:** The archiver reads files from the staging folder and **copies** them into the `--output` folder, organizing them into the structured archive (`Contacts/`, `Groups/`, chronological year folders, `Sent/` and `Received/`).
- **Efficient future runs:** Keeping the staging folder intact allows subsequent runs to retain previously pulled media. The archiver only needs to pull newly added files from your phone, avoiding long re-downloads.

**Requirements:**
- USB debugging enabled on the device
- `--staging <path>` — a persistent local folder for pulled files (phone backup)

**Example:**

```bash
wab-archiver archive \
  --from-adb \
  --e2e-key 1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a6b7c8d9e0f1a2b \
  --pull-media \
  --staging /path/to/wa-staging \
  --output /path/to/output
```

> 💡 In this command, media is pulled from the device into `/path/to/wa-staging` (the permanent phone backup). The archiver treats that staging folder as `--wa-root` and copies the organized media into `/path/to/output`.

> ⚠️ On first run, every file is transferred — if you have many gigabytes of media, expect this to take hours. Voice messages and other small files are the bottleneck: ADB spawns a new subprocess per file, making transfers very slow per unit of data.

**Hybrid workflow:** If you already have a manual copy of your media (via MTP/cable), archive it first using `--wa-root` — without `--from-adb`:

```bash
wab-archiver archive \
  --wa-root /path/to/existing-WhatsApp \
  --output /path/to/output
```

This populates the archive DB with all your existing files. From the next run onward, switch to `--from-adb --pull-media` — the archiver will skip everything already archived and only pull new files.

> Note: `--pull-media` and `--wa-root` are mutually exclusive and cannot be combined in a single invocation.

### Multiple Source Folders

If your media is spread across multiple locations (e.g. an old backup folder plus your current phone's `WhatsApp/` folder), repeat `--wa-root` for each source. The archiver searches all roots and selects the best available copy of each file automatically.

> ⚠️ **Run the archiver on the same machine where the media files are physically stored.** Processing files over a network share (NFS, SMB, etc.) will be significantly slower — the archiver hashes and copies every file.

---

## Obtaining the Database

### Recommended — Automatic Retrieval (`--from-adb`)

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
wab-archiver archive \
  --from-adb \
  --wa-root /path/to/WhatsApp \
  --e2e-key 1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a6b7c8d9e0f1a2b \
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
wab-archiver archive \
  --msgstore /path/to/msgstore.db.crypt15 \
  --e2e-key 1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a6b7c8d9e0f1a2b \
  --wa-root /path/to/WhatsApp/storage \
  --output /path/to/output
```

> ⚠️ If `--msgstore` points to a `.crypt15` file and `--e2e-key` is not provided, the archiver will exit with an error.

---

## Obtaining WhatsApp Contacts

> This step is only required for manual pull — when using `--from-adb`, contacts are pulled automatically.

Contacts are optional but strongly recommended — without them, folder names will show raw phone numbers instead of contact names.

For manual pull, query the Android contacts provider via ADB:

```bash
adb shell content query \
  --uri content://com.android.contacts/data \
  --projection display_name:data1:data4:mimetype \
  > wa_contacts
```

Pass the file to the archiver with `--contacts`.

> 💡 This command runs directly across Linux, macOS, and Windows (PowerShell or cmd.exe) without external tools like `grep`. It exports both WhatsApp profile links and device phone contacts.
>
> Alternatively, `--contacts` also accepts a plain text or CSV file with lines formatted as `phone_number,contact_name` (e.g. `+15551234567, Alice` or `15551234567, Alice`).

### Troubleshooting Contact Names on Android

If your exported contacts do not match your chats, or WhatsApp contacts appear to be missing:

1. **Refresh WhatsApp contacts**: Open WhatsApp on your phone → tap **New Chat** (chat bubble/pencil icon) → tap **⋮ (three dots)** in the top right → select **Refresh**.
2. **Ensure WhatsApp Contact Sync is enabled**: Go to Android **Settings** → **Passwords & accounts** (or **Accounts and backup → Manage accounts** on Samsung) → select **WhatsApp** → tap **Account sync** → make sure **Contacts** is toggled ON and tap **Sync now**.
3. Re-run the contact export command.

> 💡 You only need to repeat this if your contacts have changed significantly since the last run.

---

## Next Steps

Now that you have your Android backup, media folder, and contacts ready:

1. 📖 Review the **[Command Reference & Config File](command-reference.md)** to configure your run.
2. 🧪 Run your command with `--dry-run` to preview the archive before copying files.
3. 🌐 Once archived, launch the **[WhatsApp Backup Viewer](../viewer/index.md)** to browse your chats.

