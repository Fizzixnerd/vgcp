#!/usr/bin/env bash
# selftest.sh: validate the Video Game Control Protocol (VGCP) wire protocol end-to-end with
# NO Godot.
#
#   1. Python:  control.py CLI/client  <->  mock_server.py            (always runs)
#   2. Node:    TypeScript MCP server   <->  mock_server.py           (only if node present)
#
# Both prove ping/pause/step/screenshot/get_state round-trips against a faithful mock.

set -uo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
rc=0

echo "=============================================================="
echo " VGCP self-test: Python driver vs mock server (no Godot)"
echo "=============================================================="
python3 "$DIR/selftest.py" || rc=1

echo
echo "=============================================================="
echo " VGCP self-test: TypeScript MCP server vs mock server"
echo "=============================================================="
if command -v node >/dev/null 2>&1; then
  if [ ! -d "$DIR/node_modules" ]; then
    echo "  installing npm deps..."
    ( cd "$DIR" && npm ci --silent ) || { echo "  npm ci failed"; rc=1; }
  fi
  echo "  type-checking (tsc --noEmit)..."
  ( cd "$DIR" && npx --no-install tsc --noEmit ) && echo "  PASS  tsc --noEmit" || { echo "  FAIL  tsc --noEmit"; rc=1; }
  echo "  building (tsc)..."
  ( cd "$DIR" && npx --no-install tsc ) || { echo "  FAIL  tsc build"; rc=1; }
  echo "  running MCP end-to-end self-test..."
  ( cd "$DIR" && node test/mcp_selftest.mjs ) || rc=1
else
  echo "  SKIP: node not on PATH (run 'npm ci && npm run build && npm run selftest' where node exists)."
fi

echo
if [ "$rc" -eq 0 ]; then echo "OVERALL: ALL PASS"; else echo "OVERALL: FAILURES (rc=$rc)"; fi
exit "$rc"
