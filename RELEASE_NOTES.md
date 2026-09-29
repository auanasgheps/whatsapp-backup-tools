# WhatsApp Backup Tools v0.50

Introducing **WhatsApp Backup Tools**, a suite to archive and access your WhatsApp data locally and privately.

**Privacy, Privacy, Privacy**: Both tools run 100% locally on your machine. No data leaves your computer.

## What's New in v0.50

### 📦 WhatsApp Backup Archiver (`wab-archiver`)
The Archiver is the former "WhatsApp Media Archiver" which gave the original name to the repo, now refined and evolved.

- **Organized Archive**: Automatically sorts your WhatsApp media into structured folders (`Contacts/` and `Groups/`) organized by year and Sent/Received.
- **Cross-Platform Support**: Works with Android backups (no root required) and iOS backups (both plaintext and encrypted iTunes backups).
- **Identity & Rename Tracking**: Automatically detects contact renames, phone number migrations, and groups with identical names across runs.
- **Audit Reports**: Exports comprehensive missing media, duplicate media, and conflict audit CSV reports.
- **Safe & Incremental**: Skips already archived files and preserves original message timestamps.

**Notable features added in this release:**
- **ADB Media Pull (`--pull-media`)**: Pull media directly from your Android device over USB into a permanent staging folder with resume support and remote MD5 verification.
- **iOS Auxiliary Databases Extraction**: Automatically extracts auxiliary databases (`ExtChatDatabase.sqlite`, `MessagingInfraDatabase.sqlite`, `LID.sqlite`, etc.) from backups to preserve rich metadata and receipts.
- **Multi-Root Source Merging (`--wa-root`)**: Merge media from multiple drives or backup folders with automatic conflict auditing (`source_conflicts_report.csv`).
- **HD Deduplication**: Detects low-resolution and HD copies of the same photo or video, archiving only the best-quality version.
- **...and more!**: Config file generator (`wab-archiver config generate`), automatic database health checks, dry-run folder synchronization, and cyclic contact loop resolution.

### 🌐 WhatsApp Backup Viewer (`wab-viewer`)
Access your backups like WhatsApp Web, but local and offline.

- **Authentic Local Web UI**: Browse and search chats offline in a clean interface replicating WhatsApp Web with full dark and light theme support.
- **Rich Timeline**: Full message history with quoted messages, reactions, read/delivery receipts, and service events.
- **In-Browser Media Player**: Stream video, play voice notes with seeking support, and preview photos in a full-screen lightbox.
- **Media Gallery**: Explore chat media through a thumbnail grid or browse the underlying folder structure.
- **Instant Search & Jump to Date**: Fast full-text search across all conversations or scoped to individual chats.

### ⚠️ Initial Release Notes
It's the first release of the tool, and work is planned to expand its capacities.

Call history, poll voting details, and stickers are not yet rendered in the timeline. Large media gallery thumbnail optimizations are planned for subsequent updates.

---


### 🚀 Getting Started

```bash
# Clone the repository
git clone https://github.com/auanasgheps/whatsapp-backup-tools.git
cd whatsapp-backup-tools

# Install dependencies and tools
pip install .

# Archive your backup
wab-archiver archive --help

# Open the viewer
wab-viewer /path/to/archive
```

*I am working on distributing the tools via PyPI - `pip install wab-tools` will be available soon!.*
