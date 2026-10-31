#!/usr/bin/env bash
# Run mutmut for each tool and fail when its mutation score is below the threshold.
#
# Usage: scripts/mutation-gate.sh [min-percent] [tool...]     (default 80, all tools under tools/)
#
# Each tool is mutated from inside its own folder (config in tools/<name>/pyproject.toml), because
# the tests import the tool as a top-level module. Score = (killed + timeout) / (total - skipped):
# survivors and mutants no test reaches both count against it. Needs mutmut on PATH.
# Refuse to run under zsh/sh (e.g. `zsh scripts/x` or `source scripts/x`): these scripts need bash.
[ -n "${BASH_VERSION:-}" ] || { echo "$0: run this with bash (./$(basename "$0")), not zsh or sh" >&2; exit 2; }
set -euo pipefail

min="${1:-80}"
shift || true
root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
if (($# == 0)); then
  set --
  for d in "$root"/tools/*/; do [[ -f "$d/pyproject.toml" ]] && set -- "$@" "$(basename "$d")"; done
fi

bad=0
for tool in "$@"; do
  dir="$root/tools/$tool"
  echo "== $tool: mutmut run"
  rm -rf "$dir/mutants"
  (cd "$dir" && mutmut run >/dev/null && mutmut export-cicd-stats >/dev/null)
  read -r score detail < <(python3 - "$dir/mutants/mutmut-cicd-stats.json" <<'PY'
import json, sys
s = json.load(open(sys.argv[1]))
caught, total = s["killed"] + s["timeout"], s["total"] - s["skipped"]
score = 100.0 * caught / total if total else 0.0
print(f"{score:.1f} {caught}/{total} caught; survived={s['survived']} no_tests={s['no_tests']} suspicious={s['suspicious']}")
PY
)
  if python3 -c "import sys; sys.exit(0 if float('$score') >= float('$min') else 1)"; then
    echo "$tool: mutation score $score% ($detail) >= $min%. OK."
  else
    echo "::error::$tool: mutation score $score% ($detail) is below $min%." >&2
    (cd "$dir" && mutmut results 2>/dev/null | head -50) >&2 || true
    bad=1
  fi
done
exit "$bad"
