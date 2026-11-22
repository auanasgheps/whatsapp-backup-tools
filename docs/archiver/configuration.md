[← Back to Archiver Overview](index.md) • [CLI Reference](command-reference.md) • [Recipes & Restore](recipes.md) • [Main Readme](../../README.md)

# Configuration File Guide (`config.toml`)

Using a configuration file is the easiest way to run and re-run `wab-archiver`. Instead of repeating long arguments on the command line every time, save your paths and preferred settings in a `config.toml` file.

---

## 1. Generating the Example File

Generate a ready-to-edit configuration template directly from the CLI:

```bash
wab-archiver config generate
```

This writes `example-config.toml` next to the package. Open it in any text editor, fill in your paths and settings, and save it as `config.toml`.

---

## 2. Configuration Format & Specification

Below is the complete annotated structure of `config.toml`:

```toml
# =============================================================================
# wab-archiver config
# All paths can be absolute or relative to where you run the command.
# =============================================================================

# --- Archive Destination & General Settings ---
output     = "/path/to/archive"         # Destination folder for the archive (required)
# log      = ""                         # Custom log file path (default: <output>/wab-archiver.log)
# debug    = false                      # Enable debug logging in the log file (default: false, INFO level)
# timezone = ""                         # IANA timezone (e.g. Europe/Rome, UTC) for folders & reports
# business = false                      # Target WhatsApp Business instead of regular WhatsApp

# --- Archiving Features & Filtering ---
# preserve_deleted = true               # Preserve messages/chats deleted on phone or revoked by sender across runs
# link_duplicates  = true               # Hardlink duplicate media across chats instead of copying bytes to save disk space
# exif_timestamps  = true               # Write WhatsApp message timestamps to media EXIF/metadata headers
# since            = ""                 # Only archive media from messages on or after this date (YYYY-MM-DD)

# --- Source: Android (Local Files & Folders) ---
# msgstore = "msgstore.db"              # Path to msgstore.db or msgstore.db.crypt15
# e2e_key  = ""                         # 64-char hex key for encrypted .crypt15 backups
# contacts = ""                         # Path to contacts CSV or exported wa_contacts
# wa_root  = "/path/to/WhatsApp"        # Single source folder (containing Media/)
# wa_root  = ["/path/to/old-archive", "/path/to/current-phone/WhatsApp"]  # Multiple source folders

# --- Source: Android (ADB Mode — USB Connection) ---
# from_adb   = false                    # Pull msgstore and contacts automatically via ADB
# pull_media = false                    # Also pull media files from phone storage via ADB
# staging    = ""                       # Permanent local phone backup folder (copied to output)

# --- Source: iOS (iPhone Backup) ---
# ios_backup   = ""                     # Path to iPhone backup folder (containing Manifest.db)
# ios_password = ""                     # Password for encrypted iPhone backup
# ios_contacts = ""                     # Path to ContactsV2.sqlite (auto-extracted from backup if omitted)
```

> 💡 **Windows Users:** You can paste Windows paths directly (e.g. `C:\Users\...`). The archiver automatically converts backslashes to forward slashes and updates the config file in place on run.

---

## 3. How the Config File Is Discovered

When running `wab-archiver`, the configuration file is resolved using the following search order:

1. **Explicit path (`--config`)**:
   Pass `--config /path/to/myconfig.toml`. The specified file is loaded immediately without any interactive confirmation prompts.
2. **Auto-detection**:
   If `--config` is omitted, the archiver checks for `config.toml` in two locations:
   - The package directory
   - The current working directory (where you invoke the command)
   
   **Resolution logic:**
   - **Exactly one `config.toml` found** → The archiver asks for confirmation: `Found config.toml at <path> — use it? [Y/n]`.
   - **Two files found** → Aborts with an error to prevent accidental ambiguity; specify which one to use with `--config`.
   - **None found** → Config file is ignored; the archiver relies entirely on CLI arguments.

---

## 4. Precedence & Overrides

- **Command-line arguments always win**: Any argument passed explicitly on the command line overrides the corresponding setting in `config.toml`. The config file acts as your baseline default.
- **One-off execution flags**: Options such as `--dry-run` and `--limit` are intentionally excluded from `config.toml` because they are meant for previewing and testing. Always pass them directly on the command line when needed:
  ```bash
  wab-archiver archive --config config.toml --dry-run
  ```

---

## Next Steps

- 📖 **[CLI Command Reference](command-reference.md)** — Detailed syntax and flag tables for all subcommands.
- 💡 **[CLI Recipes & Workflows](recipes.md)** — Practical examples for Android, iOS, hardlinks, and metadata stamping.
- 🌐 **[WhatsApp Backup Viewer Guide](../viewer/index.md)** — Browse and search your archived chats.
