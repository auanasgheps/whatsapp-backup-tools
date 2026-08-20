
# Prerequisites

## Python

Python 3.11 or later is required.

---

## Optional Packages

These packages are installed automatically when the relevant feature is used. You can also install them upfront.

**Android encrypted backup decryption** (required for `--msgstore <file.crypt15>` or `--mode adb`):

```bash
pip install wa-crypt-tools
```

**iOS encrypted backup decryption** (required for encrypted iPhone backups):

```bash
pip install iphone-backup-decrypt
```

**Timezone support on Windows** (required for `--timezone`):

```bash
pip install tzdata
```

> 💡 **Windows users:** if `pip install` doesn't work for you, use `py -m pip install`.
