# steward project conventions

## Location
/Users/sven/git/steward

## Python tooling
- Always use `uv` for Python commands. Never use `pip`, `python`, or `pytest` directly.
- Run tests: `uv run pytest tests/ -v --tb=short`
- Lint: `uv run ruff check steward.py metrics_server.py tests/`
- Format check: `uv run ruff format --check steward.py metrics_server.py tests/`

## Post-task validation
After completing any task, always run:
1. `uv run ruff check steward.py metrics_server.py tests/`
2. `uv run pytest tests/ -v --tb=short`

Fix any issues before considering the task done.

## CI pipeline validation
Whenever `.github/workflows/build.yml` (or any file under `.github/workflows/`) is changed, always run both of the following before considering the task done:

1. `actionlint .github/workflows/build.yml` — catches structural and syntax errors fast
2. `act --dryrun` — resolves action versions against GitHub, catches bad version references

Fix any issues reported before finishing.
