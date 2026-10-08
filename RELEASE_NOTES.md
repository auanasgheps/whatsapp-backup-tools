# WhatsApp Backup Tools v0.60

Version 0.60 brings support for shared contact cards across both tools, disk space savings with media hardlinking, and a smoother viewing experience in the offline web viewer.

> 📇 **Shared Contact Cards (.vcf)**: WhatsApp stores shared contacts directly inside backup databases rather than as media files. WhatsApp Backup Tools now extracts and saves contacts as standard `.vcf` files and displays them as native contact cards in the web viewer, complete with search and 1-click download support!

---

### 📁 WhatsApp Archiver

#### ✨ New Features
- **Shared Contact Cards Archiving (`.vcf`)**: Automatically extracts contacts shared in 1:1 and group chats into organized `.vcf` files with original message timestamps and duplicate collision protection.
- **Hardlink Duplicate Media Files (`--link-duplicates`)**: Drastically save disk storage by hardlinking identical media files shared across chats instead of duplicating files on disk. Unsupported filesystems (such as exFAT or network drives) are automatically detected upfront with safe fallback to copying.

#### ⚡ Improvements
- **Original Timestamps in Duplicate Reports**: The `duplicate_media_report.csv` now records original WhatsApp message dates and times for duplicate media files, making audit logs clear and easy to follow.

#### 🐛 Bug Fixes
- Fixed duplicate reporting when identical files were shared across multiple messages, and ensured address book documents are properly preserved during restore mode.
- Minor bug fixes and reliability improvements.

---

### 🌐 WhatsApp Viewer

#### ✨ New Features
- **Contact Cards in Chat & Gallery**: Received contacts now display in native WhatsApp-style contact cards with contact avatars, names, and a 1-click download button. Contact cards are also browsable in the Media Gallery and fully indexed in search.
- **"Edited" Message Indicator**: Messages edited in WhatsApp now display an "Edited" badge next to the timestamp, with a hover tooltip showing the exact edit date and time.
- **Lightbox Wheel Zoom & Pan**: Zoom smoothly into photos up to 6× using your mouse wheel, drag to pan around, and reset back to fit with a single click.

#### ⚡ Improvements
- **Media Gallery Filter Synchronization**: Switching media filters (such as switching from links back to photos) after scrolling down multiple months now seamlessly loads items at your current position without jumping or showing empty areas.
- **Photo Aspect Ratio & Shrink-Wrapping**: Vertical and tall photos now keep their natural aspect ratio without horizontal stretching, and chat bubbles fit snugly around images without empty space.

#### 🐛 Bug Fixes
- **Voice Message Audio Player**: Fixed an issue where the inline audio player for voice messages was invisible in Google Chrome, Microsoft Edge, and other Chromium-based browsers.
- **macOS / Older SQLite Compatibility**: Fixed a startup crash (`unrecognized option: "contentless_delete"`) when running on macOS or systems with SQLite versions older than 3.43.
- Minor UI polish and rendering fixes.

---

### 🚀 How to Upgrade

Update to the latest version using pip:

```bash
pip install --upgrade whatsapp-backup-tools
```

> 💡 **Windows users:** If `pip` is not recognized, run `py -m pip install --upgrade whatsapp-backup-tools` instead.
