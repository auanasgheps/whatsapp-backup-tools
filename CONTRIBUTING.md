# Contributing to WhatsApp Backup Tools

Thank you for your interest in contributing to WhatsApp Backup Tools (`wab-tools`)!

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

```bash
pytest tests/ -v
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

---

## Submitting Pull Requests

1. Create a feature branch from `dev`:
   ```bash
   git checkout -b feature/my-enhancement
   ```
2. Make your changes with clear, concise commit messages.
3. Ensure all tests and lint checks pass locally.
4. Push your branch and open a Pull Request against `dev`.
