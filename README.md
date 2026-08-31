# WhatsApp Backup Tools

<p align="center">
  <img src="icon.svg" alt="WhatsApp Backup Tools" width="120"/>
</p>

## Overview

WhatsApp Backup Tools is a suite to archive and access your WhatsApp data.
Consists of two complementary programs:

- **wab-archiver** (WhatsApp Backup Archiver) organises your WhatsApp media into a structured folder hierarchy using metadata from the WhatsApp database. Instead of an unstructured dump, you get a browsable archive sorted by contact or group, year, and direction (Sent/Received), with original message timestamps preserved.
- **wab-viewer** (WhatsApp Backup Viewer) lets you browse and search your archived chats through a local web UI, replicating WhatsApp Web. See [docs/viewer/](docs/viewer/).

Both tools run locally — no data leaves your machine. Windows, Linux, and macOS are supported. Python 3.11 required.

> ⚠️ **These tools are designed to run on a backup copy of your WhatsApp data — never on live device files.** The archiver always makes a copy of your data.

---

### WhatsApp Backup Archiver Features

Archive your WhatsApp media in a human readable format.

- Structured archival: `Contacts/` and `Groups/` top-level folders
- Year and `Sent/` and `Received/` subfolders for 1-to-1 chats
- Sender name appended to filenames in group chats
- Correct file timestamps preserved from the WhatsApp database
- Contact number change tracking — consolidates old and new numbers into one folder
- Contact rename detection across runs — folders renamed automatically
- Group rename detection across runs — group folders renamed automatically
- Groups with identical names handled correctly — each distinct group gets its own folder
- Safe re-runs: identical files skipped, collisions renamed, never overwritten
- Duplicate media detection across runs — CSV report of files with identical content at multiple archive paths
- Missing media CSV report for manual recovery of old or deleted files
- Multiple Android media source roots — repeat `--wa-root` to search across several WhatsApp folders (e.g. old archive + current phone). Best copy selected automatically; content conflicts reported separately
- Dry run mode for safe previewing before a full run
- WhatsApp platform: Android and iOS are both supported. No root or jailbreak are required.
    - Android: Syncthing is the recommended way to get your media files off the phone. If you prefer not to set that up, `--from-adb --pull-media` can pull them directly over USB — but it is slow and unreliable for large collections (many small files over ADB can take hours and drop mid-transfer). See [docs/archiver/setup-android.md](docs/archiver/setup-android.md).
    - iOS: reads directly from an iPhone backup, encrypted backups are supported via `wa-crypt-tools`
- Restore mode — reconstructs the original `WhatsApp/Media/` folder structure from the archive (Android only)

### WhatsApp Backup Viewer Features

Aims to replicate WhatsApp Web experience, but with your archived data.

- Sidebar with contacts and groups, message counts, last activity time
- Chronological chat timeline
- Full-text search across messages, scoped to current chat or all chats
- Media gallery — images, videos, and audio stream in-browser with seeking

### Supported Media Types

The archiver handles: Images, Videos, Audio, Voice messages, Video Messages, Animated GIFs, and Documents.

---

## Archive Structure

After a successful run, the archive is organised as follows:

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
├── wab-archiver.log
├── missing_media_report.csv
├── duplicate_media_report.csv
└── source_conflicts_report.csv  ← only when multiple --wa-root roots produce conflicting copies
```

- Files in **Contacts** folders retain their original filename
- Files in **Groups** folders have the sender name appended: `IMG-20230115-WA0001_JohnDoe.jpg`
- Your own sent media in groups is appended with `_Me`
- All copied files have their **modified date set to the original WhatsApp timestamp**
- Contact folder names use the `00` prefix for phone numbers (e.g. `0039...`), not `+`

---

## Quick Start

### wab-archiver quick start
Install first (see [docs/prerequisites.md](docs/prerequisites.md)):

```bash
pip install -e .
```

Always do a dry run first:

```bash
wab-archiver archive \
  --msgstore /path/to/msgstore.db \
  --wa-root /path/to/WhatsApp/storage \
  --output /path/to/output \
  --contacts /path/to/wa_contacts \
  --dry-run
```

> 💡 On Windows, replace `\` with `` ` `` (PowerShell) or `^` (cmd.exe).

> 💡 Repeat `--wa-root` to search multiple media folders and automatically select the best copy of each file.

See [docs/archiver/](docs/archiver/) for full setup instructions and command reference.

### wab-viewer quick start

First, run wab-archiver to create the archive. Then:

```bash
pip install flask
python -m wab_viewer /path/to/archive
```

Opens in your browser at `http://127.0.0.1:5000`. See [docs/viewer/](docs/viewer/) for full usage.

---

## Re-running the Archiver

The archiver is **safe to re-run** on an existing archive:
- Files already copied with identical content will be **silently skipped**
- Files with the same name but different content will be **renamed with a numeric suffix** and logged as a warning
- New media not yet in the archive will be **copied normally**
- If a contact has been **renamed** since the last run, their folder on disk will be **automatically renamed** to match, keeping the archive consolidated
- Recommended: use the config file to keep settings for easier re-runs

---

## Prerequisites

- **Python 3.11** or later is required
- **Android**: End-to-end encrypted backup must be **enabled** in WhatsApp before pulling the database. Root is not required
- **iOS/iPadOS**: End-to-end encrypted backup must be **disabled**

See [docs/prerequisites.md](docs/prerequisites.md) for full setup instructions: E2E backup configuration, database extraction (Android and iOS), contacts pull, and locating your media folder.

---

## Known Limitations

### Archiver

- Stickers are not archived in this version
- **Year folders reflect the local time of the machine running the script**, not UTC. A message sent just after midnight on 1 January will be filed under the new year only if your machine's clock agrees. This is intentional — the archive reflects your local experience of when media was shared
- **iOS restore mode is not supported.** Restore mode reconstructs the Android `Media/` folder layout, which has no equivalent on iOS. Running `wab-archiver restore` on an iOS archive exits with a clear error
- **iOS number change tracking is best-effort.** Contacts present in the device address book are consolidated automatically. Contacts not saved to the address book appear as separate folders
- **iOS HD media duplication.** When photos or videos are shared in HD quality on iOS, WhatsApp stores both a standard-quality preview and the HD download as independent rows with no linking data. Both versions are archived. On Android the low-quality copy is suppressed automatically when the HD version is present

### Viewer

See [viewer known limitations](docs/viewer/index.md#known-limitations).

---

## Credits

- Inspired by [Wa_Immich_Tagger](https://github.com/mac12m99/Wa_Immich_Tagger) by mac12m99 — provided the initial Android DB query pattern
- iOS backup reading approach inspired by [whatsapp-chat-exporter](https://github.com/KnugiHK/whatsapp-chat-exporter) by KnugiHK

---

## AI-Assisted Development

This tool was developed with the assistance of AI coding tools. Development was human-led and responsible throughout:

- All architecture and design decisions were made by the developer
- The AI received specific, detailed instructions at each step — it did not drive direction
- All generated code was reviewed by the developer before being accepted
- The tool was tested end-to-end by the developer

## Disclaimer

WhatsApp Backup Tools are not affiliated, associated, authorised, endorsed by, or in any way officially connected with WhatsApp LLC, or any of its subsidiaries or its affiliates.

The project is provided 'as is' without any express or implied warranties.
