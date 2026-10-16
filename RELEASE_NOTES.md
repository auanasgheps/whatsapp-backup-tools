# WhatsApp Backup Tools v0.70

Version 0.70 brings powerful new capabilities to keep your WhatsApp archives complete, resilient, and authentic: deleted message preservation across backup cycles, automated Android incremental backup merging, interactive WhatsApp polls, missing media thumbnail previews, and seamless device troubleshooting.

---

### 📁 WhatsApp Archiver

#### ✨ New Features
- **Preserve Deleted Messages**: With the new `-P` / `--preserve-deleted` option, messages that were deleted from your phone between backup runs are safely retained in a preserved history database instead of vanishing from your archive.
- **Android Incremental Backups**: WhatsApp on Android now regularly generates incremental backup diffs (`msgstore-increment*.db.crypt15`). The archiver automatically discovers, decrypts, and sequentially merges these increments into your database so no recent messages are lost.
- **Database Restoration in Reverse Mode**: When restoring media back to your phone, the archiver now automatically restores your latest WhatsApp database (`msgstore.db`) alongside media files.
- **Interactive ADB Authorization Recovery**: If your Android device is detected but unauthorized, the archiver can now prompt you to restart the ADB server on the spot to re-trigger the "Allow USB debugging" prompt directly on your phone.

#### ⚡ Improvements
- **Categorized Duplicate Media Reports**: The duplicate media report (`duplicate_media_report.csv`) is now neatly structured into prioritized sections (Photos, Videos, Documents, Audio, GIFs, Stickers, Contacts) with section headers and a dedicated `media_type` column for easy filtering in spreadsheets.
- **Automatic Schema Incompatibility Guidance**: If WhatsApp updates its internal database structure or if an older legacy backup is detected, the archiver displays clear diagnostic explanations and upgrade guidance instead of technical database errors.
- **Cleaner Console & Configurable Debug Logging**: File and console logs now default to concise `INFO` output, eliminating clutter. When troubleshooting, a new `-v` / `--debug` flag (or `debug = true` in config) enables full debug traces on demand.
- **Organized Configuration Files**: You can now structure your configuration file using nested sections (such as `[source.android]` or `[options.filtering]`) for cleaner management, with key features pre-set to `true` when uncommented.
- **Accurate Connection & Status Troubleshooting**: Instead of displaying a generic checklist of possibilities, the archiver now tells you the exact device status (distinguishing between unplugged phones, multiple connected devices, offline links, and missing host permissions).
- **Clearer Backup Retrieval Errors**: If pulling a WhatsApp backup database fails over ADB, the archiver provides direct actionable instructions (such as reminding you to create an End-to-End Encrypted Backup in WhatsApp).

#### 🐛 Bug Fixes
- Minor bug fixes and reliability improvements.

---

### 🌐 WhatsApp Viewer

#### ✨ New Features
- **Interactive WhatsApp Polls**: Polls on both Android and iOS are now rendered just like in WhatsApp, showing options, vote bars, and who voted for each choice.
- **Missing Media Previews & Details**: When photos or videos are missing from your computer, the viewer renders neat missing media cards showing media type, dimensions, duration, and file size, with blurred thumbnail previews extracted directly from the backup database.
- **Preserved & Revoked Message Visibility**: Messages deleted on the phone or revoked via "Delete for Everyone" are clearly labeled with preserved badges and status indicators.
- **View-Once Media Badges**: Ephemeral photos and videos are highlighted with View-Once badges so you know which media was originally sent with single-view privacy.
- **Preserved-Only Chat Browsing**: Easily browse and inspect chats and contacts that only exist in preserved backups, complete with message counts and dates.

#### ⚡ Improvements
- **Refined Poll Results & Winner Highlights**: Poll cards display descriptive format badges ("Single choice" / "Multiple choice") without action prompts, and the "View votes" popup sorts options by highest vote count with a star indicator next to the winning poll choice.
- **Responsive Settings Modal**: The Settings panel now smoothly adapts to small browser windows, split-screen views, and compact laptop screens with internal scrolling and a pinned header so controls remain accessible at any height.

#### 🐛 Bug Fixes
- Minor UI polish and rendering fixes.

---

### 🚀 How to Upgrade

Update to the latest version using pip:

```bash
pip install --upgrade whatsapp-backup-tools
```

> 💡 **Windows users:** If `pip` is not recognized, run `py -m pip install --upgrade whatsapp-backup-tools` instead.
