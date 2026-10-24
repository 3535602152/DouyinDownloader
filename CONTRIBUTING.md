# Contributing

Use Python 3.12–3.14, uv, and Node.js 22 or 24. Windows native GUI development
also needs WebView2, .NET Framework 4.8 and Edge/Chrome.

```powershell
uv sync --locked --extra browser --extra dev --extra build
npm ci
npx playwright install chromium
uv run --locked --extra browser --extra dev python -m ruff check .
uv run --locked --extra browser --extra dev python -m pytest -q
npm test
npm run test:browser
uv run --locked --extra dev python scripts/scan_secrets.py
```

On Linux use `npx playwright install --with-deps chromium`. Browser suites use
isolated fixtures and deny external requests. `CHROMIUM_EXECUTABLE` selects an
existing browser. Python Playwright and Node Playwright are separate dependencies.

Keep changes focused and preserve the bridge API and shared ledger. Add meaningful
regression tests for behavior changes. Never commit accounts, cookies, browser
profiles, databases, media, captures, machine paths or local workbenches. Secret
fixtures require precise documented allowlists, not entire-directory exclusions.

Update dependencies intentionally with `uv lock` / `npm install --package-lock-only`,
then regenerate the pip runtime export:

```powershell
uv export --locked --extra browser --no-dev --no-emit-project --format requirements-txt --output-file requirements.txt
```

Automatic PR checks run on GitHub-hosted runners without secrets. Self-hosted
workflows are manual and may only run reviewed, trusted refs. Release builds
reject dirty worktrees. See `docs/releasing.md` for the release procedure.
