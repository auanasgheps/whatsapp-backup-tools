[← Back to Main Readme](../../README.md) • [Archiver Guide](../archiver/index.md) • [Viewer Guide](../viewer/index.md)

# WhatsApp Backup Sweeper (`wab-sweeper`)

**`wab-sweeper`** is an Android WhatsApp media management and precision restitution CLI. It safely reclaims storage space on your Android device via ADB by removing media that has already been verified in your `wab-archiver` archive—while letting you restore swept files back to their exact original paths on your phone at any time.

> ⚠️ **Android Only**: `wab-sweeper` operates on live Android devices via ADB. iOS backups are read-only local snapshots and are not supported for sweeping.

---

## Core Design Principles

- **Zero-Loss Verification**: A file on your phone is never deleted unless it passes the 4-Gate Integrity Pipeline confirming a healthy, matching copy exists in your local archive.
- **Safe by Default**: `sweep`, `restore`, and `undo` all run in **dry-run preview mode** by default. Device mutations require `--execute` (and typed confirmation for `sweep`).
- **Archive Read-Only Guarantee**: `.wa_media_archiver.db` and `msgstore.db` are opened strictly in SQLite read-only mode (`?mode=ro`). Sweeper state lives separately in `.wab-sweeper.db`.
- **Archiver Isolation**: `wab-archiver` remains strictly read-only toward your phone; `wab-sweeper` is the dedicated storage manager.

---

## Prerequisites

- An existing archive created by `wab-archiver` containing `.wa_media_archiver.db` and a decrypted `msgstore.db`.
- **ADB (Android Debug Bridge)** installed and your Android device connected and authorized (see [Prerequisites](../prerequisites.md)).
- Supports both standard **WhatsApp** (`com.whatsapp`) and **WhatsApp Business** (`com.whatsapp.w4b`), auto-detected from your archive, as well as secondary user profiles such as Samsung Dual Messenger / Dual Apps (`android_user_id = 999`).

---

## Quick Start Workflow

1. **Discover chats (optional, for single-chat sweeps)**:
   ```bash
   wab-sweeper list-chats --archive /path/to/archive --search "Family"
   ```
   This displays matching chats, their media counts, and ready-to-paste `[sweep.chat]` TOML snippets.

2. **Generate a configuration template**:
   ```bash
   wab-sweeper init-config
   ```
   Edit the generated `config.toml` to point `archive` to your archive directory and set your desired age, size, or chat filters.

3. **Run a dry-run preview**:
   ```bash
   wab-sweeper sweep --config config.toml
   ```
   Review the summary breakdown of eligible files and protected/skipped items (also written to `unprotected_skipped_report_<timestamp>.csv`).

4. **Execute the sweep**:
   ```bash
   wab-sweeper sweep --config config.toml --execute
   ```
   Inspect the final summary and type `CONFIRM` (or `CONFIRM-SHARED` if sweeping shared cross-chat media) to proceed.

---

## Configuration Reference (`config.toml`)

Generate an annotated starter file with `wab-sweeper init-config`.

```toml
[general]
archive = "C:/WhatsApp_Archive"   # Path to archive containing .wa_media_archiver.db & msgstore.db
dry_run = true                    # Always defaults to true for safety
log = "wab_sweeper.log"           # Optional log file path
# device = "emulator-5554"        # Optional: target specific ADB serial if multiple connected
android_user_id = 0               # 0 for primary user, 999 for Samsung Dual Messenger / Dual Apps

[sweep]
# Time policies (mutually exclusive)
older_than = "2y"                 # Formats: 180d, 6m, 2y, or YYYY-MM-DD
# keep_last = "1y"

# Size & Quota policies
# target_free = "10GB"            # Sweeps eligible files until 10GB reclaimed
sort_by = "oldest"                # "oldest" (default) or "largest" when target_free is set
# min_file_size = "20MB"          # Only sweep bulky files (e.g., large videos)

# Category filters (voice_note included by default)
include_types = ["image", "video", "audio", "voice_note", "document"]
# exclude_types = ["voice_note"]  # Uncomment to protect voice recordings

# Safety whitelisting
protect_starred = true            # NEVER sweep starred media
exclude_sent = false              # Set to true to keep all outbound media
sweep_preserved = false           # Keep forensic/revoked messages safe (Default: false)

# Targeted chat (OPTIONAL — omit to sweep globally across all chats; max 1 chat if set)
# [sweep.chat]
# id = "491701234567@s.whatsapp.net"
# name = "Example Contact"
# sweep_shared = false            # Set to true to override cross-chat protection
```

### `[general]` Options

| Option | Type | Default | Description |
| :--- | :--- | :--- | :--- |
| `archive` | string | *(required)* | Path to the archive folder containing `.wa_media_archiver.db` and `msgstore.db`. |
| `dry_run` | boolean | `true` | Safety default; device deletion/restoration still requires `--execute` on the CLI. |
| `log` | string | `None` | Optional path to write detailed debug/audit logs. |
| `device` | string | `None` | Optional ADB device serial. If omitted, auto-binds to the serial recorded in `.wa_media_archiver.db` or the single connected device. |
| `android_user_id` | integer | `0` | Android user profile ID (`0` for primary, `999` for Samsung Dual Messenger / Dual Apps). |

### `[sweep]` Options

| Option | Type | Default | Description |
| :--- | :--- | :--- | :--- |
| `older_than` | string | `None` | Sweep media older than a duration (`180d`, `6m`, `2y`) or `YYYY-MM-DD` date. Mutually exclusive with `keep_last`. |
| `keep_last` | string | `None` | Keep media newer than a duration (`180d`, `6m`, `2y`) or `YYYY-MM-DD` date and sweep older files. Mutually exclusive with `older_than`. |
| `target_free` | string | `None` | Stop sweeping once this amount of space is reclaimed (e.g. `"10GB"`, `"500MB"`). |
| `sort_by` | string | `"oldest"` | Order used when `target_free` is set: `"oldest"` or `"largest"`. |
| `min_file_size` | string | `None` | Only sweep files at least this large (e.g. `"20MB"`). |
| `include_types` | list | `["image", "video", "audio", "voice_note", "document"]` | Media categories eligible for sweeping. Valid values: `image`, `video`, `audio`, `voice_note`, `video_note`, `document`, `sticker`. |
| `exclude_types` | list | `[]` | Media categories to subtract from `include_types` (e.g. `["voice_note"]`). |
| `protect_starred` | boolean | `true` | Never sweep media from starred messages. |
| `exclude_sent` | boolean | `false` | When `true`, keeps all outbound (`from_me`) media on the device. |
| `sweep_preserved` | boolean | `false` | When `false`, protects media referenced by `preserved_messages.db`. |

### `[sweep.chat]` Options (Optional)

Omit `[sweep.chat]` to sweep globally across all chats. When set, at most **one** chat may be targeted:

| Option | Type | Default | Description |
| :--- | :--- | :--- | :--- |
| `id` | string | *(required)* | Canonical chat JID (copy from `wab-sweeper list-chats`). |
| `name` | string | *(required)* | Display name matching the archive (Safety Handshake). |
| `sweep_shared` | boolean | `false` | When `false`, skips media also referenced by other chats. |

---

## CLI Command Reference

### `wab-sweeper list-chats`
Discover chats with media in the archive and generate `[sweep.chat]` config snippets.

```bash
wab-sweeper list-chats --archive /path/to/archive [--search "substring"]
```

- `--archive` *(required)*: Path to the archive directory.
- `--search`: Case-insensitive substring filter on chat name or JID.

### `wab-sweeper init-config`
Write an annotated default `config.toml` template.

```bash
wab-sweeper init-config [--output config.toml] [--force]
```

- `--output`: Destination TOML file path (default: `config.toml`).
- `--force`: Overwrite an existing file.

### `wab-sweeper sweep`
Scan the device, run the 4-Gate Integrity Pipeline, and optionally delete verified media.

```bash
wab-sweeper sweep --config config.toml [--execute] [--yes] [--device SERIAL] [--sweep-shared] [--sweep-preserved] [--verbose]
```

- `--config` *(required)*: Path to `config.toml`.
- `--execute`: Perform actual deletions on the device (default is dry-run).
- `--yes`, `-y`: Skip interactive `CONFIRM` / `CONFIRM-SHARED` prompt.
- `--device`: Override the target ADB device serial.
- `--sweep-shared`: Override `sweep_shared = true` for the targeted chat.
- `--sweep-preserved`: Override `sweep_preserved = true` to allow sweeping preserved/revoked media.
- `--verbose`, `-v`: Enable debug logging.

### `wab-sweeper restore`
Push archived media matching `config.toml` back to the Android device if it is currently missing on the phone.

```bash
wab-sweeper restore --config config.toml [--execute] [--yes] [--device SERIAL] [--verbose]
```

- `--config` *(required)*: Path to `config.toml`.
- `--execute`: Actually push files to the device (default is dry-run).
- `--yes`, `-y`: Skip interactive confirmation.
- `--device`: Override the target ADB device serial.
- `--verbose`, `-v`: Enable debug logging.

### `wab-sweeper undo`
Idempotently roll back the most recent completed sweep recorded in `.wab-sweeper.db`.

```bash
wab-sweeper undo --last [--archive /path/to/archive] [--config config.toml] [--execute] [--device SERIAL] [--android-user-id 0] [--verbose]
```

- `--last` *(required)*: Undo the most recent sweep run.
- `--archive`: Path to the archive directory (default: `.`).
- `--config`: Optional `config.toml` to resolve `archive`, `log`, `device`, and `android_user_id`.
- `--execute`: Actually push files back to the device (default is dry-run).
- `--device`: Target ADB device serial.
- `--android-user-id`: Android user ID (default: `0`).
- `--verbose`, `-v`: Enable debug logging.

---

## How Safety Works

### The 4-Gate Integrity Pipeline
Every candidate file on your phone must pass four sequential checks before it can be swept:

1. **Gate 1 — Cataloged in Archive DB**: The file's relative path must exist in `.wa_media_archiver.db` (`files` and `archive_copies`). Files newer than the archive's latest message timestamp are flagged as `post_archive_cutoff` and skipped.
2. **Gate 2 — Local Physical Durability**: At least one archived copy must physically exist on disk with non-zero size.
3. **Gate 3 — Remote Confirmation & Policy Filters**: The file must be present on the device via ADB and pass all policy checks (`protect_starred`, `exclude_sent`, `sweep_preserved`, and cross-chat `sweep_shared`).
4. **Gate 4 — Content Invariance (EXIF-Aware)**:
   - If the archived copy was not modified by `--exif-timestamps` (`exif_stamped = 0`), local and remote byte sizes must match.
   - The remote file's MD5 hash on the device must match `files.md5` (the original source MD5 captured at archive time).

Any file failing a gate or protection rule is dropped from the sweep queue and recorded in `unprotected_skipped_report_<timestamp>.csv`.

### The Safety Handshake (Single-Chat Mode)
When `[sweep.chat]` is configured, `wab-sweeper` validates that `id` maps to `name` in the archive (using Unicode NFC normalization). If there is a mismatch, execution aborts immediately before touching the device.

### Cross-Chat Media Protection
WhatsApp Android stores forwarded media once on disk and references that single file from multiple chats. When sweeping a single chat with `sweep_shared = false` (default), any file also referenced by another chat is skipped and protected. Enabling `sweep_shared = true` (or `--sweep-shared`) lists all affected chats and escalates the confirmation prompt to `CONFIRM-SHARED`.

### System Files & Active Backup Guard
- **Protected Paths**: `.nomedia`, `.thumbs`, `.Shared`, `.Statuses/`, and the 8 root media folders (`WhatsApp Images`, `WhatsApp Video`, etc.) are never deleted. Empty leaf folders are pruned safely with `rmdir`, which refuses to remove directories containing `.nomedia`.
- **Active Backup Guard**: Before mutating device files, `wab-sweeper` checks whether a WhatsApp backup service is currently running on the device and aborts if one is active.

---

## Restore vs. Undo

| Feature | `wab-sweeper restore --config config.toml` | `wab-sweeper undo --last` |
| :--- | :--- | :--- |
| **Source of Truth** | `.wa_media_archiver.db` + `msgstore.db` | `.wab-sweeper.db` (`adb_sweep_state`) |
| **Scope** | Pushes **any** archived file matching `config.toml` filters that is missing on the phone—even if deleted outside `wab-sweeper`. | Reverses **only** the files deleted in the most recent `sweep_id` (`undone_at IS NULL`). |
| **Idempotency** | Skips files already on the device with matching MD5. | Marks restored entries with `undone_at` so repeated runs never re-push already undone files. |

Both `restore` and `undo` enforce:
- **Free Space Buffer**: Verifies the device has at least `batch_size + 1.0 GiB` free before pushing files.
- **Permissions & Timestamps**: Applies `chmod 664` and restores original `mtime` timestamps so restored photos appear in chronological order in your phone's gallery.
- **Collision Protection**: If a different file already exists at the target path on the phone, the existing file is backed up to `<filename>_conflict_<timestamp>.bak` and logged to `restore_conflicts_<timestamp>.csv`.

---

## State & Audit Reports

All state and audit artifacts are written to your archive directory:

- **`.wab-sweeper.db`**: Tracks staged, deleted, failed, and undone sweep entries as well as restore batches, allowing interrupted runs to resume cleanly.
- **`swept_media_report_<timestamp>.csv`**: Log of all files deleted during an executed sweep (`remote_path`, `archive_path`, `file_size_bytes`, `file_hash`, `chat_jid`, `message_timestamp`, `shared_chats`).
- **`unprotected_skipped_report_<timestamp>.csv`**: Breakdown of files skipped by safety gates or protection rules.
- **`restore_conflicts_<timestamp>.csv`**: Any existing device files backed up before replacement during `restore` or `undo`.

---

## Known Limitations

- **iOS is not supported**: Apple locks down app storage on iPhones and iPads, so external tools cannot delete or restore individual WhatsApp media files directly on the device. Because iOS backups on your computer are read-only snapshots rather than live phone storage, `wab-sweeper` only works with Android devices via ADB.
- **Stale archive warning**: If the latest message in your archive's `msgstore.db` is more than 7 days old, `wab-sweeper` emits a warning recommending that you run `wab-archiver` first so recent media is archived before sweeping.

---

## Navigation

- **Previous**: [Viewer Guide](../viewer/index.md)
- **Home**: [Main Project Readme](../../README.md)
