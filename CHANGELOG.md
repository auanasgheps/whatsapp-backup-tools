# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [0.51]

### 🌐 WhatsApp Backup Viewer (`wab-viewer`) - *Fixes*

- **Media Gallery Month Dividers & Ordering**: Fixed out-of-order month headers (e.g. June > January > May) caused by paginated media batches landing below older document/link headers. Unified chronological grid insertion for media and secondary items, ensuring month dividers remain strictly unique, properly sorted, and accurately synchronized with type filters.

---

## [0.50] - Initial Public Suite Release

This release marks the unification of the project into **WhatsApp Backup Tools (`wab-tools`)**, introducing the brand-new **`wab-viewer`** local WebUI and major architectural evolutions to **`wab-archiver`** (formerly *WhatsApp Media Archiver*).

### 🌐 WhatsApp Backup Viewer (`wab-viewer`) - *New Tool*

- **Offline WebUI**: Local browser interface replicating WhatsApp Web for reading archived chats completely offline.
- **Full Chat Timeline**: Displays complete conversation histories with quoted messages, reactions, read/delivered receipts, and group service messages.
- **In-Browser Media Player & Lightbox**: Stream videos and audio with HTTP Range seeking support, listen to voice notes, and preview photos in a full-screen lightbox.
- **Media Gallery & Archive Tree**: Explore chat media through a responsive thumbnail grid or browse the original folder hierarchy.
- **Full-Text Search (FTS5)**: Fast message search across all chats or scoped to a single conversation, with date-picker calendar navigation.
- **Performance Optimized**: Virtualized DOM windowing keeping ~100 elements rendered at once for lag-free scrolling across tens of thousands of messages.

### 📦 WhatsApp Backup Archiver (`wab-archiver`) - *Major Enhancements*

*The archiver has been heavily overhauled from the original media archiver script into a production-grade CLI suite:*

- **ADB Direct Media Pull (`--pull-media`)**:
  - Directly pull media files from connected Android devices over USB into a permanent staging folder without needing Syncthing.
  - Features remote MD5 verification, automatic resume of interrupted transfers, `--since` date filtering, and conflict auditing (`adb_conflicts_report.csv`).
- **iOS Auxiliary Databases Extraction**:
  - Automatically extracts auxiliary SQLite databases (`ExtChatDatabase.sqlite`, `MessagingInfraDatabase.sqlite`, `LID.sqlite`, `CallHistory.sqlite`, `Labels.sqlite`) from both plaintext and encrypted iTunes backups.
  - Enables rich timeline receipts and HD media associations without requiring manual database extraction.
- **Multi-Root Source Merging (`--wa-root`)**:
  - Repeat `--wa-root` to merge media from multiple backup drives or older archives into one consolidated folder.
  - Automatic file comparison and collision detection exporting `source_conflicts_report.csv` when identical filenames differ in content.
- **Smart Contact Identity & Migration Tracking**:
  - Automatically resolves contact renames and phone number migrations across runs, preserving consolidated folders.
  - Group name disambiguation distinguishing different groups that share identical names.
- **HD Media Deduplication**:
  - Detects low-res and HD versions of the same photo/video via Android `message_association` and iOS `ExtChatDatabase.sqlite`, archiving only the best-quality copy.
- **Expanded Audit & Collision Reports**:
  - Added `duplicate_media_report.csv` (identifying duplicate files across chats) and `source_conflicts_report.csv` (auditing collisions across multiple `--wa-root` sources) alongside the existing `missing_media_report.csv`.
- **Configuration File (`config.toml`)**:
  - Generate and manage runs via `wab-archiver config generate`, with auto-discovery and path normalization across Windows, macOS, and Linux.
- **Non-Destructive Dry Run & Health Checks**:
  - `--dry-run` flag across all archiving and synchronization commands.
  - Database health check validating SQLite foreign key integrity and schema compatibility before execution.

### 🐛 WhatsApp Backup Archiver - *Notable Bug Fixes*

- **Contact Redirection Loops**: Fixed a crash caused by circular phone number migration chains in WhatsApp's contact tables.
- **iOS Timestamp Alignment**: Fixed Apple Core Data epoch (Jan 1, 2001) timestamp offsets on iOS auxiliary databases to accurately preserve original message modified dates.
- **Cross-Platform Filename Sanitization**: Fixed collisions and filesystem write errors on Windows when saving media files containing emojis, reserved names, or illegal characters (`:`, `?`, `*`, `"`).
- **ADB Fault Tolerance & Recovery**: Fixed ADB connection dropouts during `--pull-media` by implementing remote MD5 verification, resume support, and automatic partial file cleanup.
- **SQLite Locking & Freelist Bloat**: Corrected `INCREMENTAL` auto-vacuum pragma order and added defragmentation routines to reclaim disk space after large indexing operations.

### 📦 Packaging & Installation

- Unified CLI under package `wab-tools` with `wab-archiver` and `wab-viewer` entrypoints.
- Multi-OS automated test suite running on Linux, macOS, and Windows.
- Distributed via GitHub Releases with source and wheel packages; PyPI publishing planned for upcoming update.
