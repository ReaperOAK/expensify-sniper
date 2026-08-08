#!/usr/bin/env bash
# Source this before any npm/node command in the Expensify App repo.
#
# The repo sets engine-strict=true and pins bun 1.3.14 / node 26.5.0 / npm 11.17.0.
# nvm's default here is node 24, which npm rejects outright, so the version has to be
# selected explicitly every time. bun is installed under the node 24 prefix and is
# symlinked into the node 26 prefix -- do NOT put the node 24 bin dir on PATH to reach
# it, because that shadows node 26 and npm fails with EBADENGINE.
export NVM_DIR="$HOME/.nvm"
# shellcheck disable=SC1091
. "$NVM_DIR/nvm.sh"
nvm use 26.5.0 >/dev/null || { echo "node 26.5.0 not installed: nvm install 26.5.0" >&2; return 1 2>/dev/null || exit 1; }

export APP_DIR="${APP_DIR:-/home/reaperoak/Documents/App}"
# rsbuild dev tries to launch a browser; the repro driver opens its own.
export BROWSER=none
export NODE_OPTIONS="--max-old-space-size=8192"
