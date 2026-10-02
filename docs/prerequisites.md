[← Back to Main Readme](../README.md)

# Prerequisites & Installation

Everything you need to get started with **WhatsApp Backup Tools (`whatsapp-backup-tools`)**.

---

## 1. System Requirements

- **Python**: Version 3.11 or later is required (Python 3.12 recommended).
- **Supported Operating Systems**: Windows 10/11, macOS (12+), and modern Linux distributions.
- **Local Storage**: Adequate free disk space for your media and extracted databases. All processing is 100% local—no data leaves your machine.

---

## 2. Installation

Install directly via `pip` from PyPI:

```bash
pip install whatsapp-backup-tools
```

Or clone the repository and install locally:

```bash
git clone https://github.com/auanasgheps/whatsapp-backup-tools.git
cd whatsapp-backup-tools
pip install .
```

You can also download pre-built `.whl` packages from [GitHub Releases](https://github.com/auanasgheps/whatsapp-backup-tools/releases) and install them:

```bash
pip install wab_tools-x.xx-py3-none-any.whl
```

> 💡 **Windows users:** If `pip install` is not recognized, run `py -m pip install .` instead.

This registers the two unified CLI commands:
- **`wab-archiver`**: Preserves and organizes media into a structured directory tree.
- **`wab-viewer`**: Launches the local WebUI chat viewer.

All required core packages (`flask`, `wa-crypt-tools`, `iphone-backup-decrypt`) are automatically installed.

### Manual Execution (without pip)

Alternatively, clone the repository and run the tools directly using Python without installing the package:

```bash
git clone https://github.com/auanasgheps/whatsapp-backup-tools.git
cd whatsapp-backup-tools
python -m wab_archiver --help
python -m wab_viewer --help
```

When using manual execution, you must manually install required packages.

---

## 3. Platform-Specific Prerequisites

- **Android Users**:
  - Enable **End-to-End Encrypted Backup** in WhatsApp (`Settings → Chats → Chat Backup → End-to-end Encrypted Backup`) and save your 64-digit key.
  - No root is required.
  - Review the [Android Setup Guide](archiver/setup-android.md).

- **iOS Users**:
  - In-app WhatsApp E2E backup must be **disabled** before making a device backup.
  - Create a device backup using **Finder** (macOS) or the **Apple Devices app / iTunes** (Windows).
  - Review the [iOS Setup Guide](archiver/setup-ios.md).

---

## Next Step

👉 Proceed to the **[Archiver Guide](archiver/index.md)** to prepare your media archive.
