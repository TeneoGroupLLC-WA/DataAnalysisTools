# DataAnalysisTools

A collection of useful data analysis tools from Teneo Group LLC: scripts and utilities for
gathering, cleaning, transforming, analyzing and visualizing data.

This README will grow as tools are added.

## Tools

_None yet. Each tool will be listed here with a short description and how to run it._

## Repository setup

- `scripts/as-bot`: runs a git/gh command as the Claude Code GitHub App bot
  (`scottthurlow-claude[bot]`), so Claude-authored PRs can be reviewed and approved normally.
  See [docs/runbooks/github-app-token.md](docs/runbooks/github-app-token.md).
- `scripts/check-bot-authorship.sh`: CI check that Claude-attributed PRs were opened by the bot.

Local configuration (`GH_APP_ID`, `GH_APP_KEY_PATH`) lives in `.envrc`, which is gitignored.

## License

See [LICENSE](LICENSE).
