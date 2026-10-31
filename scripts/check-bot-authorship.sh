#!/usr/bin/env bash
# Fail when a Claude-attributed PR was not authored by the GitHub App bot.
#
# Usage: scripts/check-bot-authorship.sh <pr-number> [owner/repo]     (needs gh + GH_TOKEN)
#
# "Claude-attributed" = the PR body has the "Generated with Claude Code" line or any commit has a
# "Co-Authored-By: Claude" trailer. Such a PR must be opened by a *[bot] account and its commits
# authored by one, so a human can review and approve it. Runs in CI (.github/workflows/bot-authorship.yml).
# Refuse to run under zsh/sh (e.g. `zsh scripts/x` or `source scripts/x`): these scripts need bash.
[ -n "${BASH_VERSION:-}" ] || { echo "$0: run this with bash (./$(basename "$0")), not zsh or sh" >&2; exit 2; }
set -euo pipefail

pr="${1:?usage: check-bot-authorship.sh <pr-number> [owner/repo]}"
repo="${2:-${GITHUB_REPOSITORY:-$(gh repo view --json nameWithOwner -q .nameWithOwner)}}"

author=$(gh api "repos/$repo/pulls/$pr" --jq .user.login)
body=$(gh api "repos/$repo/pulls/$pr" --jq '.body // ""')
messages=$(gh api --paginate "repos/$repo/pulls/$pr/commits" --jq '.[].commit.message')
authors=$(gh api --paginate "repos/$repo/pulls/$pr/commits" --jq '.[] | "\(.sha[0:7]) \(.commit.author.name) <\(.commit.author.email)>"')

claude=false
grep -qiE 'Generated with \[?Claude Code' <<<"$body" && claude=true
grep -qiE '^Co-Authored-By: Claude' <<<"$messages" && claude=true

if ! $claude; then
  echo "PR #$pr is not Claude-attributed; nothing to check."
  exit 0
fi

bad=0
if [[ "$author" != *"[bot]" ]]; then
  echo "::error::PR #$pr is Claude-attributed but opened by '$author', not a bot." >&2
  bad=1
fi
while IFS= read -r line; do
  [[ -z "$line" ]] && continue
  if [[ "$line" != *"[bot]@users.noreply.github.com>" ]]; then
    echo "::error::Claude-attributed commit not authored by the bot: $line" >&2
    bad=1
  fi
done <<<"$authors"

((bad)) && exit 1
echo "PR #$pr: Claude-attributed and authored by the bot ($author). OK."
