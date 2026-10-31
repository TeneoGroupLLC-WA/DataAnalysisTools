# Runbook: act as the GitHub App bot (Claude Code)

Claude Code opens PRs as **`scottthurlow-claude[bot]`** (GitHub App *ScottThurlow-Claude*, ID 4410817),
so the PR author is not you and you can review and approve it normally.

## Why a down-scoped token

The app is installed on all repos of the account with broad permissions (administration, secrets,
workflows, ...). `scripts/gh-app-token.sh` therefore requests a token limited to **this repo** and to
contents + pull requests (write), checks + statuses (read). That token cannot change branch
protection, secrets or workflows, and cannot reach other repos.

## Use: `scripts/as-bot` (fails closed)

Prefix any git/gh command that must be the bot's:

```bash
scripts/as-bot git commit -m "..."
scripts/as-bot git push https://github.com/TeneoGroupLLC-WA/DataAnalysisTools.git HEAD:refs/heads/my-branch
scripts/as-bot gh pr create ...
```

It mints a fresh token, checks the identity really is a `[bot]`, and only then runs the command. If
anything fails (missing key, wrong permissions, app not installed, no network) the command is **not run**,
so nothing can fall back to your own git/gh credentials. Run it from a checkout that has the script (a
worktree off `origin/main`); in an older checkout `scripts/as-bot` does not exist and nothing runs.
`GH_APP_ID` / `GH_APP_KEY_PATH` come from the environment or the main checkout's `.envrc`.

The older `eval "$(scripts/gh-app-token.sh --git)"` still works but **fails open**: if the script is missing or
errors, `eval` of empty output succeeds and the commands that follow run as you. That is how PRs ended up
authored by the user in the HomeAssistant repo. Prefer `as-bot`.

## Safety net in CI

`scripts/check-bot-authorship.sh <pr>` fails a PR whose body or commits carry Claude attribution unless it was
opened by a `[bot]` account with bot-authored commits. Run it from a workflow on `pull_request`
(`GH_TOKEN: ${{ github.token }}`, permissions `contents: read`, `pull-requests: read`) and add that check to the
required checks on `main` to make it block merging. The workflow file must be committed by a human: the bot token
deliberately cannot create or change workflows.

The private key lives outside the repo (`~/.local/credentials/scottthurlow-claude.private-key.pem`, mode 600).

## Branch protection (set with your own login, not the bot)

On `main`: require a pull request with 1 approval, require the Validate and CI checks, do not add the app
as a bypass actor.

## Notes

- Approvals from the bot never count as human review.
- If the script says "app is not installed on ...", add the repo under the app's installation settings.
- A stale `origin` URL (pre-rename repo name) is why the script asks `gh` for the canonical name.
