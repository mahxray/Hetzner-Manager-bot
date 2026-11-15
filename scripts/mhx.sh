#!/usr/bin/env bash
set -euo pipefail

main() {
  local root
  root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
  cd -- "$root"
  case "${1:-help}" in
    update)
      [[ -f .env ]] || { echo 'Run bash install.sh first.' >&2; return 1; }
      command -v flock >/dev/null || { echo 'Install util-linux (flock) first.' >&2; return 1; }
      exec 9> .git/mhx-update.lock
      flock -n 9 || { echo 'Another update is already running.' >&2; return 1; }
      [[ "$(git remote get-url origin)" == 'https://github.com/mahxray/Hetzner-Manager-bot.git' ]] || {
        echo 'Unexpected Git origin; update stopped.' >&2; return 1;
      }
      [[ "$(git branch --show-current)" == main ]] || {
        echo 'Switch to main before updating.' >&2; return 1;
      }
      git diff --quiet && git diff --cached --quiet || {
        echo 'Tracked files have local changes; save them before updating.' >&2; return 1;
      }
      git fetch origin main
      git merge --ff-only origin/main
      # Build before recreation; do not stop the working container before the image is ready.
      bash "$root/install.sh"
      echo 'Update completed. Configuration and project volume were preserved.'
      ;;
    help|--help|-h)
      echo 'mhx update  Fetch main, build and restart the bot while preserving .env and project data.'
      ;;
    *) echo 'Unknown command. Usage: mhx update' >&2; return 1 ;;
  esac
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi
