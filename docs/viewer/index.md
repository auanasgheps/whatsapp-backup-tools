[← Back to Main Readme](../../README.md) • [Archiver Guide](../archiver/index.md)

# WhatsApp Backup Viewer (`wab-viewer`)

Browse and search your archived WhatsApp chats offline through a local web UI replicating WhatsApp Web.

**Whatsapp Backup Viewer screenshots**

<p float="left">
  <img src="/docs/images/viewer_dark.png" width="700" />
  <img src="/docs/images/viewer_clean.png" width="700" /> 
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

# Serve under a sub-path behind a reverse proxy (also via WAB_BASE_PATH)
wab-viewer ./output --base-path /whatsapp

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

The source WhatsApp database (`msgstore.db` or `ChatStorage.sqlite`) is strictly required for the viewer to launch and resolve chat messages and media paths.

### Storing Media on External Storage (Symlinks & Junctions)

If you have a large archive and prefer to keep heavy media folders (`Contacts/` and `Groups/`) on an external drive or secondary disk while keeping database files on your main drive, you can link them into your archive folder:

- **Linux & macOS (Symbolic Links)**:
  ```bash
  ln -s /path/to/external/Contacts /path/to/archive/Contacts
  ln -s /path/to/external/Groups /path/to/archive/Groups
  ```
- **Windows (NTFS Directory Junctions - no admin rights required)**:
  ```cmd
  mklink /J "C:\path\to\archive\Contacts" "D:\WhatsApp\Contacts"
  mklink /J "C:\path\to\archive\Groups" "D:\WhatsApp\Groups"
  ```

`wab-viewer` automatically recognizes whitelisted `Contacts/` and `Groups/` links and serves full-size photos, videos, and audio securely from external storage.

## Features

- **Sidebar** — all contacts and groups listed with message counts and last activity time
- **Chat timeline** — messages displayed chronologically (oldest at top, newest at bottom), lazy-loaded as you scroll
- **Interactive WhatsApp polls** — poll cards rendered with options, vote count bars, percentages, checkmarks, and an interactive "View votes" popup detailing voter identities (Android & iOS)
- **Revoked & view-once badges** — "This message was deleted" tombstones and View-Once badges for ephemeral media
- **Full-text & chat list search** — search across chats and messages with instant query matching, displaying formatted international phone numbers for individual contacts, group descriptions (or "Group chat") for groups, and full-text keyword matching across message bodies
- **Media & animated GIF playback** — images, videos, looping animated GIFs, and audio stream directly in the browser with HTTP Range support, interactive click-to-play GIF badges, and video/audio seeking
- **Circular video notes (Push-to-Video)** — round video messages render in authentic circular bubbles with duration rings, play/pause controls, and standalone positioning (Android & iOS)
- **Image lightbox** — click any image to open it full-screen with smooth zoom and pan
- **Media gallery & stickers** — browse chat media by thumbnail grid or folder tree, with dedicated stickers section and contact cards (.vcf)
- **Fast, lightweight scrolling** — chats and massive media collections load smoothly on demand without slowing down your browser
- **Message & chat preservation** — messages deleted on the device ("Delete for Me"), messages revoked by sender ("Delete for Everyone"), or chats entirely deleted from the device are seamlessly preserved and badged when archiving with `-P` / `--preserve-deleted`
- **Edit history progressive disclosure** — edited messages retain their pre-edited original draft; click the "Edited" badge or "ⓘ" message details button to inspect the original text and edit timestamp
- **Interactive reply jumping** — reply bubbles accurately jump to original messages across both live and preserved conversations
- **Shared location cards** — shared location pins render with venue names, addresses, and direct Google Maps links

## Cache Invalidation

The cache is rebuilt automatically when:

- You pass `--rescan`
- The number of rows in `archive_copies` differs from the number of messages in the cache

## Privacy

The server binds to `127.0.0.1` by default — it is not accessible from other machines on your network. No data leaves your machine. To access the viewer from another device on your LAN, pass `--host 0.0.0.0` (make sure your firewall allows the port). Behind a reverse proxy that publishes the viewer under a sub-path, pass that path with `--base-path` (or `WAB_BASE_PATH`): the proxy can forward the prefix as it is or strip it, both work.

## Troubleshooting

**No chats appear in the sidebar**

- Verify `msgstore.db` (Android) or `ChatStorage.sqlite` (iOS) is in the `Whatsapp Databases` subfolder (or directly in the output directory) alongside `.wa_media_archiver.db`
- Run with `--rescan` to force a cache rebuild and check the console output for warnings

**Media files return 404**

- The source WA database must be present in the archive directory (`Whatsapp Databases/` or root) for the viewer to resolve `archive_path` from `original_path`
- Verify that the archived media files have not been moved or renamed after archiving

**Text messages are missing**

- Ensure you have not excluded message tables or run with a corrupted WhatsApp database. The viewer requires a valid `msgstore.db` or `ChatStorage.sqlite` to load messages.

## Known Limitations

- **Profile pictures are not displayed.** On Android this requires extracting `wa.db` (not yet implemented). On iOS no local image data is available in the backup.
- **Friendly profile push names for unsaved contacts are not available on Android without root.** On Android, user-chosen friendly push names (`wa_name`) reside exclusively in `/data/data/com.whatsapp/databases/wa.db` within WhatsApp's private application sandbox. WhatsApp excludes contact records from local backups (`WhatsApp/Backups/wa.db.crypt14/15`), so extracting `wa.db` requires root access on the device, which is not implemented in the Viewer or Archiver. Unsaved contacts on Android will therefore display their phone number. On iOS, WhatsApp stores push names directly in `ChatStorage.sqlite` (`ZWAPROFILEPUSHNAME`), so they are preserved in standard backups.
- **iOS read and played receipts require `MessagingInfraDatabase.sqlite`.** On modern iOS backups (v2.24+), delivery, read, and voice message played timestamps are read directly from `MessagingInfraDatabase.sqlite`. For older backups or messages not recorded in it (such as those sent via web or companion devices), the viewer falls back to `ChatStorage.sqlite`'s `ZRECEIPTINFO`, which shows "Read (time not stored)" when a read state is confirmed without a timestamp.
- **Recent Android link and media previews**: If messages were imported from WhatsApp's daily incremental backups, link previews and quoted media thumbnails will not show an image preview yet. WhatsApp omits image previews from these small updates to save mobile data. They will appear automatically once WhatsApp runs its next full base backup on your phone (typically created every 5 to 7 days, or via a manual backup in WhatsApp settings) and you update your archive, as the viewer operates completely offline and never connects to the internet to download missing files.

---

## Navigation

- **Previous**: [Archiver Guide & Workflow](../archiver/index.md)
- **Home**: [Main Project Readme](../../README.md)

