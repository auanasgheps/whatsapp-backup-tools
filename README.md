
# WhatsApp Media Archiver

## Overview

WhatApp Media Archiver organises your WhatsApp media into a structured folder hierarchy using metadata from the WhatsApp database (`msgstore.db`). Instead of an unstructured dump, you get a browsable archive sorted by contact or group, year, and direction (Sent/Received), with original message timestamps preserved.

> ⚠️ **This tool is designed to run on a backup copy of your WhatsApp data — never on live device files.** For safeguard reasons, this script will always make a copy of your data.

### Features

- Structured archival: `Contacts/` and `Groups/` top-level folders
- Year and `Sent/`/`Received/` subfolders for 1-to-1 chats
- Sender name appended to filenames in group chats
- Correct file timestamps preserved from the WhatsApp database
- Contact number change tracking — consolidates old and new numbers into one folder
- Contact rename detection across runs — folders renamed automatically
- Group rename detection across runs — group folders renamed automatically
- Groups with identical names handled correctly — each distinct group gets its own folder
- Safe re-runs: identical files skipped, collisions renamed, never overwritten
- Duplicate media detection across runs — CSV report of files with identical content at multiple archive paths
- Missing media CSV report for manual recovery of old or deleted files
- Dry run mode for safe previewing before a full run
- Encrypted backup support (`msgstore.db.crypt15`) via `wa-crypt-tools`
- Restore mode — reconstructs the original `WhatsApp/Media/` folder structure from the archive

### Supported Media Types

| Type | Folder |
|---|---|
| Images | `Media/WhatsApp Images/` |
| Videos | `Media/WhatsApp Video/` |
| Shared audio | `Media/WhatsApp Audio/` |
| Voice messages | `Media/WhatsApp Voice Notes/` |
| Video messages | `Media/WhatsApp Video Notes/` |
| Animated GIFs | `Media/WhatsApp Animated Gifs/` |
| Documents | `Media/WhatsApp Documents/` |

---

## Archive Structure

After a successful run, the archive will be organized as follows:

```
<output>/
├── Contacts/
│   ├── John Doe (0039123456789)/
│   │   ├── 2023/
│   │   │   ├── Received/
│   │   │   └── Sent/
│   │   └── 2024/
│   │       ├── Received/
│   │       └── Sent/
│   └── Unknown (0044987654321)/
│       └── 2022/
│           └── Received/
├── Groups/
│   ├── Family Chat/
│   │   ├── 2023/
│   │   └── 2024/
│   └── Work Team/
│       └── 2024/
├── .wa_media_archiver.db
├── wa_media_archiver.log
├── missing_media_report.csv
└── duplicate_media_report.csv
```

- Files in **Contacts** folders retain their original filename
- Files in **Groups** folders have the sender name appended: `IMG-20230115-WA0001_JohnDoe.jpg`
- Your own sent media in groups is appended with `_Me`
- All copied files have their **modified date set to the original WhatsApp timestamp**
- Contact folder names use the `00` prefix for phone numbers (e.g. `0039...`), not `+`

---

### Platform

The main script runs on **Linux and macOS**.
Windows users can use `windows_extractor_companion.ps1` to pull the database and contacts via ADB, then transfer to Linux or macOS. See [windows_documentation.md](windows_documentation.md).

---

## Prerequisites

### 1. Python Environment

Ensure Python 3.10+ is installed.

For encrypted backup decryption, the script uses `wa-crypt-tools`. It is installed automatically the first time it is needed — no manual step required.

---

### 2. WhatsApp E2E Encrypted Backup

**Android users** — Before pulling the database from a non-rooted device, **End-to-end encrypted backup must be enabled** in WhatsApp. This is what generates the cryptographic key required for decryption.

On your phone, navigate to:

```
Settings → Chats → Chat Backup → End-to-end Encrypted Backup
```

Enable it and note the **cryptographic key** — a long alphanumeric string. This is **not** a password. Store it somewhere safe; you will need it every time you pull a fresh backup.

> If this step is skipped, the `.crypt15` backup file cannot be decrypted and the script will not work on a non-rooted device.

**iOS / iPadOS users** — End-to-end encrypted backup must be **disabled**. If it is enabled, the database inside the iTunes/Finder backup is encrypted in a way that cannot be decrypted by this script. Disable it in WhatsApp before creating your device backup.

---

### 3. Obtaining the WhatsApp Database

#### Android

##### Recommended — Automatic Retrieval (Linux, macOS and Windows)

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

##### Alternative — Manual Pull

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

#### iOS / iPadOS ⚠️ Work in Progress

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

### 4. Obtaining WhatsApp Contacts

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

### 5. Locating Your WhatsApp Media Folder

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

## Running the Script

### Command Reference

```
usage: wa_media_archiver.py [-h]
                      [-msg MSGSTORE]
                      [-e2e E2E_KEY]
                      [-c CONTACTS]
                      [-wa WA_ROOT]
                      -o OUTPUT
                      [-l LOG]
                      [-mode {adb,restore}]
                      [--dry-run]
                      [--limit N]
                      [--since DATE]
```

| Argument | Required | Description |
|---|---|---|
| `-msg` / `--msgstore` | No | Path to `msgstore.db` or `msgstore.db.crypt15`. Defaults to `msgstore.db` in the current folder |
| `-e2e` / `--e2e_key` | If encrypted | Your cryptographic key for `.crypt15` decryption |
| `-c` / `--contacts` | No | Path to the `wa_contacts` file exported via ADB |
| `-wa` / `--wa_root` | Unless `--mode restore` | Root path of your WhatsApp folder containing `Media/` |
| `-o` / `--output` | **Yes** | Destination folder for the archive |
| `-l` / `--log` | No | Custom log file path. Defaults to `<output>/wa_media_archiver.log` |
| `-mode` / `--mode` | No | `adb` = automatically pull msgstore and contacts from a connected Android device; `restore` = reconstruct original `Media/` tree from the archive |
| `--dry-run` | No | Simulate the run without copying any files |
| `--limit N` | No | Cap rows returned per query block (groups and 1-to-1 capped independently). Useful for test runs |
| `--since DATE` | No | Only include messages on or after this date (`YYYY-MM-DD`). Combines freely with `--limit` |

---

### Usage Examples

#### Dry run first — always recommended before a real run
```bash
python3 wa_media_archiver.py \
  --msgstore /path/to/msgstore.db \
  --wa_root /path/to/WhatsApp \
  --output /path/to/output \
  --contacts /path/to/wa_contacts \
  --dry-run
```

#### Manual decryption (pre-decrypted file passed to script)

```bash
# Step 1: decrypt manually
wadecrypt your_key msgstore.db.crypt15 msgstore.db

# Step 2: run the script
python3 wa_media_archiver.py \
  --msgstore /path/to/msgstore.db \
  --wa_root /path/to/WhatsApp \
  --output /path/to/output \
  --contacts /path/to/wa_contacts
```

#### Automatic decryption (script handles it)

```bash
python3 wa_media_archiver.py \
  --msgstore /path/to/msgstore.db.crypt15 \
  --e2e your_cryptographic_key \
  --wa_root /path/to/WhatsApp \
  --output /path/to/output \
  --contacts /path/to/wa_contacts
```

#### Dry run with encrypted database

```bash
python3 wa_media_archiver.py \
  --msgstore /path/to/msgstore.db.crypt15 \
  --e2e your_cryptographic_key \
  --wa_root /path/to/WhatsApp \
  --output /path/to/output \
  --dry-run
```

> 💡 Even in dry run mode, decryption still occurs so the DB can be queried. The decrypted `msgstore.db` is written to disk as a side effect regardless of `--dry-run`.

#### Run without contacts file
```bash
python3 wa_media_archiver.py \
  --msgstore /path/to/msgstore.db \
  --wa_root /path/to/WhatsApp \
  --output /path/to/output
```
> Phone numbers will be used as folder names where contacts are not resolved. 

#### Test run — recent files only
```bash
python3 wa_media_archiver.py \
  --msgstore /path/to/msgstore.db \
  --wa_root /path/to/WhatsApp \
  --output /path/to/output \
  --since 2025-01-01 \
  --dry-run
```

#### Test run — recent files, capped row count
```bash
python3 wa_media_archiver.py \
  --msgstore /path/to/msgstore.db \
  --wa_root /path/to/WhatsApp \
  --output /path/to/output \
  --since 2025-01-01 \
  --limit 100 \
  --dry-run
```

#### Row limit only (balanced sample across all time)
```bash
python3 wa_media_archiver.py \
  --msgstore /path/to/msgstore.db \
  --wa_root /path/to/WhatsApp \
  --output /path/to/output \
  --limit 250 \
  --dry-run
```

#### Restore original Media/ folder structure
```bash
python3 wa_media_archiver.py \
  --mode restore \
  --output /path/to/output
```

> 💡 `--wa_root` is not required in restore mode. The script reads `.wa_media_archiver.db` from the archive and reconstructs `<output>/Media/` in place. Use `--dry-run` to preview what would be written.

---

## Output Files

| File | Description |
|---|---|
| `wa_media_archiver.log` | Full run log including all copied, skipped, and missing files |
| `missing_media_report.csv` | Structured report of all media referenced in the DB but not found on disk. Useful for manual recovery from old backups |
| `duplicate_media_report.csv` | Report of media files with identical content at multiple archive paths (e.g. the same meme copied across multiple group chats). One row per path, sortable by `file_count` to find the most-shared content. Only written when duplicates exist |
| `.wa_media_archiver.db` | Single SQLite database storing all persistent state: contact folder index, group folder index, and the file archive map (original WhatsApp paths, content hashes as BLOB, and archive locations). Health checks (quick integrity scan, foreign key verification) run automatically on every open. Query-planner statistics refreshed with `ANALYZE` after each forward run. Do not delete unless you want to reset all tracking |
| `restore_report.csv` | Written by restore mode when issues are encountered. One row per problem: original path, which archive copy was used as source, and status (`unrestorable`, `collision_skipped`, `error`). Successfully restored and already-present identical files are not included. Not written if there are no issues |

---

## Restore Mode

Restore mode reconstructs the flat `WhatsApp/Media/` folder structure directly inside the archive folder, without needing the original device or database. This is useful when re-importing media into tools that expect the original WhatsApp layout.

```bash
python3 wa_media_archiver.py \
  --mode restore \
  --output /path/to/output
```

The reconstructed tree is written to `<output>/Media/`, alongside the existing `Contacts/` and `Groups/` folders. No files are overwritten — identical files already in place are skipped silently.

**How it works:** During every real (non-dry-run) forward run, the script records each original WhatsApp `file_path` and the archive path it was copied to, in `.wa_media_archiver.db`. Restore mode reads this data and copies each unique original file back to its original relative path. If the same file was archived from multiple chats, it is restored exactly once.

**Limitations:**
- Restored file timestamps reflect the WhatsApp message timestamp (same as in the archive), not the original on-device creation date.
- If a forward run was filtered with `--since` or `--limit`, the restore index only covers what was actually archived — the restored `Media/` will be a partial replica.

---

## Re-running the Script

The script is **safe to re-run** on an existing archive:
- Files already copied with identical content will be **silently skipped**
- Files with the same name but different content will be **renamed with a numeric suffix** and logged as a warning
- New media not yet in the archive will be **copied normally**
- If a contact has been **renamed** since the last run, their folder on disk will be **automatically renamed** to match, keeping the archive consolidated

---

## Known Limitations

- **Windows is not natively supported.** Windows users should use `windows_extractor_companion.ps1` to extract the database and contacts, then transfer to Linux or macOS to run `wa_media_archiver.py`.
- Very old media is likely missing from disk even if present in the database. Use `missing_media_report.csv` to assist manual recovery.
- Group names reflect the **current** name at time of DB export, not historical names.
- Stickers are not archived in this version.

---

## Credits

This project was inspired by [Wa_Immich_Tagger](https://github.com/mac12m99/Wa_Immich_Tagger) by mac12m99, a tool that tags WhatsApp media into Immich with chat and sender metadata. That script provided the initial DB query pattern that this tool builds on.

---

## AI-Assisted Development

This tool was developed with the assistance of AI coding tools. Development was human-led and responsible throughout:

- All architecture and design decisions were made by the developer
- The AI received specific, detailed instructions at each step — it did not drive direction
- All generated code was reviewed by the developer before being accepted
- The tool was tested end-to-end by the developer

AI was used as a productivity accelerator, not as a replacement for developer judgement.

## Disclaimer

WhatsApp Media Archiver is not affiliated, associated, authorized, endorsed by, or in any way officially connected with the WhatsApp LLC, or any of its subsidiaries or its affiliates. 

The project is provided 'as is' without any express or implied warranties.