# Chat Viewer

Browse and search your archived WhatsApp chats through a local web UI.

## Requirements

```bash
pip install flask
```

## Usage

```bash
python -m wab_viewer /path/to/archive
```

The viewer opens your browser at `http://127.0.0.1:5000`. Use `--rescan` to force a cache rebuild.

```bash
# Example
python -m wab_viewer ./output

# Custom port
python -m wab_viewer ./output --port 8080

# Rebuild the cache
python -m wab_viewer ./output --rescan
```

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

The server binds to `127.0.0.1` by default — it is not accessible from other machines on your network. No data leaves your machine.

## Troubleshooting

**No chats appear in the sidebar**

- Verify `msgstore.db` (Android) or `ChatStorage.sqlite` (iOS) is in the output directory alongside `.wa_media_archiver.db`
- Run with `--rescan` to force a cache rebuild and check the console output for warnings

**Media files return 404**

- The source WA database must be present for the viewer to resolve `archive_path` from `original_path`
- If you archived without keeping the source DB, only file-mtime timestamps will be available (media-only mode)

**Text messages are missing**

- The source WA database is required to show text-only messages. Media messages with captions will show the caption but not text-only messages.

## Known Limitations

- **Profile pictures are not displayed.** On Android this requires extracting `wa.db` (not yet implemented). On iOS no local image data is available in the backup.
- **iOS read timestamps absent for recent messages.** WhatsApp stopped storing read timestamps on-device around June 2026 — they are fetched live from servers instead. When a message is confirmed as read but no timestamp is on-device, the viewer shows "Read (time not stored)".
- **Played receipts are not shown** for voice messages and video messages. The timing data is encoded in the receipt protobuf but has not been verified against ground-truth data across both platforms, so it is intentionally omitted for now.
