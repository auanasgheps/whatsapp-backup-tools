# Contributing to WhatsApp Backup Tools

Thank you for your interest in contributing to WhatsApp Backup Tools (`whatsapp-backup-tools`)!

## Core Principles & Privacy

1. **Privacy-First & Local-Only**: This project never transmits user data over the network. All processing happens locally on the user's machine.
2. **Strictly No Real Device Data**: Never commit real WhatsApp device data, database files, protobuf dumps, phone numbers, contact names, or `@lid` JIDs to the repository, tests, or issues. Always use synthesized or anonymized test fixtures.
3. **Clean Diffs & Scoped PRs**: Keep pull requests focused on a single bug fix or feature.

---

## Development Setup

### Prerequisites

- Python 3.11+ (Python 3.12 recommended)
- `git`

### Installation

Clone the repository and install all dependencies in editable mode:

```bash
git clone https://github.com/auanasgheps/whatsapp-backup-tools.git
cd whatsapp-backup-tools

# Install in editable mode with development/test dependencies
pip install -e ".[all]"
```

---

## Testing & Quality Gates

Before submitting a pull request, ensure all tests and lint checks pass:

### 1. Run the test suite

The test suite is organized into modular packages matching the project architecture:

```bash
# Run the entire test suite
pytest tests/ -v

# Run targeted package suites
pytest tests/viewer/         # Web Viewer routes, hydration, and UI
pytest tests/archiver/       # WhatsApp Archiver CLI, ADB, and copy engine
pytest tests/merger/         # Increment delta replay and protobuf decoding
pytest tests/preservation/   # Preserved messages and two-phase diff engine
pytest tests/common/         # Shared validators and metadata writers
pytest tests/e2e/            # Multi-scenario and multi-cycle simulation

# Run using domain markers
pytest -m "viewer and not slow"
pytest -m archiver
pytest -m merger
```

### 2. Lint and format checks

We use [Ruff](https://astral.sh/ruff) for linting and code formatting:

```bash
# Check formatting
ruff format --check .

# Run linter
ruff check .

# Automatically apply safe fixes and formatting
ruff check --fix .
ruff format .
```

### 3. Frontend Development (`wab_viewer`)

The chat viewer Web UI uses a zero-build, pure Python assembly architecture:
- **Source of Truth**: Modular JavaScript source files live in `wab_viewer/chat_viewer/js/`. There is no static `app.js` file on disk.
- **In-Memory Bundle Assembly**: At runtime, Flask serves `/static/app.js` assembled directly in-memory from `wab_viewer/chat_viewer/js/*.js` via `builder.get_bundle_content()`.
- **Dev-Mode Hot Reloading**: In debug mode (`wab-viewer --verbose`), changes in `js/` take effect immediately without requiring manual builds.
- **Assembly Verification**: `test_app_js_bundle_assembles_cleanly` in `tests/viewer/test_ui_template.py` validates that all 31 modules assemble into a valid bundle.

---

## Submitting Pull Requests

1. Create a feature branch from `dev`:
   ```bash
   git checkout -b feature/my-enhancement
   ```
2. Make your changes with clear, concise commit messages.
3. Ensure all tests and lint checks pass locally.
4. Push your branch and open a Pull Request against `dev`.
