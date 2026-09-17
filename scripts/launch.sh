#!/usr/bin/env bash
# COWMATA Tail-Ring Annotator - launcher for development environments
set -euo pipefail
cd "$(dirname "$0")/.."
exec python3 -m cowmata_tailring "$@"
