# Contributing to WhatsApp Backup Tools

Thank you for your interest in contributing to WhatsApp Backup Tools (`whatsapp-backup-tools`)!

## Core Principles & Privacy

1. **Privacy-First & Local-Only**: This project never transmits user data over the network. All processing happens locally on the user's machine. Zero external API calls, tracking, or telemetry are permitted.
2. **Strictly No Real Device Data**: Never commit real WhatsApp device data, database rows, protobuf dumps, phone numbers, contact names, or `@lid` JIDs to the repository, tests, or pull requests.
3. **Synthetic Test Data Standards**:
   - **Phone Numbers**: Use designated fictional ranges (e.g., `+1-555-01XX`) or structured sequential counters.
   - **Identities & Domains**: Use RFC 2606 reserved domains (`@example.com`) and standard test personas (`Alice`, `Bob`).
   - **Protobuf Payloads**: Use programmatic factories in `tests/fixtures/protobuf_factory.py` rather than binary blobs extracted from personal devices.
   - **Mock Schema Fidelity**: Mock database tables must accurately replicate real WhatsApp schema column constraints and `NOT NULL` rules (verified against `.agents/knowledge/android/database-schema.md` or `.agents/knowledge/ios/chatstorage-schema.md`) to avoid false CI confidence.
4. **Defensive Database Integrity**: All database mutations must execute within explicit atomic transactions (`BEGIN ... COMMIT`). Never issue blind `INSERT OR REPLACE` over autoincrement primary keys (`_id`, `Z_PK`); resolve natural keys first and preserve child table records atomically.
5. **Clean Diffs & Scoped PRs**: Keep pull requests focused on a single bug fix or feature. Keep diffs minimal and avoid unrelated reformatting.

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

# Install in editable mode with all development, test, and lint dependencies
pip install -e ".[all]"
```

---

## Testing & Quality Gates

Before submitting a pull request, ensure all tests, coverage thresholds, and lint checks pass locally:

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

### 2. Test Coverage Requirement (90%+)

We require **at least 90% test coverage** on all new code, features, bug fixes, and modifications. Pull requests that introduce untested paths or drop coverage without justification may be asked to add corresponding tests before merging.

To verify coverage locally:

```bash
# Full test suite coverage with missing line numbers
pytest tests/ --cov=wab_archiver --cov=wab_viewer --cov=shared --cov-report=term-missing

# Targeted package coverage
pytest tests/archiver/ --cov=wab_archiver --cov-report=term-missing
pytest tests/viewer/ --cov=wab_viewer --cov-report=term-missing
pytest tests/merger/ --cov=shared --cov-report=term-missing

# Generate an interactive HTML report to inspect uncovered lines in a browser
pytest tests/ --cov=wab_archiver --cov=wab_viewer --cov=shared --cov-report=html
# View report: open htmlcov/index.html
```

> **Exemptions**: Unavoidable OS-specific branches (such as platform-specific subprocess signals or live USB ADB transport) that cannot be simulated deterministically in test runners may use `# pragma: no cover` accompanied by a brief explanatory comment.

### 3. Lint and format checks

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

### 4. Frontend Architecture & UI Testing (`wab_viewer`)

The chat viewer Web UI uses a zero-build, pure Python assembly architecture:
- **Source of Truth**: Modular JavaScript source files live in `wab_viewer/chat_viewer/js/`. There is no static `app.js` file on disk.
- **In-Memory Bundle Assembly**: At runtime, Flask serves `/static/app.js` assembled directly in-memory from `wab_viewer/chat_viewer/js/*.js` via `builder.get_bundle_content()`.
- **Dev-Mode Hot Reloading**: In verbose mode (`wab-viewer --verbose`), changes in `js/` take effect immediately without requiring manual builds.
- **Assembly Verification**: `test_app_js_bundle_assembles_cleanly` in `tests/viewer/test_ui_template.py` validates that all 31 modules assemble into a valid bundle.
- **Viewer URLs**: Build requests to viewer routes using `API` and `MEDIA` objects (defined in `00_state.js`), never bare `'/api'` or `'/media'` strings, preserving compatibility under `--base-path`. `test_app_js_bundle_has_no_bare_viewer_urls` enforces this.
- **Phone Layout**: For viewports up to 600px (`PHONE_LAYOUT_QUERY` in `00_state.js`), the chat list and detail pane operate as two levels (`css/18_phone_layout.css`). Anything opening a detail view calls `showDetailPane()` (`03_navigation.js`), and returning calls `closeDetailPane()`.
- **Headless Browser UI Testing**: Local browser tests run via standard-library Chrome DevTools Protocol (CDP) against headless Edge or Chrome:
  ```bash
  # Run UI tests
  pytest -m ui

  # Run full interactive audit CLI with visual screenshots saved to .test_artifacts/screenshots/
  python tests/ui/run_audit.py --sweep
  ```

---

## Code Style & Conventions

- **Comments & Docstrings**: Code is the primary documentation. Write concise comments in English.
- **Explicit Parameters**: Make parameters explicit; avoid flag parameters that toggle internal multi-mode behavior.
- **Clear Error Handling**: Raise explicit, specific exceptions. Never swallow exceptions with empty `except:` or `except Exception: pass` blocks.
- **Dual-Theming Token Purity**: In `app.css` and viewer styles, never hardcode hex colors on styled surfaces; always use theme CSS variables (`--bubble-in`, `--bubble-meta`, etc.).

---

## Submitting Pull Requests

1. **Create a feature branch** from `dev`:
   ```bash
   git checkout dev
   git pull origin dev
   git checkout -b feature/my-enhancement
   ```
2. **Make your changes** with clear, focused commits.
3. **Verify the Quality Checklist** locally:
   - [ ] Added unit or integration tests covering new logic or bug fixes.
   - [ ] Verified test coverage meets or exceeds **90%** on changed modules (`--cov-report=term-missing`).
   - [ ] Synthetic data standards strictly followed (zero real personal numbers, names, or device dumps).
   - [ ] Code passes Ruff formatting and linting (`ruff format --check .` and `ruff check .`).
   - [ ] All tests pass cleanly (`pytest tests/ -v`).
4. **Push your branch and open a Pull Request** targeting the `dev` branch.
