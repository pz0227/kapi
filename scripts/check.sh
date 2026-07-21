#!/usr/bin/env bash
# Pre-push sanity check: run the test suite in a CLEAN minimal venv, exactly
# like CI does, so import coupling the dev machine masks (a heavy dep leaking
# into a test) is caught locally instead of turning the CI badge red.
#
#   ./scripts/check.sh
#
set -euo pipefail
cd "$(dirname "$0")/../analytics-backend"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

echo "→ creating clean venv with lightweight test deps..."
python3 -m venv "$TMP/venv"
"$TMP/venv/bin/pip" install --quiet --upgrade pip
"$TMP/venv/bin/pip" install --quiet -r requirements-test.txt

echo "→ running unit tests..."
"$TMP/venv/bin/python" -m pytest tests/ -q

echo "→ running offline eval suites..."
"$TMP/venv/bin/python" -m services.eval.router_offline_eval >/dev/null
"$TMP/venv/bin/python" -m services.eval.holdout_eval >/dev/null

echo "✓ all green in a clean env — safe to push."
