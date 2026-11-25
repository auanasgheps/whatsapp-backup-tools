[← Back to Main Readme](../../README.md) • [Prerequisites](../prerequisites.md) • [Viewer Guide](../viewer/index.md) • [Sweeper Guide](../sweeper/index.md)

# WhatsApp Backup Archiver (`wab-archiver`)

**`wab-archiver`** organizes your unstructured WhatsApp media into a clean, human-readable directory hierarchy while preserving original message timestamps, sender names, and contact identities.

---

## The 5-Step Archiving Journey

```text
┌────────────────────────┐      ┌────────────────────────┐      ┌────────────────────────┐
│  1. Setup Platform     │ ───> │  2. Configure Run      │ ───> │  3. Dry-Run Preview    │
│  (Android or iOS)      │      │  (config.toml or CLI)  │      │  (--dry-run)           │
└────────────────────────┘      └────────────────────────┘      └────────────────────────┘
                                                                             │
                                                                             ▼
                                ┌────────────────────────┐      ┌────────────────────────┐
                                │  5. Explore in Viewer  │ <─── │  4. Archive & Audit    │
                                │  (wab-viewer)          │      │  (live run + reports)  │
                                └────────────────────────┘      └────────────────────────┘
```

---

### Step 1: Set Up Your Device Platform

Choose your mobile platform to extract the necessary databases and media files:

- 📱 **[Android Setup Guide](setup-android.md)**: Enable in-app E2E encryption (save your 64-digit key) and pull media via **ADB** or **Syncthing** (no root required).
- 🍏 **[iOS Setup Guide](setup-ios.md)**: Disable in-app WhatsApp E2E encryption and create a local backup via **Finder** (macOS) or **Apple Devices / iTunes** (Windows).

---

### Step 2: Configure Your Run

- **Config File (Recommended)**: Generate a starter template with `wab-archiver config generate`, rename `example-config.toml` to `config.toml`, and fill in your paths. See the **[Configuration Guide](configuration.md)** for all options.
- **CLI Flags & Recipes**: Pass options directly on the command line. See the **[Command Reference](command-reference.md)** and **[Recipes & Workflows](recipes.md)**.

> 💡 **Cloud Photo Galleries (Google Photos, Immich, Apple Photos):** Enable `--exif-timestamps` (or `exif_timestamps = true` in `config.toml`) to embed original message dates into image/video headers without re-compression. Already have an archive? Backfill it in-place with `wab-archiver update-metadata -o /path/to/archive` (see [Recipes](recipes.md#5-retroactively-backfill-metadata-on-existing-archives-update-metadata)).

---

### Step 3: Run a Dry-Run First

Before copying files, run with `--dry-run` to verify database decryption, schema compatibility, and expected file counts:

```bash
wab-archiver archive --dry-run
```

*(If you are not using `config.toml`, pass `--msgstore`, `--e2e-key`, `--wa-root` / `--ios-backup`, and `--output`; see [Command Reference](command-reference.md) for flag examples.)*

---

### Step 4: Perform the Live Run & Check Audit Reports

Remove `--dry-run` to build or update your archive:

```bash
wab-archiver archive
```

Your output folder will contain:
- **`Contacts/`** & **`Groups/`**: Media organized by contact/group, year, and `Sent/` / `Received/`.
- **`WhatsApp Databases/`** & **`.wa_media_archiver.db`**: Decrypted databases and archive state (plus `preserved_messages.db` when using `-P`).
- **CSV Audit Reports**: `missing_media_report.csv`, `duplicate_media_report.csv`, and `source_conflicts_report.csv` (see the [Main Readme](../../README.md#archive-structure) for the full layout).

---

### Step 5: Explore Your Archive in the Web Viewer

Launch the local chat viewer pointing to your output folder:

```bash
wab-viewer /path/to/archive
```

See the **[WhatsApp Backup Viewer Guide](../viewer/index.md)** for browsing, search, and media playback.

> 🧹 **Optional (Android): Reclaim Phone Storage**  
> Once your archive is verified, you can safely remove older archived media from your Android device via ADB (with `restore` and `undo` support). See the **[Sweeper Guide](../sweeper/index.md)**.

---

## Guided Navigation

- **Previous**: [Prerequisites & Installation](../prerequisites.md)
- **Reference Guides**:
  - 🤖 **[Android Setup Guide](setup-android.md)**
  - 🍎 **[iOS Setup Guide](setup-ios.md)**
  - ⚙️ **[Configuration Guide](configuration.md)**
  - 📖 **[CLI Command Reference](command-reference.md)**
  - 💡 **[CLI Recipes & Workflows](recipes.md)**
