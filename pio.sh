#!/usr/bin/env bash
# PlatformIO for firmware/, with its toolchain kept inside this project.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
export PLATFORMIO_CORE_DIR="$here/.pio-core"
cd "$here/firmware"
exec "$here/.venv/bin/pio" "$@"
