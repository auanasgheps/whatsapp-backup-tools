[← Back to Archiver Overview](index.md) • [CLI Reference](command-reference.md) • [Configuration](configuration.md) • [Main Readme](../../README.md)

# CLI Recipes, Workflows & Restore

Practical recipes and copy-paste examples for common archiving scenarios, advanced workflows, and data recovery.

> 💡 You do not have to use these: it's only a reference manual for advanced users. 
For day-to-day usage and configuration, see **[Configuration Guide](configuration.md)**.

> 💡 The examples below use line continuation characters. Replace them with the character for your shell:
>
> | Shell | Continuation Character |
> |---|---|
> | bash / zsh (Linux, macOS) | `\` |
> | PowerShell (Windows) | `` ` `` |
> | cmd.exe (Windows) | `^` |

---

## Android Recipes

### 1. ADB Mode — Automatic Pull & Decrypt (Recommended)

Automatically pulls `msgstore.db.crypt15`, daily increments, and contacts over USB, then decrypts them using your 64-hex key:

```bash
wab-archiver archive \
  --from-adb \
  --wa-root /path/to/WhatsApp \
  --e2e-key 1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a6b7c8d9e0f1a2b \
  --output /path/to/output \
  --dry-run
```

### 2. ADB Mode — Also Pull Media Over USB

Transfers media directly from device storage over USB into a local staging folder:

```bash
wab-archiver archive \
  --from-adb \
  --e2e-key 1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a6b7c8d9e0f1a2b \
  --pull-media \
  --staging /path/to/wa-staging \
  --output /path/to/output
```

> 💡 `--staging` maintains a permanent local copy of your device storage (acting as a phone backup). The archiver treats it as `--wa-root` and copies media into `--output`; files in `--staging` are never deleted.
>
> ⚠️ On first run without `--since`, all media is transferred over USB, which can take time for large collections. Subsequent runs skip already-archived files. Use `--since YYYY-MM-DD` to restrict transfer to recent media.

### 3. Manual — Decrypted Database

When you already have a decrypted `msgstore.db` and local media folder:

```bash
wab-archiver archive \
  --msgstore /path/to/msgstore.db \
  --wa-root /path/to/WhatsApp \
  --output /path/to/output \
  --contacts /path/to/wa_contacts \
  --dry-run
```

### 4. Manual — Encrypted Database (`.crypt15`)

When you have a `.crypt15` file and want the archiver to handle decryption:

```bash
wab-archiver archive \
  --msgstore /path/to/msgstore.db.crypt15 \
  --e2e-key 1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a6b7c8d9e0f1a2b \
  --wa-root /path/to/WhatsApp \
  --output /path/to/output \
  --dry-run
```

> 💡 Even in dry run mode, decryption still occurs so the database can be queried. The decrypted `msgstore.db` is saved into `Whatsapp Databases/` in your output folder.

### 5. Manual — Multiple Source Folders

When media is split across multiple directories (e.g. an old archive drive and a current phone sync folder), repeat `--wa-root`:

```bash
wab-archiver archive \
  --msgstore /path/to/msgstore.db \
  --wa-root /path/to/old-archive/WhatsApp \
  --wa-root /path/to/current-phone/WhatsApp \
  --output /path/to/output \
  --dry-run
```

> 💡 When the same file exists in multiple roots, the largest copy is chosen (higher quality heuristic). If sizes match but content differs, the first root takes priority and the conflict is logged in `source_conflicts_report.csv`.

---

## iOS Recipes

### 1. Standard Flow — Unencrypted iPhone Backup

```bash
wab-archiver archive \
  --ios-backup ~/Library/Application\ Support/MobileSync/Backup/<UDID> \
  --output /path/to/output \
  --dry-run
```

### 2. Standard Flow — Encrypted iPhone Backup

```bash
wab-archiver archive \
  --ios-backup ~/Library/Application\ Support/MobileSync/Backup/<UDID> \
  --ios-password your_backup_password \
  --output /path/to/output \
  --dry-run
```

> 💡 `--wa-root` and `--contacts` are not needed for iOS. The archiver automatically extracts `ChatStorage.sqlite` and `ContactsV2.sqlite` directly from the iPhone backup.

### 3. Pre-extracted Mode

For advanced setups where the WhatsApp app container has already been extracted:

```bash
wab-archiver archive \
  --msgstore /path/to/ChatStorage.sqlite \
  --wa-root /path/to/AppDomainGroup-group.net.whatsapp.WhatsApp.shared \
  --ios-contacts /path/to/ContactsV2.sqlite \
  --output /path/to/output
```

---

## Advanced Archiving Features

### 1. Test Runs (`--since`, `--limit`)

Preview recent activity without scanning years of history:

```bash
wab-archiver archive \
  --msgstore /path/to/msgstore.db \
  --wa-root /path/to/WhatsApp \
  --output /path/to/output \
  --since 2025-01-01 \
  --limit 100 \
  --dry-run
```

### 2. Deduplicate with Hardlinks (`--link-duplicates`)

When media (photos, videos, audio, documents) is shared across multiple chats or groups, `--link-duplicates` creates filesystem hardlinks for duplicate copies instead of duplicating file bytes. This saves disk space while maintaining independent folder paths for each chat:

```bash
wab-archiver archive \
  --config config.toml \
  --link-duplicates
```

> 💡 The first occurrence of a file is archived as the canonical copy. Any subsequent duplicate points to the existing file on disk via a hardlink. If the destination filesystem (e.g. FAT32, exFAT) or network share does not support hardlinks, the archiver automatically falls back to standard file copying.
>
> ⚠️ **File Modification Timestamps:** Because filesystem hardlinks share the same physical record on disk, updating a hardlinked file's modification time updates it across all linked paths. When duplicate media is forwarded across different dates, the file modification timestamp (`mtime`) on disk reflects the most recent message. To preserve the exact date context for every copy, the archiver records the original message timestamp of each duplicate in `.wa_media_archiver.db` and exports it in the `original_timestamp` column of `duplicate_media_report.csv`.

### 3. Preserve Deleted Messages (`-P`, `--preserve-deleted`)

By default, the archive reflects the state of your current backup database. If you delete messages or chats on your phone between backup runs, passing `-P` / `--preserve-deleted` retains missing items in `preserved_messages.db`:

```bash
wab-archiver archive \
  --config config.toml \
  --preserve-deleted
```

> 💡 Preserved messages, sender-revoked messages ("Delete for Everyone"), voice/video calls (`preserved_calls`), interactive polls, shared locations, view-once media metadata, and pre-edited message drafts are retained into `preserved_messages.db` across backup cycles. Preserved content and deleted chats are automatically surfaced in the viewer with visual badges, progressive edit disclosure, and bidirectional reply linking.

### 4. Write EXIF & Container Timestamps (`--exif-timestamps`)

By default, the archiver sets filesystem modification timestamps (`mtime`) on all copied files to match the original WhatsApp message date. However, cloud galleries and photo management software (such as **Google Photos, Apple Photos, Immich, Nextcloud Memories, Synology Photos, and Adobe Lightroom**) prioritize embedded metadata tags over filesystem dates when building chronological timelines.

Enable `--exif-timestamps` (or set `exif_timestamps = true` in `config.toml`) to inject timestamps directly into media metadata headers:

```bash
wab-archiver archive \
  --config config.toml \
  --exif-timestamps
```

> 💡 **Metadata Engine Details:**
> - **Images (JPEG)**: Adds standard EXIF `DateTimeOriginal` and `DateTimeDigitized` tags (in local time) alongside `OffsetTimeOriginal` when a timezone is configured. Pre-existing camera capture dates and portrait orientation tags are strictly preserved.
> - **Videos (MP4 / MOV)**: Adds QuickTime `mvhd`, `tkhd`, and `mdhd` creation and modification timestamps in UTC.
> - **Zero Re-compression**: Operates with 100% loss-free header manipulation; pixel data and video streams are never transcoded.
> - **Hardlink Protection**: Automatically skipped on hardlinked files (`--link-duplicates`) to prevent shared inodes from cross-mutating timestamps.

### 5. Retroactively Backfill Metadata on Existing Archives (`update-metadata`)

If you previously archived media without `--exif-timestamps`, you can inject missing EXIF and container timestamps retroactively without re-importing or needing your device:

```bash
wab-archiver update-metadata \
  --output /path/to/output \
  --timezone Europe/Berlin
```

- **Decoupled from device/sources**: Operates entirely from `.wa_media_archiver.db` without needing `--wa-root`, `--msgstore`, or ADB.
- **Simulate first**: Pass `--dry-run` to preview how many files would be stamped without writing to disk.
- **Force re-evaluation**: Pass `--force` if you changed timezone settings and want to re-evaluate all files.
- **Fast and idempotent**: Tracks stamping state in `.wa_media_archiver.db` (`exif_stamped` column); subsequent runs skip already-stamped files in milliseconds.

---

## Restore Mode

Restore mode reconstructs the flat `WhatsApp/Media/` folder structure and restores the latest WhatsApp database (`Databases/msgstore.db`) directly inside the archive folder, without needing the original device or backup:

```bash
wab-archiver restore --output /path/to/output
```

> ⚠️ **Android archives only.** iOS media cannot be restored this way.
>
> 💡 **Need to push media directly back to a connected Android phone?** Use [`wab-sweeper restore`](../sweeper/index.md#wab-sweeper-restore) for filtered, resumable ADB transfers back to the device. `wab-archiver restore` is for offline local reconstruction of the full `Media/` tree and `Databases/msgstore.db` on disk.

The reconstructed tree is written to `<output>/Media/` and `<output>/Databases/msgstore.db`, alongside the existing `Contacts/` and `Groups/` folders. No files are overwritten — identical files already in place are skipped silently.

### How It Works

During every forward archiving run, the archiver records each original `file_path` and its corresponding archive location in `.wa_media_archiver.db`. Restore mode reads this index and copies each unique file back to its original relative path. If the same file was archived from multiple chats, it is restored exactly once.

### Behavior & Limitations

- **Physical Copies**: Restored files are reconstructed as independent physical copies (never hardlinks) to faithfully recreate the original phone storage layout.
- **Database Restoration**: The latest WhatsApp database (`msgstore.db`) is automatically restored to `<output>/Databases/msgstore.db`.
- **Contact Cards**: Virtual contact cards (`.vcf`) are ignored during restore mode since they were generated from database records rather than files on the phone.
- **Timestamps**: Restored file timestamps reflect the WhatsApp message timestamp (same as in the archive).
- **Filtered Runs**: If a forward run was filtered with `--since` or `--limit`, the restore index only covers media that was actually archived.

---

## Next Steps

- 📖 **[CLI Command Reference](command-reference.md)** — Comprehensive options and flag reference tables.
- ⚙️ **[Configuration Guide](configuration.md)** — Configure reusable runs with `config.toml`.
- 🌐 **[WhatsApp Backup Viewer Guide](../viewer/index.md)** — Explore your archived chats offline.
