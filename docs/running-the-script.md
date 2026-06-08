
# Running the Script

## Command Reference

```
usage: wa_media_archiver.py [-h]
                      [-msg MSGSTORE]
                      [-e2e E2E_KEY]
                      [-c CONTACTS]
                      [-wa WA_ROOT]
                      [--ios_backup PATH]
                      [--ios_password PASSWORD]
                      [--ios_contacts PATH]
                      [--business]
                      -o OUTPUT
                      [-l LOG]
                      [-mode {adb,restore}]
                      [--dry-run]
                      [--limit N]
                      [--since DATE]
                      [--timezone TZ]
```

| Argument | Required | Description |
|---|---|---|
| `-msg` / `--msgstore` | No | Path to `msgstore.db`, `msgstore.db.crypt15`, or `ChatStorage.sqlite`. Not needed with `--ios_backup`. Defaults to `msgstore.db` in the current folder |
| `-e2e` / `--e2e_key` | If encrypted | Your cryptographic key for `.crypt15` decryption |
| `-c` / `--contacts` | No | Path to the `wa_contacts` file exported via ADB (Android only) |
| `-wa` / `--wa_root` | Android / iOS pre-extracted | Root path of your WhatsApp folder from mass storage. Android: this contains the `Media/` folder. iOS pre-extracted: `AppDomainGroup-group.net.whatsapp.WhatsApp.shared` folder. Not required with `--ios_backup` or `--mode restore` |
| `--ios_backup` | iOS (recommended) | Path to the iPhone backup directory (the folder containing `Manifest.db`). Mutually exclusive with `--wa_root` |
| `--ios_password` | No | Password for an encrypted iPhone backup. Only needed when the backup is encrypted |
| `--ios_contacts` | No | Path to `ContactsV2.sqlite` for iOS contacts. Auto-extracted from `--ios_backup` if omitted |
| `--business` | No | Target **WhatsApp Business** instead of the regular WhatsApp app. Affects the ADB pull path (Android `--mode adb`) and the backup domain (iOS `--ios_backup`). Not needed when using `--wa_root` — just point it at the `WhatsApp Business/` folder |
| `-o` / `--output` | **Yes** | Destination folder for the archive |
| `-l` / `--log` | No | Custom log file path. Defaults to `<output>/wa_media_archiver.log` |
| `-mode` / `--mode` | No | `adb` = automatically pull msgstore and contacts from a connected Android device; `restore` = reconstruct original `Media/` tree from the archive (Android archives only) |
| `--dry-run` | No | Simulate the run without copying any files |
| `--limit N` | No | Cap rows returned per chat type (N/2 from groups, N/2 from 1-to-1). Total rows ≤ N. Useful for test runs |
| `--since DATE` | No | Only include messages on or after this date (`YYYY-MM-DD`). Combines freely with `--limit` |
| `--timezone TZ` | No | [IANA timezone name](https://en.wikipedia.org/wiki/List_of_tz_database_time_zones#List) for year folder assignment and report timestamps (e.g. `Europe/Rome`, `America/New_York`, `UTC`). Ensures files land in the correct `<YYYY>/` subfolder regardless of where or when the script is run. Also applied to `--since` date interpretation. Defaults to machine local time. **Windows users: requires `pip install tzdata`** |

---

## Line Continuation by Shell

The examples below use `\` to split long commands across multiple lines. Replace it with the correct character for your shell:

| Shell | Character |
|---|---|
| bash / zsh (Linux, macOS) | `\` |
| PowerShell (Windows) | `` ` `` |
| cmd.exe (Windows) | `^` |

---

## Usage Examples

### Android

#### ADB mode — automatic pull and decrypt (recommended)

```bash
python3 wa_media_archiver.py \
  --mode adb \
  --e2e your_cryptographic_key \
  --wa_root /path/to/WhatsApp/storage \
  --output /path/to/output \
  --dry-run
```

> 💡 The script pulls the encrypted database and contacts directly from the connected device and decrypts on the fly. No separate steps needed.

#### Manual — decrypted database

```bash
python3 wa_media_archiver.py \
  --msgstore /path/to/msgstore.db \
  --wa_root /path/to/WhatsApp/storage \
  --output /path/to/output \
  --contacts /path/to/wa_contacts \
  --dry-run
```

#### Manual — encrypted database (script decrypts)

```bash
python3 wa_media_archiver.py \
  --msgstore /path/to/msgstore.db.crypt15 \
  --e2e your_cryptographic_key \
  --wa_root /path/to/WhatsApp/storage \
  --output /path/to/output \
  --dry-run
```

> 💡 Even in dry run mode, decryption still occurs so the DB can be queried. The decrypted `msgstore.db` is written to disk regardless of `--dry-run`.

---

### iOS

#### Standard flow — unencrypted backup (recommended)

```bash
python3 wa_media_archiver.py \
  --ios_backup ~/Library/Application\ Support/MobileSync/Backup/<UDID> \
  --output /path/to/output \
  --dry-run
```

#### Encrypted backup

```bash
python3 wa_media_archiver.py \
  --ios_backup ~/Library/Application\ Support/MobileSync/Backup/<UDID> \
  --ios_password your_backup_password \
  --output /path/to/output \
  --dry-run
```

> 💡 `--wa_root` and `--contacts` are not needed. The script extracts `ChatStorage.sqlite` and `ContactsV2.sqlite` directly from the backup. `ChatStorage.sqlite` is also saved to the output folder for re-runs.

---

### Advanced

#### Test run — recent files only

```bash
python3 wa_media_archiver.py \
  --msgstore /path/to/msgstore.db \
  --wa_root /path/to/WhatsApp/storage \
  --output /path/to/output \
  --since 2025-01-01 \
  --limit 100 \
  --dry-run
```

#### iOS — pre-extracted mode

```bash
python3 wa_media_archiver.py \
  --msgstore /path/to/ChatStorage.sqlite \
  --wa_root /path/to/AppDomainGroup-group.net.whatsapp.WhatsApp.shared \
  --ios_contacts /path/to/ContactsV2.sqlite \
  --output /path/to/output
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

> ⚠️ **Android archives only.** iOS media is stored at `Message/Media/...` paths that have no equivalent reconstruction target outside the iPhone backup format. Running restore mode on an iOS archive is disallowed.

The reconstructed tree is written to `<output>/Media/`, alongside the existing `Contacts/` and `Groups/` folders. No files are overwritten — identical files already in place are skipped silently.

**How it works:** During every real (non-dry-run) forward run, the script records each original WhatsApp `file_path` and the archive path it was copied to, in `.wa_media_archiver.db`. Restore mode reads this data and copies each unique original file back to its original relative path. If the same file was archived from multiple chats, it is restored exactly once.

**Limitations:**
- Restored file timestamps reflect the WhatsApp message timestamp (same as in the archive), not the original on-device creation date.
- If a forward run was filtered with `--since` or `--limit`, the restore index only covers what was actually archived — the restored `Media/` will be a partial replica.
