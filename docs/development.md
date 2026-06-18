# Development

This guide is for contributors working on Baloo itself.

Use this path when you want to:

- edit code locally
- run tests and linters
- debug Baloo without Docker
- work on prompts, webhook behavior, or internals

## 1. Prerequisites

You need:

- Python `3.10+`
- `uv`
- `npm`

Optional but recommended:

- `gitleaks`

## 2. Install Dependencies

```bash
git clone https://github.com/Blue-Bear-Security/baloo-bear.git
cd baloo-bear
uv sync
npm install
cp .env.example .env
```

### Custom pi providers

The default `glm` model uses the custom `synthetic` provider, which `pi` reads
from `~/.pi/agent/models.json`. Install the committed config once so local runs
(and `scripts/local_review.py`) can resolve it:

```bash
mkdir -p ~/.pi/agent
cp pi/models.json ~/.pi/agent/models.json
```

The file references the API key only as `${SYNTHETIC_API_KEY}`, so set that in
your environment (`.env`) — no secret is stored in the file. See
[Models → Custom Providers](features/models.md#custom-providers).

## 3. Run Baloo Directly

```bash
uv run python main.py
```

This is the direct developer workflow. If you want the service-style stack with PostgreSQL, use [docs/getting-started.md](getting-started.md) instead.

## 4. Test and Lint

```bash
uv run pytest
uv run pytest --cov=baloo --cov-report=term-missing
uv run ruff check baloo tests
uv run black --check baloo tests
```

## 5. Git Hooks

The repository uses Husky and `gitleaks` for local pre-commit secret scanning.

If `gitleaks` is not installed yet:

```bash
brew install gitleaks
```

Hook setup:

```bash
npm install
```

The installed pre-commit hook runs:

```bash
gitleaks git --staged --pre-commit --no-banner --redact
```
