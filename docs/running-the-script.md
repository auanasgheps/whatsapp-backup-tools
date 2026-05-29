
# Running the Script

## Command Reference

```
usage: wa_media_archiver.py [-h]
                      [-msg MSGSTORE]
                      [-e2e E2E_KEY]
                      [-c CONTACTS]
                      [-wa WA_ROOT]
                      [--ios_backup PATH]
                      [--ios_contacts PATH]
                      [--business]
                      -o OUTPUT
                      [-l LOG]
                      [-mode {adb,restore}]
                      [--dry-run]
                      [--limit N]
                      [--since DATE]
```

| Argument | Required | Description |
|---|---|---|
| `-msg` / `--msgstore` | No | Path to `msgstore.db`, `msgstore.db.crypt15`, or `ChatStorage.sqlite`. Not needed with `--ios_backup`. Defaults to `msgstore.db` in the current folder |
| `-e2e` / `--e2e_key` | If encrypted | Your cryptographic key for `.crypt15` decryption |
| `-c` / `--contacts` | No | Path to the `wa_contacts` file exported via ADB (Android only) |
| `-wa` / `--wa_root` | Android / iOS pre-extracted | Root path of your WhatsApp folder. Android: folder containing `Media/`. iOS pre-extracted: `AppDomainGroup-group.net.whatsapp.WhatsApp.shared` folder. Not required with `--ios_backup` or `--mode restore` |
| `--ios_backup` | iOS (recommended) | Path to the iPhone backup directory (the folder containing `Manifest.db`). Mutually exclusive with `--wa_root` |
| `--ios_contacts` | No | Path to `ContactsV2.sqlite` for iOS contacts. Auto-extracted from `--ios_backup` if omitted |
| `--business` | No | Target **WhatsApp Business** instead of the regular WhatsApp app. Affects the ADB pull path (Android `--mode adb`) and the backup domain (iOS `--ios_backup`). Not needed when using `--wa_root` — just point it at the `WhatsApp Business/` folder |
| `-o` / `--output` | **Yes** | Destination folder for the archive |
| `-l` / `--log` | No | Custom log file path. Defaults to `<output>/wa_media_archiver.log` |
| `-mode` / `--mode` | No | `adb` = automatically pull msgstore and contacts from a connected Android device; `restore` = reconstruct original `Media/` tree from the archive (Android archives only) |
| `--dry-run` | No | Simulate the run without copying any files |
| `--limit N` | No | Cap total rows returned across all chats. Useful for test runs |
| `--since DATE` | No | Only include messages on or after this date (`YYYY-MM-DD`). Combines freely with `--limit` |

---

## Usage Examples

#### Android — dry run first (always recommended)
```bash
python3 wa_media_archiver.py \
  --msgstore /path/to/msgstore.db \
  --wa_root /path/to/WhatsApp \
  --output /path/to/output \
  --contacts /path/to/wa_contacts \
  --dry-run
```

#### Android — full run
```bash
python3 wa_media_archiver.py \
  --msgstore /path/to/msgstore.db \
  --wa_root /path/to/WhatsApp \
  --output /path/to/output \
  --contacts /path/to/wa_contacts
```

#### iOS — standard flow (backup read directly)
```bash
python3 wa_media_archiver.py \
  --ios_backup ~/Library/Application\ Support/MobileSync/Backup/<UDID> \
  --output /path/to/output \
  --dry-run
```

> 💡 `--wa_root` and `--contacts` are not needed. The script extracts `ChatStorage.sqlite` and `ContactsV2.sqlite` directly from the backup.

#### iOS — full run
```bash
python3 wa_media_archiver.py \
  --ios_backup ~/Library/Application\ Support/MobileSync/Backup/<UDID> \
  --output /path/to/output
```

#### iOS — with explicit contacts file
```bash
python3 wa_media_archiver.py \
  --ios_backup ~/Library/Application\ Support/MobileSync/Backup/<UDID> \
  --ios_contacts /path/to/ContactsV2.sqlite \
  --output /path/to/output
```

#### iOS — pre-extracted mode (advanced)
```bash
python3 wa_media_archiver.py \
  --msgstore /path/to/ChatStorage.sqlite \
  --wa_root /path/to/AppDomainGroup-group.net.whatsapp.WhatsApp.shared \
  --ios_contacts /path/to/ContactsV2.sqlite \
  --output /path/to/output
```

#### Manual decryption (Android, pre-decrypted file)

```bash
# Step 1: decrypt manually
wadecrypt your_key msgstore.db.crypt15 msgstore.db

# Step 2: run the script
python3 wa_media_archiver.py \
  --msgstore /path/to/msgstore.db \
  --wa_root /path/to/WhatsApp \
  --output /path/to/output \
  --contacts /path/to/wa_contacts
```

#### Automatic decryption (script handles it)

```bash
python3 wa_media_archiver.py \
  --msgstore /path/to/msgstore.db.crypt15 \
  --e2e your_cryptographic_key \
  --wa_root /path/to/WhatsApp \
  --output /path/to/output \
  --contacts /path/to/wa_contacts
```

#### Dry run with encrypted database

```bash
python3 wa_media_archiver.py \
  --msgstore /path/to/msgstore.db.crypt15 \
  --e2e your_cryptographic_key \
  --wa_root /path/to/WhatsApp \
  --output /path/to/output \
  --dry-run
```

> 💡 Even in dry run mode, decryption still occurs so the DB can be queried. The decrypted `msgstore.db` is written to disk as a side effect regardless of `--dry-run`.

#### Test run — recent files only
```bash
python3 wa_media_archiver.py \
  --msgstore /path/to/msgstore.db \
  --wa_root /path/to/WhatsApp \
  --output /path/to/output \
  --since 2025-01-01 \
  --dry-run
```

#### Test run — recent files, capped row count
```bash
python3 wa_media_archiver.py \
  --msgstore /path/to/msgstore.db \
  --wa_root /path/to/WhatsApp \
  --output /path/to/output \
  --since 2025-01-01 \
  --limit 100 \
  --dry-run
```

#### Row limit only (balanced sample across all time)
```bash
python3 wa_media_archiver.py \
  --msgstore /path/to/msgstore.db \
  --wa_root /path/to/WhatsApp \
  --output /path/to/output \
  --limit 250 \
  --dry-run
```

#### Restore original Media/ folder structure
```bash
python3 wa_media_archiver.py \
  --mode restore \
  --output /path/to/output
```

> 💡 `--wa_root` is not required in restore mode. The script reads `.wa_media_archiver.db` from the archive and reconstructs `<output>/Media/` in place. Use `--dry-run` to preview what would be written.
> ⚠️ Restore mode is supported for **Android archives only**. Running it against an iOS archive exits with a clear error.

---

## Output Files

| File | Description |
|---|---|
| `wa_media_archiver.log` | Full run log including all copied, skipped, and missing files |
| `missing_media_report.csv` | Structured report of all media referenced in the DB but not found on disk. Useful for manual recovery from old backups |
| `duplicate_media_report.csv` | Report of media files with identical content at multiple archive paths (e.g. the same meme copied across multiple group chats). One row per path, sortable by `file_count` to find the most-shared content. Only written when duplicates exist |
| `.wa_media_archiver.db` | Single SQLite database storing all persistent state: contact folder index, group folder index, and the file archive map (original WhatsApp paths, content hashes as BLOB, and archive locations). Health checks (quick integrity scan, foreign key verification) run automatically on every open. Query-planner statistics refreshed with `ANALYZE` after each forward run. Do not delete unless you want to reset all tracking |
| `restore_report.csv` | Written by restore mode when issues are encountered. One row per problem: original path, which archive copy was used as source, and status (`unrestorable`, `collision_skipped`, `error`). Successfully restored and already-present identical files are not included. Not written if there are no issues |

---

## Restore Mode

Restore mode reconstructs the flat `WhatsApp/Media/` folder structure directly inside the archive folder, without needing the original device or database. This is useful when re-importing media into tools that expect the original WhatsApp layout.

> ⚠️ **Android archives only.** iOS media is stored at `Message/Media/...` paths that have no equivalent reconstruction target outside the iPhone backup format. Running restore mode on an iOS archive exits with a clear error.

```bash
python3 wa_media_archiver.py \
  --mode restore \
  --output /path/to/output
```

The reconstructed tree is written to `<output>/Media/`, alongside the existing `Contacts/` and `Groups/` folders. No files are overwritten — identical files already in place are skipped silently.

**How it works:** During every real (non-dry-run) forward run, the script records each original WhatsApp `file_path` and the archive path it was copied to, in `.wa_media_archiver.db`. Restore mode reads this data and copies each unique original file back to its original relative path. If the same file was archived from multiple chats, it is restored exactly once.

**Limitations:**
- Restored file timestamps reflect the WhatsApp message timestamp (same as in the archive), not the original on-device creation date.
- If a forward run was filtered with `--since` or `--limit`, the restore index only covers what was actually archived — the restored `Media/` will be a partial replica.
