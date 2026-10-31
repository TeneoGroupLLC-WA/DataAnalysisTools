# DataAnalysisTools

A collection of useful data analysis tools from Teneo Group LLC: scripts and utilities for
gathering, cleaning, transforming, analyzing and visualizing data.

This README will grow as tools are added.

## Tools

Each tool lives in its own folder under `tools/`, with a README covering setup and usage.

| Tool | What it does |
|---|---|
| [zip4](tools/zip4/README.md) | Adds ZIP+4 codes to a CSV of addresses (or one address) using the USPS Addresses API. |

## Repository setup

- `scripts/as-bot`: runs a git/gh command as the Claude Code GitHub App bot
  (`scottthurlow-claude[bot]`), so Claude-authored PRs can be reviewed and approved normally.
  See [docs/runbooks/github-app-token.md](docs/runbooks/github-app-token.md).
- `scripts/check-bot-authorship.sh`: CI check that Claude-attributed PRs were opened by the bot.

Local configuration (`GH_APP_ID`, `GH_APP_KEY_PATH`) lives in `.envrc`, which is gitignored.

## License

See [LICENSE](LICENSE).
