# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [0.80]

### 📁 WhatsApp Backup Archiver (`wab-archiver`)

- **EXIF & Container Timestamp Writing**: Added zero-dependency native metadata injection (`--exif-timestamps` / `exif_timestamps = true`) writing message timestamps directly into JPEG APP1 EXIF segments (`DateTimeOriginal`, `DateTimeDigitized`, `DateTime`, `OffsetTimeOriginal`) and QuickTime/MP4 video containers (`mvhd`, `tkhd`, `mdhd` creation and modification timestamps in UTC). Protects camera capture dates, orientation tags, and shared hardlink inodes.
- **Retroactive Metadata Backfill Subcommand (`update-metadata`)**: Added a dedicated `wab-archiver update-metadata` command to retroactively stamp EXIF and container timestamps on previously archived media without requiring device sources. Tracks injection state via `exif_stamped` in `.wa_media_archiver.db` for instant idempotency, protects hardlink inodes, and restores Windows creation times (`SetFileTime`).
- **Incremental Collision Shielding & In-Memory Pre-fetch**: Added `load_archived_file_set` to pre-fetch archived media sets into memory during incremental runs, eliminating N+1 SQL queries across large archives, preventing false collision renames, and extending container timestamp patching to `.m4a` audio files.
- **Group Mention Preservation**: Added support for extracting and preserving group `@mentions` across incremental backups and differential archival passes in `preserved_messages.db` (schema v3), retaining mention metadata and participant associations even when messages are removed from the device.
- **iOS Call History Discovery & Verification**: Added discovery, staging, and SQLite integrity validation (`PRAGMA quick_check`) for WhatsApp call logs (`CallHistory.sqlite` / `CallHistory.storedata`) from iOS backups across standard and AppDomain paths.
- **Android Incremental Mention Ingestion**: Extended the delta replay engine to extract Tag 15 `mentionedJid` from Android incremental delta streams and insert participant records into `message_mentions` and `message_quoted_mentions`.

### 🌐 WhatsApp Backup Viewer (`wab-viewer`)

- **Large Chat Loading Latency & Tail Anchoring Acceleration**:
  - Replaced hardcoded 1,500 ms `requestAnimationFrame` loop in `_pinToBottom` with adaptive image settling in `wab_viewer/chat_viewer/app.js`. Text chats, cached media, and chats with 0 pending images unfreeze and settle in 2 animation frames (~32 ms) instead of waiting 1.5 seconds, while uncached media settles as soon as thumbnails load (typically 80–200 ms).
  - Added connection-level idempotency to `_ensure_jid_map_resolved` via sentinel temporary table `temp._resolution_done` in `wab_viewer/db_cache.py`, preventing repeated mapping rebuilds and dropping warm execution on the same connection from ~2,100 ms to <0.05 ms.
  - Bounded Step 6 direct message scanning in `_ensure_jid_map_resolved` strictly to chats with unresolved LIDs, eliminating unindexed `SCAN m` passes across 1M+ messages and accelerating cold resolution on 682 MB databases from 841 ms down to 6.1 ms with 100% mapping fidelity.
  - Added `_get_call_log_present_jids` process-level call JID caching in `wab_viewer/queries/calls.py`. In `_query_android_chat_calls`, chats without calls bypass SQL joins, participant counting, and caller info resolution, eliminating 800–1,200 ms of latency for >98% of chats.
  - Accelerated cold chat load on 236k-message chats from 5,259 ms to 383 ms (a 13.7x speedup) and warm chat load from 4,200 ms to 20 ms (a 200x speedup).
- **Media Gallery Query & Performance Optimizations**:
  - Implemented Two-Stage Deferred Join Pagination (`/api/media`) by selecting candidate message IDs first via indexed tables in 5–15 ms before hydrating full media joins, accelerating page 1 on 236k-message chats from 6,070 ms to 80 ms (a 75x acceleration).
  - Accelerated media count queries (`/api/media/count`) via direct SQL aggregations and session-level caching (`ViewerContext.media_counts_cache`), dropping count latency from 6,940 ms to 750 ms cold / 0.3 ms cached.
  - Decoupled media count fetching from page rendering so the gallery opens optimistically with default media types and dispatches page 1 requests immediately without waiting for counts.

- **Media Gallery Unified Stream Replay & Stat Filter Synchronicity**: Fixed an issue where switching from default photos+videos to all items ("Total" stat pill) failed to render new media types within the existing window. Reset pagination stream and reloaded page 1 (`types=all`) when toggling from filtered subsets to prevent skipped items, while keeping toggles back to default photos+videos as instantaneous in-memory DOM filters. Preserved archive view header counts on unpopulated past years.
- **Media Gallery Sparse Grid Row Packing & Layout Stabilization**: Packed grid rows to the top (`align-content: start; grid-auto-rows: min-content`) in `#media-gallery-grid` and pinned the infinite scroll sentinel to span all columns invisibly, eliminating layout distortion in sparse or single-item filtered views.
- **Voice Message Waveforms & Audio Player Component**: Implemented interactive 64-bar waveform scrubber and circular player with playback speed toggling (`1x`, `1.2x`, `1.5x`, `2x`). Extracts raw amplitude data from Android `audio_data.waveform` and Protobuf `audioMessage` tags, decodes iOS Opus audio client-side via Web Audio API (`decodeAudioData`), renders flat baseline fallbacks for undecodable audio, and caches waveforms in `recent_messages` and `preserved_messages`.
- **Dynamic Missing Media Handling in Chat & Media Grids**: Added runtime load error listeners to chat media and album grids, dynamically replacing failed images and videos with authentic `.media-missing-card` placeholders with blurred embedded thumbnail backgrounds, sanitized alt text, and lightbox bypass.
- **Media Gallery Serialization & Waveform Sanitization**: Sanitized binary waveform BLOBs across `/api/media`, `/api/media/documents`, `/api/media/links`, and `/api/search` to avoid 500 JSON serialization errors, and normalized message payloads in `/api/messages/at` for seamless jump-to-chat navigation.
- **Media Gallery Multi-Selection**: Re-enabled concurrent selection of images and videos in the media gallery grid with synchronized loading spinners and stat pill states.
- **Centered Gallery Loading & Race Condition Elimination**: Redesigned initial gallery loading with centered spinners, implemented session generation tracking (`galleryOpenGen`) with `AbortController` cancellation to eliminate stale fetch races, and implemented viewport-driven link preview hydration via `IntersectionObserver`.
- **Chat Avatars in Chat List & Starred Messages**: Added 42px circular avatars with initials and palette colors in the active and archived chat lists as well as starred message previews.
- **Lightbox Smooth Directional Transitions & Stage Architecture**: Replaced modal teardown with persistent stage architecture (`.lb-stage`), directional slide/fade transitions, adjacent preloading, stage-aware zoom clamping, and missing media navigation skipping.
- **Media Caption Collapsing Parity**: Extended long-message collapsing ("Read More" ↔ "Show Less") to media captions across all attachment formats with grapheme cluster preservation.
- **Chat Duration & System Message Exclusion**: Excluded group creation events, security notifications, and system/service messages from conversation start dates and active chat duration calculations.
- **Audio & Voice Note Gallery Differentiation**: Differentiated voice notes (PTT) from audio tracks across Android (`message.origin`) and iOS (`ZMEDIAORIGIN`), with dedicated `.gallery-audio-item` cards, duration badges, sanitized titles, distinct stat pills, and an integrated inline audio controller with playback teardown.
- **Call History & Call Inspector**: Added a dedicated "Calls" tab in the bottom navigation bar powered by `/api/calls`, featuring infinite-scroll list browsing, call type filtering (All, Missed, Video), two-line call cards, and a right-pane Call Inspector with caller metadata and participant breakdown. Includes a contextual "Jump to this Moment in Chat" button with time gap indicators navigating directly to the call timestamp in chat context.
- **Chronological In-Chat Call Bubbles**: Integrated voice and video call records directly into chat timelines across Android, iOS, and preserved databases as borderless call bubbles showing outcome, duration, and metadata popovers.
- **Starred Messages & Slide-Over Drawer**: Added global starred message browsing in the sidebar and a per-chat slide-over drawer with real-time toolbar count badges. Clicking any starred card smoothly jumps to the message with a pulsing gold halo animation.
- **Pinned Messages Banner & De-Referencing**: Added a docked pinned message banner strip beneath the chat header supporting multi-pin cycling (`1 of N`), session-scoped dismissal, and 1-click jumping with a pulsing emerald highlight. De-references target messages to suppress raw stanza IDs and renders rich previews across all media types.
- **Archived Chats Tab & Navigation**: Added a dedicated "Archived" tab isolating archived conversations from the active chat list, with empty-state messaging and full chat selection support.
- **Tab-Aware Real-Time Search & Global Shortcuts**: Dynamically routes the search box to server-side full-text search on Chats, instant client-side filtering on Calls and Starred, or archived chat filtering on Archived. Added `/` and `Ctrl+K` / `Cmd+K` global focus shortcuts and `Escape` dismissal chains.
- **Cross-Platform Group `@mentions` & 1-to-1 Chat Linking**: Parsed and hydrated group `@mentions` across Android (`message_mentions`) and iOS (Protobuf wire Tag 15 in `ZWAMEDIAITEM.ZMETADATA` and quoted `ContextInfo`). Formatted mentions display as styled badges that link directly to active, archived, or preserved 1-to-1 chats with that contact.
- **Collapsible Long Messages ("Read More" ↔ "Show Less")**: Long text messages exceeding 750 characters or 15 lines are truncated with an accessible two-way toggle, preserving emoji grapheme clusters (ZWJ sequences), formatting, and scroll stability, with automatic expansion on search or quote jumps.
- **Theme-Adaptive Group Participant Colors**: Assigned consistent 10-color participant palettes (based on WhatsApp's native high-contrast scheme) to group sender headers and quoted reply borders, dynamically adapting across dark and light themes.
- **Circular Video Note Player (Push-To-Video / PTV)**: Rendered video notes as circular video bubbles with centered play/pause controls, SVG countdown progress rings, remaining time pills, fullscreen lightbox viewing, and automatic viewport pausing.
- **In-Bubble Metadata Indicators**: Added compact 12px inline SVG icons for starred (`⭐`) and pinned (`📌`) messages within the `.msg-meta` timestamp cluster, maintaining a clean bubble layout without top headers.
- **Media Gallery Filtering, Performance & Visual Feedback Lifecycle**: Implemented an "All Media" default view with exclusive category pill isolation and clean state machine toggling. Resolved a critical CSS cascade flaw where flex-based audio items, missing media cards, and link cards overrode filter suppression, ensuring strict computed isolation (`display: none !important`) across all categories. Accelerated pagination by 95% via batch DOM fragment construction (`_insertGalleryGridBatch`), replaced layout-thrashing rect checks with instant scroll arithmetic, and auto-pages until viewport scrollbars appear to prevent pagination deadlocks. Added a complete feedback lifecycle including active stat pill spinners (`.gallery-stat.is-loading`), centered category loading indicators (`.gallery-filter-loading`), scroll loaders (`.gallery-scroll-loading`), and end-of-results notices (`.gallery-end-notice`). Excluded static and live locations (`type = 5`, `16`) from gallery streams and counts, unified document query handling (`message_type IN (8, 9)` / `ZMESSAGETYPE = 8`), deduplicated document counts, and ensured unarchived contact cards (`vcard`) are not marked missing.
- **Captioned Media Containment & Orientation Sizing**: Enforced fixed orientation-clamped media dimensions (landscape vs portrait) with flexible caption containment, preventing wide captions from stretching bubbles or leaving empty space.
- **Modernized UI Shell & Responsive Layout**: Restructured header with top-right branding, platform badge, and settings button; implemented responsive sidebar scaling bound to viewport and display scale; and elevated chat bubbles with subtle drop shadows.
- **Modular Backend Architecture & Performance Optimization**: Refactored `wab_viewer/main.py` into modular Blueprints, query builders, and per-instance `ViewerContext` state isolation with scoped thumbnail 404 caching. Accelerated iOS message pagination to scan physical `ZMESSAGEDATE` and `Z_PK` columns directly, eliminating temporary sorting tables on large chats.
- **Android Calls & Starred Privacy LID Resolution**: Automatically resolved privacy Linked Identities (`@lid`) to canonical phone numbers and contact names in Android call logs, starred messages, and chat lists.
- **Android Pinned & Starred Temp Table Resolution**: Ensured complete initialization of temporary mapping tables (`_contacts_resolved`, `_lid_map_resolved`, `_jid_map_resolved`) for Android pinned and starred queries regardless of prior chat filter execution.
- **iOS Call History CoreData Joins & Parity**: Mapped CoreData relationships (`Z1CALLEVENTS`, `Z1PARTICIPANTS`) in `CallHistory.sqlite` with privacy LID resolution and archived chat retention.

---

## [0.70]

### 📁 WhatsApp Backup Archiver (`wab-archiver`)

- **Message & Chat Preservation Across Archival Runs (`-P` / `--preserve-deleted`)**: Added a dedicated preservation database (`preserved_messages.db`) to retain messages deleted on phone ("Delete for Me"), sender-revoked messages ("Delete for Everyone"), and deleted chats across backup cycles without mutating original WhatsApp device databases. Includes automatic account fingerprint matching, covering indexes for sub-second diff performance, live progress reporting, and fail-safe `.old` database staging with automatic rollback.
- **Android Incremental Backups Merging (`msgstore-increment*`)**: Added automated multi-file discovery, ADB staging, decryption, and sequential delta replaying of WhatsApp incremental diff archives (`msgstore-increment-[ID]-[YYYY-MM-DD].db.crypt15`) into a unified SQLite database using a standalone Protocol Buffers wire/varint decoder and delta replay engine.
- **Database Restoration in Reverse Mode**: Enhanced `wab-archiver restore` to automatically restore `Whatsapp Databases/msgstore.db` back to `Databases/msgstore.db` at the target root alongside media files.
- **Automatic WhatsApp Database Schema Incompatibility Detection**: Proactively detects schema changes in `msgstore.db` and `ChatStorage.sqlite` as well as unsupported legacy WhatsApp database formats, presenting diagnostic guidance, upgrade recommendations, and sanitized zero-PII pre-filled GitHub issue links instead of unhandled SQL errors.
- **Categorized Duplicate Media Reports**: Reorganized `duplicate_media_report.csv` into prioritized media sections (Photos & Images, Videos, Documents, Audio, Animated GIFs, Stickers, Contacts) with visual CSV section headers, continuous group IDs, and a dedicated `media_type` column for effortless spreadsheet filtering.
- **Configurable Log Level & Debug Flags**: Set the default file handler log level (`wab-archiver.log`) to `INFO` to eliminate clutter, adding `-v`, `--debug`, and `--verbose` CLI flags to both `archive` and `restore` commands and supporting `debug` in configuration.
- **Sectioned TOML Configuration**: Structured `example-config.toml` into logical sections with recursive TOML table flattening, and updated commented defaults for `preserve_deleted` and `link_duplicates` to `true` for one-step activation.
- **Exact ADB Status Diagnosis**: Replaced generic connectivity checklist with targeted diagnoses distinguishing between 0 devices detected, multiple attached devices, host permission issues (`no permissions`), offline devices, and unauthorized devices.
- **Interactive Authorization Recovery Flow**: When an Android device is detected as `unauthorized`, prompts interactive users to optionally restart the ADB server (`adb kill-server`) to re-trigger the on-device "Allow USB debugging?" dialog, re-verifying device readiness automatically.
- **Precise Database Pull Error Messages**: Replaced generic fallback text on `pull_msgstore` failures with exact guidance based on command output (e.g., reminding users to enable End-to-End Encrypted Backup in WhatsApp settings when backups are missing on the phone).
- **Android Reverse Restore Argument Guard**: Fixed an issue where running reverse restore could fail when network path warnings were evaluated.
- **Sticker Categorization in Duplicate Reports**: Categorized `.webp` and `.was` stickers under the dedicated Stickers section at the end of duplicate reports across iOS and Android.

### 🌐 WhatsApp Backup Viewer (`wab-viewer`)

- **Interactive WhatsApp Polls & Winner Highlighting**: Added poll card rendering for Android and iOS databases with descriptive format labels ("Single choice" / "Multiple choice"), proportional vote bars, vote percentages, and local user choice indicators. The "View votes" progressive disclosure popup (`/api/poll_details/<msg_id>`) sorts options by highest vote count and highlights the winning option with a star indicator (`★`), complete with full-text search indexing across questions and options.
- **Missing Media Cards with Embedded Thumbnail Previews**: When media files are missing on disk, renders styled cards preserving the media's natural aspect ratio, overlaid with media type badges, dimensions, file size, and duration. If thumbnail data exists in WhatsApp database blobs (`message_thumbnail`, `media_hash_thumbnail`, `message_quoted_media` on Android, or `ZXMPPTHUMBPATH` / `ZTHUMBNAILLOCALPATH` on iOS), renders blurred background preview images beneath a frosted glass backdrop.
- **Revoked & View-Once Message Support**: Rendered native WhatsApp tombstones (`⃠ This message was deleted`) for revoked messages, status pills (`① Photo · Opened`) for purged view-once media, and overlay badges for preserved media, with database schema v3 migration (`recent_messages.is_view_once`).
- **Preserved Message & Chat Browsing**: Integrated with `preserved_messages.db` to display preserved chat badges, tooltip metadata, and preserved messages merged into chat timelines and full-text search.
- **Responsive Settings Modal Adaptability**: Added dynamic height constraints and internal scrolling (`#settings-body`) with a pinned header, ensuring the Settings modal adapts smoothly to small screens, split windows, and low-height viewports without overflowing.
- **Android Video Notes in Web Viewer**: Android Push-to-Video messages (`message_type = 28`) now reliably render with the authentic circular video note player and preview properly in the recent chat list, matching iOS video notes.
- **Background Event & System Message Chat List Isolation**: Contacts with zero exchanged messages no longer appear in your recent chat list due to internal WhatsApp events, and system notices no longer displace conversation previews or re-order recent chats.
- **Clarified Preserved Message & Chat Tooltip Attribution**: Refactored `.msg-preserved` tooltip formatting from ambiguous trailing timestamps to explicit source-first attribution (`Preserved from backup (<date>) • <reason>`), avoiding confusion with device deletion timestamps.
- **Quoted Message Navigation & Jump Stabilization**: Clicking a quoted reply reliably scrolls and jumps directly to the original quoted message with a highlight pulse, smoothly locking onto older photos or offloaded media without drifting from asynchronous image loading.

---

## [0.60]

### 📁 WhatsApp Backup Archiver (`wab-archiver`)

- **Stickers Archiving on Android**: Added complete extraction and archiving for WhatsApp stickers on Android (`STK-*.webp` and `*.was`) from `Media/WhatsApp Stickers/` into chat folders and database tracking, including targeted ADB pull support.
- **vCard (.vcf) Contact Cards Archiving**: Added extraction and archiving of shared contact cards (`.vcf`) from both Android (`msgstore.db`) and iOS (`ChatStorage.sqlite`) databases into the media archive output hierarchy (`Contacts/<Contact>/<Year>/<Direction>/<DisplayName>.vcf` or `Groups/<Group>/<Year>/<DisplayName> (<Sender>).vcf`).
- **Standard Serialization & Deduplication**: Serialized raw database vCard payloads into standard UTF-8 encoded files with CRLF line endings and preserved original message timestamps. Implemented deduplication and collision disambiguation via virtual original path tracking (`vcard:{msg_id}`).
- **Multi-Contact Bundles & Name Resolution**: Handled multi-contact share bundles by consolidating entries into single message `.vcf` files while resolving user-friendly composite names (e.g., `"Alice & 2 others"`).
- **Hardlink Duplicate Media Files (`--link-duplicates`)**: Added opt-in support for filesystem hardlinking duplicate media shared across chats instead of duplicating disk bytes. Includes upfront filesystem capability probing (detecting unsupported exFAT, FAT32, or network shares), per-file copy fallback on `OSError`, automatic database schema upgrade to v2 (`is_hardlink` column in `archive_copies`), duplicate reporting with `link_status` and original timestamps in `duplicate_media_report.csv` (probing active on-disk hardlink peers), and independent physical file restoration in restore mode.
- **Granular Skipped Breakdown & Duplicate Reporting**: The final archiver summary report now breaks down skipped files into clear categories (differentiating files already archived in prior runs from same-chat duplicate message references and empty vCards). When media files are deduplicated via `--link-duplicates`, the hardlink count is clearly reported under copied files, and overall duplicate counts in the archive are highlighted with a direct pointer to `duplicate_media_report.csv`.

### 🌐 WhatsApp Backup Viewer (`wab-viewer`)

- **Dedicated Stickers Section in Media Gallery & Chat Classification**: Added full sticker support across both platforms in the Media Gallery and chat views. On iOS, messages with `ZMESSAGETYPE = 15` are now properly categorized as stickers rather than falling through to generic images. Server-side type filtering (`types=sticker`) in `/api/media` and count badges in `/api/media/count` now accurately reflect sticker media.
- **Contact Card Chat Bubbles**: Rendered native WhatsApp-style contact cards with contact avatar icons, display names, "Contact card" subtitle, and clean `.vcf` download buttons.
- **Media Gallery & Archive View Integration**: Added square `.vcf` cards with contact icons to media galleries and archive tree views, complete with a dedicated `vcard` media filter tab and stat pill.
- **Full-Text Search (FTS)**: Included shared contact names in full-text search indexing across Android and iOS databases.
- **Chat List Previews**: Added contact card previews (`vcard`) in chat list snippets and timestamps.
- **Voice Message Audio Player in Chromium Browsers**: Fixed an issue where the inline audio player for voice messages failed to appear in Google Chrome, Microsoft Edge, Brave, and other Chromium-based browsers due to an intrinsic layout collapse when shrink-wrapping media containers. Audio players now render at full width with playback controls across all browsers.
- **Chat Bubble Photo Aspect Ratio & Bubble Shrink-Wrapping**: Resolved an issue where vertical and tall photos or videos were stretched horizontally when accompanied by captions, sender names, or quoted messages, and fixed message bubbles expanding unnecessarily with empty space when displaying standalone photos. Media containers and bubbles now tightly shrink-wrap photos to eliminate wasted space while preserving the intrinsic aspect ratio.
- **"Edited" Message Indicator**: Messages edited in WhatsApp now display an "Edited" label immediately to the left of the timestamp in chat bubbles and media grid cells, matching WhatsApp's native interface.
- **Media Gallery Viewport Sync & Filter Switching**: Enhanced Media Gallery lazy loading and filter transitions so that toggling media filters (such as switching from links back to photos) after scrolling down multiple months automatically backfills and hydrates images down to the visible viewport, maintaining scroll stability and preventing blank media grids.
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
