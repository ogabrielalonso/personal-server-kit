#!/usr/bin/env bash
# personal-server-kit bootstrap.
#
#   curl -fsSL https://raw.githubusercontent.com/ogabrielalonso/personal-server-kit/main/install.sh | bash
#   ./install.sh                 (from a copy of the repository)
#   ./install.sh --answers FILE  (unattended; see docs/INSTALLER.md)
#
# It only finds a suitable Python and the kit's code, then hands over to
# `python3 -m pskit install`, which asks the questions. Nothing is changed
# on the machine by this script itself. Works with the bash 3.2 of macOS.

set -euo pipefail

REPO="${PSKIT_REPO:-ogabrielalonso/personal-server-kit}"
REF="${PSKIT_REF:-main}"
TARBALL_SHA256="${PSKIT_SHA256:-}"

say() { printf '%s\n' "$*" >&2; }
die() { say "pskit: $*"; exit 1; }

python_ok() {
  "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null
}

find_python() {
  local candidate
  case "$(uname -s)" in
    Linux)
      for candidate in /usr/bin/python3 python3; do
        if command -v "$candidate" >/dev/null 2>&1 && python_ok "$candidate"; then
          command -v "$candidate"; return 0
        fi
      done
      ;;
    Darwin)
      for candidate in /opt/homebrew/bin/python3 /usr/local/bin/python3; do
        if [ -x "$candidate" ] && python_ok "$candidate"; then
          printf '%s\n' "$candidate"; return 0
        fi
      done
      # /usr/bin/python3 is a stub that pops up an install dialog when the
      # command line tools are missing: only call it when they are present.
      if xcode-select -p >/dev/null 2>&1 && python_ok /usr/bin/python3; then
        printf '%s\n' /usr/bin/python3; return 0
      fi
      ;;
  esac
  return 1
}

explain_python() {
  case "$(uname -s)" in
    Darwin)
      say "Python 3 was not found. Install Apple's command line tools, then run this again:"
      say "    xcode-select --install"
      say "(Python 3 nao encontrado. Instale as ferramentas de linha de comando da Apple e rode de novo.)"
      ;;
    *)
      say "Python 3.9 or newer was not found. On Ubuntu:"
      say "    sudo apt-get update && sudo apt-get install -y python3"
      say "(Python 3.9 ou mais novo nao encontrado.)"
      ;;
  esac
}

source_dir_from_checkout() {
  local dir here
  dir="$(dirname "${BASH_SOURCE[0]:-$0}")"
  here="$(cd "$dir" 2>/dev/null && pwd)" || return 1
  if [ -f "$here/pskit/__init__.py" ]; then
    printf '%s\n' "$here"
    return 0
  fi
  return 1
}

sha256_of() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | awk '{print $1}'
  else
    shasum -a 256 "$1" | awk '{print $1}'
  fi
}

download_source() {
  local tmp url tarball top
  tmp="$(mktemp -d "${TMPDIR:-/tmp}/pskit.XXXXXX")"
  url="https://github.com/${REPO}/archive/${REF}.tar.gz"
  tarball="$tmp/src.tar.gz"
  say "Downloading ${REPO}@${REF} ..."
  curl -fsSL --retry 3 -o "$tarball" "$url" || die "download failed: $url"
  if [ -n "$TARBALL_SHA256" ]; then
    [ "$(sha256_of "$tarball")" = "$TARBALL_SHA256" ] || die "checksum mismatch for $url"
  fi
  tar -xzf "$tarball" -C "$tmp"
  top="$(find "$tmp" -mindepth 1 -maxdepth 1 -type d | head -n 1)"
  [ -f "$top/pskit/__init__.py" ] || die "the download does not contain the kit"
  printf '%s\n' "$top"
}

main() {
  case "$(uname -s)" in
    Linux|Darwin) ;;
    *) die "unsupported system: $(uname -s)" ;;
  esac
  local py src
  if ! py="$(find_python)"; then
    explain_python
    exit 1
  fi
  if ! src="$(source_dir_from_checkout)"; then
    src="$(download_source)"
  fi
  export PYTHONPATH="$src"
  export PYTHONDONTWRITEBYTECODE=1
  exec "$py" -B -m pskit install "$@"
}

main "$@"
