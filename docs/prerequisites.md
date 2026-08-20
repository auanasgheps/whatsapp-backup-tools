
# Prerequisites

## Python

Python 3.11 or later is required.

---

## wab-archiver Packages

Install these manually before running the archiver.

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

---

## wab-viewer Packages

**Flask** (required):

```bash
pip install flask
```

> 💡 **Windows users:** if `pip install` doesn't work for you, use `py -m pip install`.
