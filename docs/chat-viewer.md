# Chat Viewer

Browse and search your archived WhatsApp chats through a local web UI.

## Requirements

```bash
pip install flask
```

## Usage

```bash
python wa_chat_viewer.py <output_root> [--port PORT] [--host HOST] [--rescan]
```

- `<output_root>` — path to the archive output directory (contains `.wa_media_archiver.db`)
- `--port` — port to listen on (default: `5000`)
- `--host` — host to bind to (default: `127.0.0.1`)
- `--rescan` — force rebuild of the viewer cache DB

The script opens your default browser automatically.

```bash
# Example
python wa_chat_viewer.py ./output

# Custom port
python wa_chat_viewer.py ./output --port 8080

# Rebuild the cache
python wa_chat_viewer.py ./output --rescan
```

## What it does

On first run (or with `--rescan`), the viewer scans the archive output directory and builds a **viewer cache DB** (`.wa_chat_viewer_cache.db`) by combining:

1. **Source WA database** (`msgstore.db` for Android, `ChatStorage.sqlite` for iOS) — provides timestamps, sender names, and text message bodies
2. **Archive database** (`.wa_media_archiver.db`) — maps original media paths to archived file locations
3. **File mtime fallback** — used when no source WA database is present (media-only mode)

## Features

- **Sidebar** — all contacts and groups listed with message counts and last activity time
- **Chat timeline** — messages displayed chronologically (oldest at top, newest at bottom), lazy-loaded as you scroll
- **Full-text search** — search across all messages; scoped to the current chat or across all chats
- **Media playback** — images, videos, and audio stream directly in the browser with HTTP Range support (seeking works in video/audio players)
- **Image lightbox** — click any image to open it full-screen
- **DOM windowing** — only ~100 message elements kept in the DOM at once, so long chats stay fast

## Cache invalidation

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
