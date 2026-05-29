
# WhatsApp Media Archiver

## Overview

WhatsApp Media Archiver organises your WhatsApp media into a structured folder hierarchy using metadata from the WhatsApp database (`msgstore.db`). Instead of an unstructured dump, you get a browsable archive sorted by contact or group, year, and direction (Sent/Received), with original message timestamps preserved.

> ⚠️ **This tool is designed to run on a backup copy of your WhatsApp data — never on live device files.** For safeguard reasons, this script will always make a copy of your data.

### Features

- Structured archival: `Contacts/` and `Groups/` top-level folders
- Year and `Sent/`and `Received/` subfolders for 1-to-1 chats
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
- iOS support — reads directly from an iPhone backup (`--ios_backup`); no third-party extraction tool required
- Encrypted backup support (`msgstore.db.crypt15`) via `wa-crypt-tools`
- Restore mode — reconstructs the original `WhatsApp/Media/` folder structure from the archive (Android only)

### Supported Media Types

The script handles: Images, Videos, Audio, Voice messages, Video Messages, Animated GIFs, and Documents.

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

The script runs on **Linux, macOS, and Windows** (Python 3.10+).

> ⚠️ **Run the script on the same machine where the media files are physically stored.** Processing files over a network share (NFS, SMB, etc.) will be significantly slower.

---

## Prerequisites

- **Python 3.10+** required
- **`wa-crypt-tools`** — required for encrypted backup decryption (`pip install wa-crypt-tools`)
- **Android**: End-to-end encrypted backup must be **enabled** in WhatsApp before pulling the database
- **iOS/iPadOS**: End-to-end encrypted backup must be **disabled**
- **ADB** required for automatic retrieval (`--mode adb`)

See [docs/prerequisites.md](docs/prerequisites.md) for full setup instructions: E2E backup configuration, database extraction (Android and iOS), contacts pull, and locating your media folder.

---

## Running the Script

Always do a dry run first:

```bash
python3 wa_media_archiver.py \
  --msgstore /path/to/msgstore.db \
  --wa_root /path/to/WhatsApp \
  --output /path/to/output \
  --contacts /path/to/wa_contacts \
  --dry-run
```

> 💡 On Windows, replace `\` with `` ` `` (PowerShell) or `^` (cmd.exe).

See [docs/running-the-script.md](docs/running-the-script.md) for the full command reference, all usage examples, output files description, and restore mode.

---

## Re-running the Script

The script is **safe to re-run** on an existing archive:
- Files already copied with identical content will be **silently skipped**
- Files with the same name but different content will be **renamed with a numeric suffix** and logged as a warning
- New media not yet in the archive will be **copied normally**
- If a contact has been **renamed** since the last run, their folder on disk will be **automatically renamed** to match, keeping the archive consolidated

---

## Known Limitations

- Very old media is likely missing from disk even if present in the database. Use `missing_media_report.csv` to assist manual recovery.
- Group names reflect the **current** name at time of DB export, not historical names.
- Stickers are not archived in this version.
- **Year folders reflect the local time of the machine running the script**, not UTC. A message sent just after midnight on 1 January will be filed under the new year only if your machine's clock agrees. This is intentional — the archive reflects your local experience of when media was shared.
- **iOS restore mode is not supported.** Restore mode reconstructs the Android `Media/` folder layout, which has no equivalent on iOS. Running `--mode restore` on an iOS archive exits with a clear error.
- **iOS encrypted backups are not supported.** If your iPhone backup is encrypted, disable encryption in Finder (macOS) or Apple Devices (Windows), create a new backup, then re-run.
- **iOS number change tracking is best-effort.** Contacts present in the device address book are consolidated automatically. Contacts not saved to the address book (`ZCONTACTABID = NULL`) appear as separate folders.

---

## Credits

- Inspired by [Wa_Immich_Tagger](https://github.com/mac12m99/Wa_Immich_Tagger) by mac12m99 — provided the initial Android DB query pattern.
- iOS backup reading approach inspired by [whatsapp-chat-exporter](https://github.com/KnugiHK/whatsapp-chat-exporter) by KnugiHK — the idea of reading directly from the iPhone backup via `Manifest.db` instead of requiring a third-party extraction tool. iOS implementation in this project is original code.

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