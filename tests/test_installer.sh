#!/usr/bin/env bash
# Isolated command stubs: no package installs, service changes or network requests.
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
source ./install.sh
work="$(mktemp -d)"
trap 'rm -rf -- "$work"' EXIT

printf 'ID=ubuntu\nVERSION_CODENAME=noble\n' > "$work/release"
detect_platform "$work/release"
[[ "$DOCKER_DISTRO:$DOCKER_CODENAME" == ubuntu:noble ]]
printf 'ID=debian\nVERSION_CODENAME=trixie\n' > "$work/release"
detect_platform "$work/release"
[[ "$DOCKER_DISTRO:$DOCKER_CODENAME" == debian:trixie ]]
printf 'ID=fedora\nVERSION_CODENAME=unknown\n' > "$work/release"
if detect_platform "$work/release" 2>/dev/null; then
  echo 'Unsupported distribution accepted' >&2; exit 1
fi

# Already working Docker: do not modify system packages.
(
  docker() { return 0; }
  run_root() { echo 'Unexpected privileged change' >&2; exit 1; }
  DOCKER_COMMAND=(docker)
  ensure_docker
)

# Compose missing: install only its plugin, keep the existing engine.
(
  ready=0
  docker() {
    if [[ "$*" == 'compose version' ]] && (( ready == 0 )); then return 1; fi
    return 0
  }
  detect_platform() { DOCKER_DISTRO=ubuntu; DOCKER_CODENAME=noble; }
  configure_repository() { echo repository >> "$work/compose-log"; }
  run_root() {
    printf '%s\n' "$*" >> "$work/compose-log"
    if [[ "$*" == 'apt-get --yes --no-remove install docker-compose-plugin' ]]; then ready=1; fi
  }
  DOCKER_COMMAND=(docker)
  ensure_docker
  grep -q '^apt-get --yes --no-remove install docker-compose-plugin$' "$work/compose-log"
  ! grep -q 'install docker-ce' "$work/compose-log"
)

# Clean machine: Docker and Compose installed together, then daemon started.
(
  installed=0
  running=0
  command() {
    if [[ "$*" == '-v docker' ]] && (( installed == 0 )); then return 1; fi
    builtin command "$@"
  }
  docker() {
    if [[ "$*" == info ]] && (( running == 0 )); then return 1; fi
    return 0
  }
  detect_platform() { DOCKER_DISTRO=debian; DOCKER_CODENAME=bookworm; }
  configure_repository() { echo repository >> "$work/clean-log"; }
  run_root() {
    printf '%s\n' "$*" >> "$work/clean-log"
    case "$*" in
      'apt-get --yes --no-remove install docker-ce '*) installed=1 ;;
      'docker info') docker info; return $? ;;
      'systemctl enable --now docker') running=1 ;;
    esac
    return 0
  }
  DOCKER_COMMAND=(docker)
  ensure_docker
  grep -q '^apt-get --yes --no-remove install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin$' "$work/clean-log"
  grep -q '^systemctl enable --now docker$' "$work/clean-log"
)

# Existing daemon accessible only with sudo: use it without reinstalling Docker.
(
  docker() { [[ "$*" != info ]]; }
  run_root() {
    [[ "$*" == 'docker info' ]] || { echo 'Unexpected privileged change' >&2; exit 1; }
  }
  DOCKER_COMMAND=(docker)
  ensure_docker
  [[ "${DOCKER_COMMAND[*]}" == 'run_root docker' ]]
)
echo 'Installer checks passed.'
