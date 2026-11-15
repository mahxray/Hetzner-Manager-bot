#!/usr/bin/env bash
set -euo pipefail

run_root() {
  if (( EUID == 0 )); then
    "$@"
  else
    command -v sudo >/dev/null || { echo 'Root or sudo is required for this step.' >&2; return 1; }
    sudo -- "$@"
  fi
}

detect_platform() {
  local release_file="${1:-/etc/os-release}"
  [[ -r "$release_file" ]] || { echo 'Cannot detect Linux distribution.' >&2; return 1; }
  local ID='' VERSION_CODENAME='' UBUNTU_CODENAME=''
  # /etc/os-release is supplied by the operating system.
  . "$release_file"
  DOCKER_DISTRO="$ID"
  DOCKER_CODENAME="${UBUNTU_CODENAME:-$VERSION_CODENAME}"
  case "$DOCKER_DISTRO:$DOCKER_CODENAME" in
    ubuntu:jammy|ubuntu:noble|ubuntu:resolute|ubuntu:stonking|debian:bookworm|debian:trixie) ;;
    *) echo 'Automatic installation supports Ubuntu 22.04/24.04/26.04/26.10 and Debian 12/13. Install Docker and Compose manually on other systems.' >&2; return 1 ;;
  esac
}

configure_repository() {
  local architecture
  architecture="$(dpkg --print-architecture)"
  # Preserve an existing Docker repository and its signing configuration.
  if ! grep -qs 'download.docker.com/linux/' /etc/apt/sources.list /etc/apt/sources.list.d/*.list /etc/apt/sources.list.d/*.sources; then
    run_root install -m 0755 -d /etc/apt/keyrings
    run_root curl --fail --silent --show-error --location \
      "https://download.docker.com/linux/$DOCKER_DISTRO/gpg" -o /etc/apt/keyrings/docker.asc
    run_root chmod a+r /etc/apt/keyrings/docker.asc
    printf 'Types: deb\nURIs: https://download.docker.com/linux/%s\nSuites: %s\nComponents: stable\nArchitectures: %s\nSigned-By: /etc/apt/keyrings/docker.asc\n' \
      "$DOCKER_DISTRO" "$DOCKER_CODENAME" "$architecture" \
      | run_root tee /etc/apt/sources.list.d/hetzner-manager-docker.sources >/dev/null
  fi
}

ensure_docker() {
  local packages=()
  if ! command -v docker >/dev/null; then
    packages=(docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin)
  elif ! docker compose version >/dev/null 2>&1; then
    packages=(docker-compose-plugin)
  fi
  if (( ${#packages[@]} )); then
    detect_platform
    echo 'Installing Docker prerequisites from the official Docker APT repository...'
    run_root apt-get update
    run_root apt-get --yes --no-remove install ca-certificates curl python3
    configure_repository
    run_root apt-get update
    # Refuse removal of existing packages (e.g. a conflicting distro Docker installation).
    run_root apt-get --yes --no-remove install "${packages[@]}"
  fi
  docker compose version >/dev/null
  if ! docker info >/dev/null 2>&1; then
    if ! run_root docker info >/dev/null 2>&1; then
      run_root systemctl enable --now docker
    fi
    # Use sudo for this script only; no automatic docker-group membership changes.
    if ! docker info >/dev/null 2>&1; then
      DOCKER_COMMAND=(run_root docker)
    fi
  fi
  "${DOCKER_COMMAND[@]}" info >/dev/null
}

install_cli() {
  local destination="${1:-/usr/local/bin/mhx}" script
  script="$(pwd -P)/scripts/mhx.sh"
  if [[ -e "$destination" ]] && ! grep -qs '^# Hetzner Manager Bot CLI$' "$destination"; then
    echo "An unrelated command already exists at $destination; refusing to overwrite it." >&2
    return 1
  fi
  run_root install -d -m 0755 "$(dirname -- "$destination")"
  printf '#!/usr/bin/env bash\n# Hetzner Manager Bot CLI\nexec bash %q "$@"\n' "$script" \
    | run_root tee "$destination" >/dev/null
  run_root chmod 0755 "$destination"
}

main() {
  cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
  DOCKER_COMMAND=(docker)
  ensure_docker
  if ! command -v python3 >/dev/null; then
    detect_platform
    run_root apt-get update
    run_root apt-get --yes --no-remove install python3
  fi
  if [[ ! -f .env ]]; then
    python3 scripts/setup.py
  fi
  chmod 600 .env
  "${DOCKER_COMMAND[@]}" compose up -d --build
  install_cli
  echo 'Started. In Telegram open your bot and send /start.'
  echo 'Next update: mhx update'
  echo 'Logs: docker compose logs --tail=50 bot (use sudo if required).'
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi
