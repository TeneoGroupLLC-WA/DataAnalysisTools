# DataAnalysisTools

A collection of useful data analysis tools from Teneo Group LLC: scripts and utilities for
gathering, cleaning, transforming, analyzing and visualizing data.

This README will grow as tools are added.

## Tools

Each tool lives in its own folder under `tools/`, with a README covering setup and usage.

| Tool | What it does |
|---|---|
| [zip4](tools/zip4/README.md) | Adds ZIP+4 codes to a CSV of addresses (or one address) using the USPS Addresses API. |
| [geocode](tools/geocode/README.md) | Adds latitude/longitude and Census areas (county, tract, block group, block) to a CSV of addresses, using the free Census Bureau batch geocoder. Suited to large files and geographic clustering. |

## Quality gates

CI (`.github/workflows/ci.yml`) runs on every pull request, and each job must pass before merging:

| Check | Tool | Passes when |
|---|---|---|
| Lint | [ruff](https://docs.astral.sh/ruff/) | no findings |
| Security scan | [bandit](https://bandit.readthedocs.io/) | no findings at any severity |
| Tests (Python 3.10, 3.13) | pytest + [coverage](https://coverage.readthedocs.io/) | all tests pass, line + branch coverage >= 80% |
| Mutation score | [mutmut](https://mutmut.readthedocs.io/) | each tool's tests catch >= 80% of mutants |

Settings live in `pyproject.toml`. Each tool also has its own `tools/<name>/pyproject.toml` for mutmut, which
runs from inside the tool's folder because the tests import the tool as a top-level module. Run the gates
locally:

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
export PATH="$PWD/.venv/bin:$PATH"
ruff check
bandit -c pyproject.toml -r tools
coverage run -m pytest && coverage report
scripts/mutation-gate.sh 80        # about 30 seconds; add tool names to run only some
```

A bandit false positive gets `# nosec <id>` with a comment saying why. A mutant that cannot change behavior
(an equivalent mutant) can get `# pragma: no mutate` with a reason; keep those rare.

The tools stay standard library only: `requirements-dev.txt` is for these checks, not for running the tools.

## Repository setup

- `scripts/as-bot`: runs a git/gh command as the Claude Code GitHub App bot
  (`scottthurlow-claude[bot]`), so Claude-authored PRs can be reviewed and approved normally.
  See [docs/runbooks/github-app-token.md](docs/runbooks/github-app-token.md).
- `scripts/check-bot-authorship.sh`: CI check that Claude-attributed PRs were opened by the bot.

Local configuration (`GH_APP_ID`, `GH_APP_KEY_PATH`) lives in `.envrc`, which is gitignored.

## License

See [LICENSE](LICENSE).
