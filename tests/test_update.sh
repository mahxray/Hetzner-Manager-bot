#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
work="$(mktemp -d)"
trap 'rm -rf -- "$work"' EXIT
project="$work/path with spaces"
mkdir -p "$project/scripts" "$project/.git"
cp scripts/mhx.sh "$project/scripts/mhx.sh"
printf 'preserved-config\n' > "$project/.env"
printf '#!/usr/bin/env bash\nprintf "installed\\n" >> "$MHX_LOG"\n' > "$project/install.sh"
export MHX_LOG="$work/commands"

run_case() (
  local mode="$1"
  source "$project/scripts/mhx.sh"
  git() {
    printf '%s\n' "$*" >> "$MHX_LOG"
    case "$*" in
      'remote get-url origin') echo 'https://github.com/mahxray/Hetzner-Manager-bot.git' ;;
      'branch --show-current') echo main ;;
      'diff --quiet') [[ "$mode" != dirty ]] ;;
      'fetch origin main') [[ "$mode" != network-failure ]] ;;
      *) return 0 ;;
    esac
  }
  flock() { [[ "$mode" != locked ]]; }
  main update
)

run_case success
grep -q '^fetch origin main$' "$MHX_LOG"
grep -q '^merge --ff-only origin/main$' "$MHX_LOG"
grep -q '^installed$' "$MHX_LOG"
grep -q '^preserved-config$' "$project/.env"
for mode in dirty network-failure locked; do
  : > "$MHX_LOG"
  # Run in a separate shell so the installer's set -e remains active on failure.
  export -f run_case
  export project mode
  if bash -c 'set -euo pipefail; run_case "$mode"'; then
    echo "Expected failure: $mode" >&2; exit 1
  fi
  if grep -q '^installed$' "$MHX_LOG"; then
    echo 'Restarted after failed update' >&2; exit 1
  fi
done
echo 'Update checks passed.'
