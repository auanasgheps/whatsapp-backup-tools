# WhatsApp Backup Tools v0.51

This minor update focuses on fixes, performance optimizations, and documentation refinements that were left outside of the initial stable release. Notably, official PyPI distribution is now available!

## What's New in v0.51

### 🌐 WhatsApp Backup Viewer (`wab-viewer`)

- **Faster & Smoother Media Gallery**: Browsing large media collections is now much faster and more responsive. Media items load on demand as you scroll and unload when off-screen, drastically reducing browser memory usage and keeping scrolling smooth.
- **Accurate Video Previews**: Video thumbnails now show actual preview frames instead of blank or black rectangles, and generate quietly in the background without causing scroll stutter.
- **Multi-Year Archive View**: Fixed an issue where older years in large chats were omitted. All years are now automatically fetched and browsable in the Archive View.
- **Sorted Month Dividers**: Fixed out-of-order month headers when browsing through mixed media and documents.
- **Accurate Chat Media Size**: Fixed the Chat Info panel to accurately calculate and display total media storage used by individual chats.

### 📁 WhatsApp Backup Archiver (`wab-archiver`)

- No changes in this release.

### 📦 Installation & Packaging

- **Now on PyPI**: You can now install and update the tools directly with pip:
  ```bash
  pip install whatsapp-backup-tools
  ```
- **Automated Release Pipeline**: Configured seamless publishing to PyPI for future updates.

### 📚 Documentation

- **Simplified Onboarding**: Streamlined the setup flow into an easy 3-step guide in the `README`.
- **Syncthing Guide**: Added a step-by-step walkthrough for pairing and syncing Android media over local Wi-Fi.
- **Stickers Archival Status**: Clarified that WhatsApp stickers are safely saved into dedicated archive folders by `wab-archiver`.

---

### 🚀 Getting Started

Install via pip:

```bash
pip install whatsapp-backup-tools
```

> 💡 **Windows users:** If `pip` is not recognized, run `py -m pip install whatsapp-backup-tools` instead.

#### 1. Archive your backup

The easiest and recommended way to run the archiver is using a configuration file, avoiding long terminal commands:

1. **Generate the configuration file:**
   ```bash
   wab-archiver config generate
   ```
   This creates an `example-config.toml`. Open it in any text editor, fill in your paths and encryption key, and save it as `config.toml`.

2. **Preview and run:**
   ```bash
   wab-archiver archive --dry-run
   ```
   Once the preview completes without errors, run `wab-archiver archive` to build your archive!

<details>
<summary><b>Alternatively, run directly with command-line arguments</b></summary>

```bash
# Android example (dry run first)
wab-archiver archive \
  --msgstore /path/to/msgstore.db.crypt15 \
  --e2e-key YOUR_64_CHAR_KEY \
  --wa-root /path/to/WhatsApp \
  --output /path/to/my-archive \
  --dry-run

# iOS example
wab-archiver archive \
  --ios-backup /path/to/Backup/<UDID> \
  --output /path/to/my-archive \
  --dry-run
```
</details>

#### 2. Open the viewer
```bash
wab-viewer /path/to/my-archive
```

Opens locally in your browser at `http://127.0.0.1:5000`.
