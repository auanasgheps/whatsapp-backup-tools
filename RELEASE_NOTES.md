# WhatsApp Backup Tools v0.70

Version 0.70 introduces smarter Android ADB status troubleshooting with interactive authorization recovery in the archiver, alongside an adaptable, scrollable Settings modal for the web viewer on smaller screens.

---

### 📁 WhatsApp Archiver

#### ✨ New Features
- **Interactive ADB Authorization Recovery**: If your Android device is detected but unauthorized, the archiver can now prompt you to restart the ADB server on the spot to re-trigger the "Allow USB debugging" prompt directly on your phone.

#### ⚡ Improvements
- **Accurate Connection & Status Troubleshooting**: Instead of displaying a generic checklist of possibilities, the archiver now tells you the exact device status (distinguishing between unplugged phones, multiple connected devices, offline links, and missing host permissions).
- **Clearer Backup Retrieval Errors**: If pulling a WhatsApp backup database fails over ADB, the archiver provides direct actionable instructions (such as reminding you to create an End-to-End Encrypted Backup in WhatsApp).

#### 🐛 Bug Fixes
- Minor bug fixes and reliability improvements.

---

### 🌐 WhatsApp Viewer

#### ⚡ Improvements
- **Responsive Settings Modal**: The Settings panel now smoothly adapts to small browser windows, split-screen views, and compact laptop screens with internal scrolling and a pinned header so controls remain accessible at any height.

#### 🐛 Bug Fixes
- Minor UI polish and rendering fixes.

---

### 🚀 How to Upgrade

Update to the latest version using pip:

```bash
pip install --upgrade whatsapp-backup-tools
```

> 💡 **Windows users:** If `pip` is not recognized, run `py -m pip install --upgrade whatsapp-backup-tools` instead.
