# Archiver

Organise your WhatsApp media into a structured archive.

## Quick Start

Always do a dry run first:

> 💡 Without `pip install -e .`, use `python -m wab_archiver` instead of `wab-archiver`.

```bash
wab-archiver archive \
  --msgstore /path/to/msgstore.db \
  --wa-root /path/to/WhatsApp/storage \
  --output /path/to/output \
  --contacts /path/to/wa_contacts \
  --dry-run
```

> 💡 On Windows, replace `\` with `` ` `` (PowerShell) or `^` (cmd.exe).

### Android — automatic pull and decrypt

Connect your phone via USB with USB debugging enabled:

```bash
wab-archiver archive \
  --from-adb \
  --e2e-key 1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a6b7c8d9e0f1a2b \
  --output /path/to/output \
  --dry-run
```

To also pull media files over ADB (instead of using Syncthing or a manual copy), add `--pull-media --staging /path/to/staging`. See [Android setup](setup-android.md#pulling-media-via-adb-optional) for details.

### iOS — read directly from an iPhone backup

```bash
wab-archiver archive \
  --ios-backup /path/to/iPhone/backup \
  --output /path/to/output \
  --dry-run
```

---

## Setup

- [Android setup](setup-android.md)
- [iOS setup](setup-ios.md)

## Reference

- [Command reference and config file](command-reference.md)
