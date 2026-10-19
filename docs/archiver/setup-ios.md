[← Back to Archiver Overview](index.md) • [Main Readme](../../README.md)

# iOS Setup

WhatsApp on iOS stores its database and media inside an iPhone backup. The archiver reads the backup directly via `--ios-backup`, resolving all chats, media files, and auxiliary databases automatically.

---

## Important: WhatsApp In-App E2E Encrypted Backup

**WhatsApp in-app E2E encrypted backup** must be **disabled** before creating the iPhone backup:

```
WhatsApp Settings → Chats → Chat Backup → End-to-end Encrypted Backup → Turn Off
```

> ⚠️ **Why this is required:** WhatsApp's in-app E2E encryption encrypts the internal SQLite database (`ChatStorage.sqlite`) with a separate client-side key before writing it to disk. This is separate from iPhone backup encryption. If enabled, the database inside the iPhone backup cannot be read.
>
> **iPhone backup encryption is fully supported** — you can safely encrypt your entire iPhone backup in Finder or the Apple Devices app.

---

## Step 1 — Create a Device Backup

Connect your iPhone or iPad to your computer via USB cable.

### macOS (Finder)
1. Open **Finder** and select your device in the sidebar.
2. Under the **General** tab, select **"Back up all of the data on your iPhone to this Mac"**.
3. Choose whether to encrypt the backup:
   - **Unencrypted**: Faster, but excludes saved passwords and health data.
   - **Encrypted**: Recommended. Set a memorable password (needed for `--ios-password`).
4. Click **Back Up Now** and wait for completion.

### Windows (Apple Devices App or iTunes)
- **Modern Windows (Recommended)**: Install [Apple Devices](https://apps.microsoft.com/detail/9NP83LWLPZ9K) from the Microsoft Store. Connect your device, select it, choose backup destination (**This computer**), and click **Back Up Now**.
- **Classic Windows (iTunes)**: Open iTunes, click the phone icon in the top toolbar, select **This computer**, and click **Back Up Now**.

---

## Step 2 — Locate the Backup Directory

The backup directory is the folder containing `Manifest.db`. Look for your device's Unique Device Identifier (UDID):

| Operating System | Tool Used | Backup Directory Path |
| :--- | :--- | :--- |
| **macOS** | Finder | `~/Library/Application Support/MobileSync/Backup/<UDID>/` |
| **Windows** | Apple Devices App | `C:\Users\<Username>\Apple\MobileSync\Backup\<UDID>\` |
| **Windows** | iTunes (Desktop) | `%AppData%\Apple Computer\MobileSync\Backup\<UDID>\` |
| **Windows** | iTunes (Store) | `%UserProfile%\Apple\MobileSync\Backup\<UDID>\` |

> 💡 **Tip for finding the folder quickly on Windows:** Press `Win + R`, paste `%AppData%\Apple Computer\MobileSync\Backup` or `%UserProfile%\Apple\MobileSync\Backup`, and press Enter.

---

## Step 3 — Run the Archiver

### Unencrypted Backup

```bash
wab-archiver archive \
  --ios-backup "~/Library/Application Support/MobileSync/Backup/<UDID>" \
  --output "/path/to/output" \
  --dry-run
```

### Encrypted iPhone Backup

If your iPhone backup is password-protected, provide `--ios-password`:

```bash
wab-archiver archive \
  --ios-backup "~/Library/Application Support/MobileSync/Backup/<UDID>" \
  --ios-password your_backup_password \
  --output "/path/to/output" \
  --dry-run
```

> 💡 On Windows, replace `\` with `` ` `` (PowerShell) or `^` (cmd.exe).  
> 💡 **Paths with spaces:** Windows backup paths often contain spaces (e.g. `C:\Users\John Doe\Apple\...`) — always wrap paths in double quotes.

---

## How iOS Backup Extraction Works

1. **Manifest Parsing (`Manifest.db`)**: The archiver opens the backup's SQLite manifest to locate WhatsApp's AppGroup container:
   - Standard WhatsApp: `AppDomainGroup-group.net.whatsapp.WhatsApp.shared`
   - WhatsApp Business: `AppDomainGroup-group.net.whatsapp.WhatsAppSMB.shared`
2. **Encrypted Backups**: When `--ios-password` is provided, the archiver unlocks the backup keybag using PBKDF2 + AES-GCM/CBC, decrypting files transparently on the fly without writing unencrypted intermediate backups to disk.
3. **Differential Storage**: Apple's backup engine is differential — files that have not changed since the last backup are retained, and only modified files are updated. You can re-run the archiver on the same backup folder over time.
4. **Auxiliary Databases Extraction**: During archiving, all essential databases are extracted and preserved into `<output>/Whatsapp Databases/`:
   - `ChatStorage.sqlite` (Primary message store)
   - `ContactsV2.sqlite` (Address book contacts & LIDs)
   - `ExtChatDatabase.sqlite` (HD media associations & parent/child links)
   - `MessagingInfraDatabase.sqlite` (Read/delivery receipts ledger)
   - `LID.sqlite` (LID account phone mappings)
   - `Labels.sqlite` (WhatsApp Business labels)
   - `CallHistory.sqlite` (Call event logs)

---

## Troubleshooting

### "Manifest.db not found"
- Verify the path passed to `--ios-backup` points directly to the `<UDID>` directory containing `Manifest.db`, not to the parent `Backup/` folder.
- If you backed up with macOS Finder, ensure Terminal has **Full Disk Access** in `System Settings → Privacy & Security → Full Disk Access` to access `~/Library/Application Support/MobileSync/`.

### "Decryption failed / Incorrect password"
- The password is the **iPhone backup encryption password** set in Finder or Apple Devices, NOT your Apple ID password or device lockscreen PIN.
- If you forgot your backup password, Apple allows resetting it on the device: `Settings → General → Transfer or Reset iPhone → Reset → Reset All Settings` (this resets backup password without deleting device data, allowing you to create a new backup with a new password).

### "No WhatsApp data found in backup"
- Ensure WhatsApp was installed and active on the device during the backup.
- If you use WhatsApp Business, the archiver automatically detects `WhatsAppSMB.shared`. Ensure the app had chats present when the backup ran.

---

## Next Steps

Now that your iOS backup is configured:

1. 📖 Review the **[Command Reference & Config File](command-reference.md)** for advanced options (filtering by date, deduplication, deleted messages preservation).
2. 🧪 Run your command with `--dry-run` to preview the media organization and message count.
3. 🌐 Launch the **[WhatsApp Backup Viewer](../viewer/index.md)** to browse your conversations with full iOS receipts, reactions, and polls.
