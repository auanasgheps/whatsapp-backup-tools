[← Back to Main Readme](../../README.md) • [Archiver Guide](../archiver/index.md)

# WhatsApp Backup Viewer (`wab-viewer`)

Browse and search your archived WhatsApp chats offline through a local web UI replicating WhatsApp Web.

**Whatsapp Backup Viewer screenshots**

<p float="left">
  <img src="images/viewer_dark.png" width="700" />
  <img src="images/viewer_clean.png" width="700" /> 
</p>

## Quick Start

Point `wab-viewer` to your archive output folder:

```bash
wab-viewer /path/to/archive
```

The viewer opens your browser at `http://127.0.0.1:5000`. Use `--rescan` to force a cache rebuild.

```bash
# Example
wab-viewer ./output

# Custom port
wab-viewer ./output --port 8080

# Expose on the local network (accessible from other devices)
wab-viewer ./output --host 0.0.0.0

# Start server without opening a browser (e.g. headless or remote machine)
wab-viewer ./output --no-browser

# Rebuild the cache
wab-viewer ./output --rescan

# Enable verbose HTTP request logging (default is error only)
wab-viewer ./output --verbose

# Check the installed version
wab-viewer --version
```

By default, routine HTTP request logs (`GET /api/... 200`) are silenced to keep the console clean and focused on startup, cache, and error messages. Pass `--verbose` (or `-v`) to enable HTTP request logging for debugging.

## How It Works

On first run (or with `--rescan`), the viewer builds a cache DB (`.wa_chat_viewer_cache.db`) combining:

1. **Source WhatsApp database** (`msgstore.db` for Android, `ChatStorage.sqlite` for iOS) — timestamps, sender names, and text message bodies
2. **Archive database** (`.wa_media_archiver.db`) — maps original media paths to archived file locations
3. **File mtime fallback** — used when no source WA database is present (media-only mode)

The source WA database must be present for the viewer to resolve media file paths. If you archived without keeping it, only timestamps from file modification times are available.

## Features

- **Sidebar** — all contacts and groups listed with message counts and last activity time
- **Chat timeline** — messages displayed chronologically (oldest at top, newest at bottom), lazy-loaded as you scroll
- **Full-text search** — search across all messages; scoped to the current chat or across all chats
- **Media playback** — images, videos, and audio stream directly in the browser with HTTP Range support (seeking works in video/audio players)
- **Image lightbox** — click any image to open it full-screen
- **DOM windowing** — only ~100 message elements kept in the DOM at once, so long chats stay fast

## Cache Invalidation

The cache is rebuilt automatically when:

- You pass `--rescan`
- The number of rows in `archive_copies` differs from the number of messages in the cache

## Privacy

The server binds to `127.0.0.1` by default — it is not accessible from other machines on your network. No data leaves your machine. To access the viewer from another device on your LAN, pass `--host 0.0.0.0` (make sure your firewall allows the port).

## Troubleshooting

**No chats appear in the sidebar**

- Verify `msgstore.db` (Android) or `ChatStorage.sqlite` (iOS) is in the `Whatsapp Databases` subfolder (or directly in the output directory) alongside `.wa_media_archiver.db`
- Run with `--rescan` to force a cache rebuild and check the console output for warnings

**Media files return 404**

- The source WA database must be present for the viewer to resolve `archive_path` from `original_path`
- If you archived without keeping the source DB, only file-mtime timestamps will be available (media-only mode)

**Text messages are missing**

- The source WA database is required to show text-only messages. Media messages with captions will show the caption but not text-only messages.

## Known Limitations

- **Initial release scope**: Call history, poll interactions/votes, and stickers are not yet rendered in the timeline.
- **Media gallery optimization**: Large media galleries load original files directly; optimized thumbnail caching and lazy loading optimizations are planned for future releases.
- **Profile pictures are not displayed.** On Android this requires extracting `wa.db` (not yet implemented). On iOS no local image data is available in the backup.
- **Friendly profile push names for unsaved contacts are not available on Android without root.** On Android, user-chosen friendly push names (`wa_name`) reside exclusively in `/data/data/com.whatsapp/databases/wa.db` within WhatsApp's private application sandbox. WhatsApp excludes contact records from local backups (`WhatsApp/Backups/wa.db.crypt14/15`), so extracting `wa.db` requires root access on the device, which is not implemented in the Viewer or Archiver. Unsaved contacts on Android will therefore display their phone number. On iOS, WhatsApp stores push names directly in `ChatStorage.sqlite` (`ZWAPROFILEPUSHNAME`), so they are preserved in standard backups.
- **iOS read and played receipts require `MessagingInfraDatabase.sqlite`.** On modern iOS backups (v2.24+), delivery, read, and voice message played timestamps are read directly from `MessagingInfraDatabase.sqlite`. For older backups or messages not recorded in it (such as those sent via web or companion devices), the viewer falls back to `ChatStorage.sqlite`'s `ZRECEIPTINFO`, which shows "Read (time not stored)" when a read state is confirmed without a timestamp.

---

## Navigation

- **Previous**: [Archiver Guide & Workflow](../archiver/index.md)
- **Home**: [Main Project Readme](../../README.md)

