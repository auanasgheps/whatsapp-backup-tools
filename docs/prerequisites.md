
# Prerequisites

## Python

Python 3.11 or later is required.

---

## Installation

Install the tools from the repo root:

```bash
pip install -e .
```

This registers the `wab-archiver` and `wab-viewer` entry points so they are available as commands in your terminal.

> 💡 **Windows users:** if `pip install` doesn't work for you, use `py -m pip install`.

---

## wab-archiver Packages

Install these manually before running the archiver.

**Android encrypted backup decryption** (required for `--msgstore <file.crypt15>` or `--from-adb`):

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
