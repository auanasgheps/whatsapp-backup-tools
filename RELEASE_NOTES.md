# WhatsApp Backup Tools v0.70

Version 0.70 marks a major milestone for _WhatsApp Backup Tools_, progressing towards a more mature suite to handle your archiving needs.

Introducing deleted message preservation, Android incremental backup merging, polls, missing media thumbnail previews, and comprehensive system event support.

Work is planned to fully support features like call history and starred messages. 

Your feedback is highly appreciated!

#### **Release highlights:**

> 🛡️ **Message Preservation Across Backups**:
> Traditional WhatsApp backups only reflect the current state of your device, meaning messages or chats deleted between backup cycles vanish forever. With the new Message Preservation engine (`-P` / `--preserve-deleted`), deleted messages, revoked "Delete for Everyone" texts, and removed chats are automatically saved into a dedicated preserved history (`preserved_messages.db`). This feature compares the previous extraction with the current one, and preserves anything missing in a dedicated database.
Preserved records remain fully searchable and browsable in the web viewer with clear badges, without modifying original WhatsApp databases. 

> 🔬 **Engineering for Protocol Fidelity & Long-Term Reliability**:
> To ensure your archives stand the test of time, significant effort went into deeply mapping WhatsApp's internal database architectures, multi-database relationships, and binary protocols across both Android and iOS. This foundation unlocks full fidelity for complex entities, including those not yet implemented, while providing proactive resilience against WhatsApp schema changes and future updates.

---

### 📁 WhatsApp Archiver

#### ✨ New Features
- **Preserve Deleted Messages**: With the new `-P` / `--preserve-deleted` option, messages that were deleted from your phone between backup runs are safely retained in a preserved history database instead of vanishing from your archive.
- **Android Incremental Backups**: WhatsApp on Android regularly generates incremental backup packages (`msgstore-increment*.db.crypt15`). The archiver automatically discovers, decrypts, and sequentially merges these increments into your database with full entity support (reactions, polls, chat pins, locations, and edits). Previously, WhatsApp Archiver would only read the full `msgstore.db`, which was not the last backup, causing chats in the Viewer to not be up to date.
- **Database Restoration in Reverse Mode**: When restoring media back to your phone, the archiver now automatically restores your latest WhatsApp database (`msgstore.db`).
- **Accurate Connection & Status Troubleshooting for ADB Operations**: Instead of displaying a generic checklist of possibilities, the archiver now tells you the exact device status (distinguishing between unplugged phones, multiple connected devices, offline links, and missing host permissions).

#### ⚡ Improvements
- **Selective iOS Media Thumbnail Archiving**: When archiving from iPhone backups, companion `.thumb` preview files are now saved to an isolated `Whatsapp Databases/.thumbnails/` folder and indexed—extracted only when the primary photo or video is missing from disk—keeping user-facing media folders clean while ensuring offline previews remain accessible in the viewer.
- **Consolidated Run Summary**: The final run summary banner has been redesigned into clean functional tiers, providing clear metrics for copied, skipped, preserved, missing, and duplicate files without intermediate logging clutter.
- **Categorized Duplicate Media Reports**: The duplicate media report (`duplicate_media_report.csv`) is now neatly structured into prioritized sections (Photos, Videos, Documents, Audio, GIFs, Stickers, Contacts) with section headers and a dedicated `media_type` column for easy filtering in spreadsheets.
- **Automatic Schema Incompatibility Guidance**: If WhatsApp updates its internal database structure or if an older legacy backup is detected, the archiver displays clear diagnostic explanations and upgrade guidance instead of technical database errors.
- **Cleaner Console & Configurable Debug Logging**: File and console logs now default to concise `INFO` output, eliminating clutter. When troubleshooting, a new `-v` / `--debug` flag (or `debug = true` in config) enables full debug traces on demand.
- **Organized Configuration Files**: You can now structure your configuration file using nested sections (such as `[source.android]` or `[options.filtering]`) for cleaner management, with key features pre-set to `true` when uncommented.
- **Clearer Backup Retrieval Errors**: If pulling a WhatsApp backup database fails over ADB, the archiver provides direct actionable instructions (such as reminding you to create an End-to-End Encrypted Backup in WhatsApp).
- **Extended iOS Setup Guidance**: Detailed documentation guides you through backing up iOS devices using macOS Finder, the Apple Devices app on Windows, or iTunes, with troubleshooting tips for permissions, passwords, and WhatsApp Business.

#### 🐛 Bug Fixes
- **Sticker Categorization in Duplicate Reports**: Categorized `.webp` and `.was` stickers under the dedicated Stickers section at the end of duplicate reports across iOS and Android.
- **Restore Command Argument Guard**: Fixed an issue where running reverse restore could fail when network path warnings were evaluated.
- Minor bug fixes and reliability improvements.

---

### 🌐 WhatsApp Viewer

#### ✨ New Features
- **WhatsApp Polls**: Polls on both Android and iOS are now rendered just like in WhatsApp, showing options, vote bars, and who voted for each choice. The "View votes" popup sorts options by highest vote count with a star indicator next to the winning poll choice.
- **Exhaustive System & Group Events**: Group events and system notifications—including member additions and removals (with natural phrasing like "Alice, Bob, and Charlie"), group subject/icon changes, admin promotions, disappearing message timer changes, and security codes—are now fully recognized and rendered across Android and iOS.
- **Unavailable Media Cards & Preview Recovery**: When photos, videos, or audio files are missing from your computer or were never downloaded to disk, chat bubbles display dedicated unavailable media cards with media metadata (original filename, file size, dimensions, and playback duration). Low-resolution blurred previews are recovered directly from your backup whenever possible across both platforms—retrieving embedded thumbnails from Android databases (`message_thumbnail`, `media_hash_thumbnail`, and recovering previews preserved in quoting replies), and extracting `.thumb` preview files or embedded CoreData metadata on iOS.
- **Preserved & Revoked Message Visibility**: Messages deleted on the phone or revoked via "Delete for Everyone" are clearly labeled with preserved badges and status indicators.
- **View-Once Media Badges**: Ephemeral photos and videos are highlighted with View-Once badges so you know which media was originally sent with single-view privacy.
- **Preserved-Only Chat Browsing**: Easily browse and inspect chats and contacts that only exist in preserved backups, complete with message counts and dates.

#### ⚡ Improvements
- **Responsive Settings Modal**: The Settings panel now smoothly adapts to small browser windows, split-screen views, and compact laptop screens with internal scrolling and a pinned header so controls remain accessible at any height.

#### 🐛 Bug Fixes

- **Quoted Message Navigation**: Clicking a quoted reply now reliably scrolls and jumps directly to the original quoted message with a highlight pulse, even across long time intervals or when restored from incremental backups.
- **Background Event Chat List Filtering**: Contacts with zero exchanged messages no longer appear in your recent chat list due to WhatsApp internal events.
- **iOS Group Event Classification**: Resolved an issue where iOS group events could be ignored or treated as undownloaded media cards.
- Minor UI polish and rendering fixes.

---

### 🚀 How to Upgrade

Update to the latest version using pip:

```bash
pip install --upgrade whatsapp-backup-tools
```

> 💡 **Windows users:** If `pip` is not recognized, run `py -m pip install --upgrade whatsapp-backup-tools` instead.
