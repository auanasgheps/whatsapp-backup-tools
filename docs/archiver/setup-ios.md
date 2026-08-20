# iOS Setup

WhatsApp on iOS stores its database and media inside an iPhone backup. The archiver reads the backup directly via `--ios_backup`.

---

## WhatsApp E2E Encrypted Backup

**WhatsApp E2E encrypted backup** (a separate WhatsApp setting) must be **disabled** before creating the iPhone backup. If it is enabled, the database inside the iPhone backup is encrypted in a way that cannot be read by the archiver.

Disable it in WhatsApp: `Settings → Chats → Chat Backup → End-to-end Encrypted Backup → Turn Off`

> This is separate from iPhone backup encryption (which the archiver **does** support).

---

## Step 1 — Create a Device Backup

**macOS** — Connect your iPhone or iPad and open Finder. Select your device and click "Back Up Now". You may choose encrypted or unencrypted.

**Windows** — Install [Apple Devices](https://apps.microsoft.com/detail/9NP83LWLPZ9K) from the Microsoft Store. Connect your device and create a backup.

---

## Step 2 — Locate the Backup Directory

**macOS:**

```
~/Library/Application Support/MobileSync/Backup/<UDID>/
```

**Windows (iTunes):**

```
%AppData%\Apple\MobileSync\Backup\<UDID>\
```

**Windows (Apple Devices app):**

```
C:\Users\<Username>\Apple\MobileSync\Backup\<UDID>\
```

The backup directory is the folder that contains `Manifest.db`. Pass it to `--ios_backup`.

---

## Step 3 — Run the Archiver

Unencrypted backup:

```bash
python -m wab_archiver \
  --ios_backup ~/Library/Application\ Support/MobileSync/Backup/<UDID> \
  --output /path/to/output \
  --dry-run
```

Encrypted iPhone backup (use the password you set in Finder/Apple Devices):

```bash
python -m wab_archiver \
  --ios_backup ~/Library/Application\ Support/MobileSync/Backup/<UDID> \
  --ios_password your_backup_password \
  --output /path/to/output \
  --dry-run
```

> 💡 On Windows, replace `\` with `` ` `` (PowerShell) or `^` (cmd.exe).

Contacts are automatically extracted from the backup. `ChatStorage.sqlite` is also saved to the output folder for re-runs.

---

## Encrypted vs. Unencrypted Backups

Both encrypted and unencrypted iPhone backups are supported.

- **Unencrypted backup** — no extra argument needed.
- **Encrypted backup** — pass `--ios_password <password>`. Requires `pip install iphone-backup-decrypt`.
