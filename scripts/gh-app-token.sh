#!/usr/bin/env bash
# Mint a short-lived, down-scoped GitHub App installation token for this repo.
#
# Usage:  eval "$(scripts/gh-app-token.sh)"        # exports GH_TOKEN for gh (1 hour)
#         eval "$(scripts/gh-app-token.sh --git)"   # also: commits are authored by the bot and
#                                                   # git pushes authenticate with the token
#
# The app may hold far broader permissions than we need, so the token requested here is
# limited to THIS repository and to contents + pull requests (write), checks + statuses
# (read). It cannot change settings, branch protection, secrets or workflows.
#
# Env (see .envrc, which is gitignored): GH_APP_ID, GH_APP_KEY_PATH.
# Optional: GH_APP_REPO=owner/name (default: parsed from the origin remote).
# Needs: openssl, curl, python3. The private key must be mode 600 and never lives in the repo.
set -euo pipefail

with_git=false
[[ "${1:-}" == "--git" ]] && with_git=true

err() { echo "gh-app-token: $*" >&2; exit 1; }

[[ -n "${GH_APP_ID:-}" ]] || err "GH_APP_ID not set (run direnv allow, or source .envrc)"
[[ -f "${GH_APP_KEY_PATH:-}" ]] || err "private key not found: ${GH_APP_KEY_PATH:-<unset>}"
mode=$(stat -f '%OLp' "$GH_APP_KEY_PATH" 2>/dev/null || stat -c '%a' "$GH_APP_KEY_PATH")
[[ "$mode" == 600 || "$mode" == 400 ]] || err "key permissions are $mode, expected 600"

# Prefer gh's canonical name: the origin URL can be a stale pre-rename name (API calls do not follow redirects).
repo="${GH_APP_REPO:-$(gh repo view --json nameWithOwner -q .nameWithOwner 2>/dev/null \
  || git remote get-url origin | sed -E 's#(git@github.com:|https://github.com/)##; s#\.git$##')}"
[[ "$repo" == */* ]] || err "could not determine owner/name (set GH_APP_REPO)"

b64() { openssl base64 -A | tr '+/' '-_' | tr -d '='; }
now=$(date +%s)
header=$(printf '{"alg":"RS256","typ":"JWT"}' | b64)
payload=$(printf '{"iat":%d,"exp":%d,"iss":"%s"}' $((now - 60)) $((now + 300)) "$GH_APP_ID" | b64)
sig=$(printf '%s.%s' "$header" "$payload" | openssl dgst -sha256 -sign "$GH_APP_KEY_PATH" | b64)
jwt="$header.$payload.$sig"

api() { # method path [json-body]
  curl -sf --connect-timeout 10 --max-time 30 -X "$1" \
    -H "Authorization: Bearer $jwt" -H "Accept: application/vnd.github+json" \
    -H "X-GitHub-Api-Version: 2022-11-28" ${3:+-d "$3"} "https://api.github.com$2"
}

install_id=$(api GET "/repos/$repo/installation" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])' 2>/dev/null) \
  || err "app is not installed on $repo"

body=$(python3 -c '
import json, sys
print(json.dumps({
    "repositories": [sys.argv[1].split("/", 1)[1]],
    "permissions": {"contents": "write", "pull_requests": "write", "checks": "read", "statuses": "read"},
}))' "$repo")

resp=$(api POST "/app/installations/$install_id/access_tokens" "$body") || err "token request failed"
token=$(printf '%s' "$resp" | python3 -c 'import json,sys; print(json.load(sys.stdin)["token"])')
exp=$(printf '%s' "$resp" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("expires_at",""))')

echo "gh-app-token: token for $repo, expires $exp" >&2
printf "export GH_TOKEN='%s'\n" "$token"

if $with_git; then
  slug=$(api GET /app | python3 -c 'import json,sys; print(json.load(sys.stdin)["slug"])') \
    || err "could not read app slug"
  bot="${slug}[bot]"
  # Authenticated: the anonymous limit is 60 calls/hour per IP and was exhausted by repeated runs (403).
  bot_id=$(curl -sf -H "Authorization: Bearer ${token}" "https://api.github.com/users/${slug}%5Bbot%5D" \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])') || err "bot user not found"
  email="${bot_id}+${bot}@users.noreply.github.com"
  printf "export GIT_AUTHOR_NAME='%s' GIT_AUTHOR_EMAIL='%s'\n" "$bot" "$email"
  printf "export GIT_COMMITTER_NAME='%s' GIT_COMMITTER_EMAIL='%s'\n" "$bot" "$email"
  # Use gh's credential helper (reads GH_TOKEN) instead of the stored user credential.
  printf "export GIT_CONFIG_COUNT=2\n"
  printf "export GIT_CONFIG_KEY_0=credential.helper GIT_CONFIG_VALUE_0=\n"
  printf "export GIT_CONFIG_KEY_1=credential.helper GIT_CONFIG_VALUE_1='!gh auth git-credential'\n"
  echo "gh-app-token: git identity $bot <$email>" >&2
fi
