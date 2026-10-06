# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [0.60]

### 📁 WhatsApp Backup Archiver (`wab-archiver`)

- **vCard (.vcf) Contact Cards Archiving**: Added extraction and archiving of shared contact cards (`.vcf`) from both Android (`msgstore.db`) and iOS (`ChatStorage.sqlite`) databases into the media archive output hierarchy (`Contacts/<Contact>/<Year>/<Direction>/<DisplayName>.vcf` or `Groups/<Group>/<Year>/<DisplayName> (<Sender>).vcf`).
- **Standard Serialization & Deduplication**: Serialized raw database vCard payloads into standard UTF-8 encoded files with CRLF line endings and preserved original message timestamps. Implemented deduplication and collision disambiguation via virtual original path tracking (`vcard:{msg_id}`).
- **Multi-Contact Bundles & Name Resolution**: Handled multi-contact share bundles by consolidating entries into single message `.vcf` files while resolving user-friendly composite names (e.g., `"Alice & 2 others"`).

### 🌐 WhatsApp Backup Viewer (`wab-viewer`)

- **Contact Card Chat Bubbles**: Rendered native WhatsApp-style contact cards with contact avatar icons, display names, "Contact card" subtitle, and clean `.vcf` download buttons.
- **Media Gallery & Archive View Integration**: Added square `.vcf` cards with contact icons to media galleries and archive tree views, complete with a dedicated `vcard` media filter tab and stat pill.
- **Full-Text Search (FTS)**: Included shared contact names in full-text search indexing across Android and iOS databases.
- **Chat List Previews**: Added contact card previews (`vcard`) in chat list snippets and timestamps.
- **Voice Message Audio Player in Chromium Browsers**: Fixed an issue where the inline audio player for voice messages failed to appear in Google Chrome, Microsoft Edge, Brave, and other Chromium-based browsers due to an intrinsic layout collapse when shrink-wrapping media containers. Audio players now render at full width with playback controls across all browsers.
- **Chat Bubble Photo Aspect Ratio & Bubble Shrink-Wrapping**: Resolved an issue where vertical and tall photos or videos were stretched horizontally when accompanied by captions, sender names, or quoted messages, and fixed message bubbles expanding unnecessarily with empty space when displaying standalone photos. Media containers and bubbles now tightly shrink-wrap photos to eliminate wasted space while preserving the intrinsic aspect ratio.
- **Lightbox Wheel Zoom & Pan**: Added interactive wheel zooming (up to 6×) in the full-screen photo viewer, complete with smooth click-and-drag panning, click-to-toggle zoom (1× / 2.5×), and a 1-click reset badge.

---

## [0.51]

This minor update focuses on fixes, performance optimizations, and documentation refinements that were left outside of the initial stable release. Notably, official PyPI distribution (`pip install whatsapp-backup-tools`) is now available!

### 🌐 WhatsApp Backup Viewer (`wab-viewer`)

- **Faster & Smoother Media Gallery**: Browsing large media collections is now significantly faster and more responsive. Photos and videos load on demand as you scroll and unload when off-screen, dramatically reducing browser memory usage.
- **Accurate Video Previews**: Video thumbnails now show real preview frames instead of blank or black boxes, and generate smoothly in the background without interrupting scrolling.
- **Multi-Year Archive View**: Earlier years in chats with extensive media collections now automatically load in Archive View rather than stopping at the first page.
- **Chronological Month Dividers**: Fixed an issue where month headers in the media gallery could appear out of chronological order when paginating through mixed media types.
- **Accurate Chat Media Size**: Fixed disk size calculation for individual chats in the Chat Info panel to reflect total media storage accurately.

### 📁 WhatsApp Backup Archiver (`wab-archiver`)

- No changes in this release.

### 📦 Packaging & Installation

- **PyPI Distribution**: You can now install and update directly using `pip install whatsapp-backup-tools`.
- **Automated Publishing**: Streamlined release delivery to PyPI and TestPyPI via automated workflows.

### 📚 Documentation

- **Simplified Quick Start**: Refreshed the setup instructions in the `README` with an easy 3-step guide and a detailed walkthrough for syncing Android media with Syncthing.
- **Stickers Status**: Clarified that stickers are safely archived into dedicated folders by `wab-archiver` while timeline rendering in the viewer is in development.
- **Development Philosophy**: Added an overview of the engineering practices and extensive automated testing behind the project.

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
