# Contributing

Thanks for helping. applybot is a small, local, review-first tool; the design
rules in the README ("The LLM extracts; plain code decides", nothing sent
without approval, letters grounded only in `facts.yaml`) are what keep it safe,
so changes that weaken them will not be merged.

## Development

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python -m pytest        # no network or LLM needed
ruff check src tests
```

- The test suite builds its own home directory from the committed `*.example`
  files. It never reads your personal `config/*.yaml`, CVs or database.
- Add a test with every behaviour change. Tests use a fake LLM; do not call
  real models or job boards from tests.
- Keep personal data out of the repository: no real names, emails, CVs, API
  keys or passwords, not even in examples or test data.

## Pull requests

Open an issue first for anything large. Keep PRs focused, describe what and why,
and make sure CI is green.
