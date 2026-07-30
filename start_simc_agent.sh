#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"
# The Python entrypoint prompts for the two required values only when this file
# is absent and stdin is interactive; unattended launches fail closed instead.
CONFIG_PATH="${SIMC_AGENT_CONFIG:-$SCRIPT_DIR/simc_agent.json}"
exec "$PYTHON_BIN" "$SCRIPT_DIR/simc_agent_consumer.py" --config "$CONFIG_PATH" "$@"
