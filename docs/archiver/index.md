[← Back to Main Readme](../../README.md) • [Prerequisites](../prerequisites.md)

# WhatsApp Backup Archiver (`wab-archiver`)

**`wab-archiver`** organizes your unstructured WhatsApp media into a clean, human-readable directory hierarchy while preserving original message timestamps, sender names, and contact identities.

---

## The 5-Step Archiving Journey

Follow this workflow to create your structured archive:

```
┌────────────────────────┐      ┌────────────────────────┐      ┌────────────────────────┐
│  1. Setup Platform     │ ───> │  2. Configure Run      │ ───> │  3. Execute Dry-Run    │
│  (Android or iOS)      │      │  (CLI or config.toml)  │      │  (--dry-run preview)   │
└────────────────────────┘      └────────────────────────┘      └────────────────────────┘
                                                                             │
                                                                             ▼
┌────────────────────────┐      ┌────────────────────────┐      ┌────────────────────────┐
│  Explore in Viewer     │ <─── │  5. Inspect Audit      │ <─── │  4. Live Run           │
│  (wab-viewer)          │      │  (CSV audit reports)   │      │  (copy & preserve)     │
└────────────────────────┘      └────────────────────────┘      └────────────────────────┘
```

---

### Step 1: Set Up Your Device Platform

Choose your mobile platform to extract the necessary databases and media files:

- 📱 **[Android Setup Guide](setup-android.md)**:
  - Enable in-app E2E encryption and save your 64-digit key.
  - Pull your WhatsApp media folder over USB via **ADB** or sync using **Syncthing**.
  - No root required.
- 🍏 **[iOS Setup Guide](setup-ios.md)**:
  - Disable in-app WhatsApp E2E encryption before backing up.
  - Create a local device backup using **Finder** (macOS) or **Apple Devices / iTunes** (Windows).
  - The archiver reads directly from the backup directory (unencrypted or encrypted).

---

### Step 2: Configure Your Run

You can run the archiver using command-line arguments or via a convenient `config.toml` file:

- **Config File (Recommended for repeatable runs)**:
  Generate an example config file:
  ```bash
  wab-archiver config generate
  ```
  Rename `example-config.toml` to `config.toml`, fill in your paths, and run simply with `wab-archiver archive`. See the **[Configuration Guide](configuration.md)** for all options and discovery rules.
- **Command-Line Arguments & Recipes**:
  Specify options directly on the terminal. See the **[Command Reference](command-reference.md)** for the full flag specification and **[Recipes & Workflows](recipes.md)** for practical examples.

> 💡 **Tip — Cloud Photo Galleries (Google Photos, Immich, Apple Photos):**  
> If you plan to sync or upload your archive to cloud photo managers, consider enabling `--exif-timestamps` (or `exif_timestamps = true` in `config.toml`) so message dates are stamped directly into media headers for proper chronological timeline indexing.

---

### Step 3: Always Run a Dry Run First

Before writing any files to disk, run with `--dry-run` to verify that your paths, encryption keys, and contact mappings resolve properly:

**Android example:**
```bash
wab-archiver archive \
  --msgstore "/path/to/WhatsApp/Databases/msgstore.db.crypt15" \
  --e2e-key YOUR_64_HEX_KEY \
  --wa-root "/path/to/WhatsApp" \
  --output "/path/to/archive" \
  --dry-run
```

**iOS example:**
```bash
wab-archiver archive \
  --ios-backup "/path/to/backup/folder" \
  --output "/path/to/archive" \
  --dry-run
```

The dry run simulates the entire process, tests database decryption, verifies schema compatibility, and displays expected file counts without copying media files or writing reports (note: if your database is encrypted, the decrypted SQLite database is extracted to allow querying).

---

### Step 4: Perform the Live Run & Check Audit Reports

Once satisfied with the dry run output, remove the `--dry-run` flag to execute:

```bash
wab-archiver archive --config config.toml
```

When finished, your output folder will be structured as follows:

```
<output>/
├── Contacts/                    ← Media organized by contact name & phone
│   └── John Doe (00123456789)/
│       └── 2024/
│           ├── Received/
│           └── Sent/
├── Groups/                      ← Media organized by group name
│   └── Family Chat/
│       └── 2024/
├── Whatsapp Databases/          ← Decrypted SQLite databases saved for viewer
├── preserved_messages.db        ← Preserved deleted messages & chats (when using -P)
├── wab-archiver.log             ← Execution audit log
├── missing_media_report.csv     ← Media referenced in chats but missing from storage
├── duplicate_media_report.csv   ← Duplicate media files audited
├── source_conflicts_report.csv  ← Conflicts across multiple backup roots (--wa-root)
└── .wa_media_archiver.db        ← Archive state database
```

Review the generated CSV audit reports to identify any media files that were missing from device storage or duplicate files skipped.

> 💡 **Photo Library & Cloud Sync (Google Photos, Immich, Apple Photos):**
> By default, the archiver sets filesystem modification dates (`mtime`). However, cloud galleries prioritize embedded metadata tags (`DateTimeOriginal` for JPEG photos, `mvhd` creation time for MP4/MOV/3GP/M4A media).
> 
> Pass `--exif-timestamps` (or set `exif_timestamps = true` in `config.toml`) to inject message timestamps directly into media metadata headers without re-compression:
> - **Zero Re-compression**: Loss-free header injection; pixel data and video streams are never transcoded.
> - **Original Photos Protected**: Existing camera capture dates and orientation tags are strictly preserved.
> - **Timezone Aware**: Pair with `--timezone <Region/City>` to embed exact UTC offsets (`OffsetTimeOriginal`).
>
> 🔄 **Already have an existing archive without metadata?**  
> You do not need to re-import or connect your device. Backfill your archive in seconds:
> ```bash
> wab-archiver update-metadata -o /path/to/output --timezone Europe/Berlin
> ```
> See the [Command Reference](command-reference.md#subcommand-update-metadata) or [Recipes](recipes.md#5-retroactively-backfill-metadata-on-existing-archives-update-metadata) for full details.

---

### Step 5: Explore Your Archive in the Web Viewer

Your archive is now ready for offline browsing! Launch the local chat viewer pointing to your output folder:

```bash
wab-viewer /path/to/archive
```

See the **[WhatsApp Backup Viewer Guide](../viewer/index.md)** for timeline browsing, full-text search, and media playback instructions.

---

## Guided Navigation

- **Previous**: [Prerequisites & Installation](../prerequisites.md)
- **Next**: Choose your platform guide:
  - 🤖 **[Android Setup Guide](setup-android.md)**
  - 🍎 **[iOS Setup Guide](setup-ios.md)**
  - 📖 **[CLI Command Reference](command-reference.md)**
  - ⚙️ **[Configuration Guide](configuration.md)**
  - 💡 **[CLI Recipes & Workflows](recipes.md)**
