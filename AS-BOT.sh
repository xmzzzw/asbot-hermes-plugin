#!/bin/sh
set -eu
folder=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
base=${HERMES_HOME:-"$HOME/.hermes"}
for python in "${HERMES_PYTHON:-}" "$base/hermes-agent/venv/bin/python" "$base/hermes-agent/.venv/bin/python" "$HOME/.hermes/hermes-agent/venv/bin/python" "$HOME/.hermes/hermes-agent/.venv/bin/python"; do
    if [ -n "$python" ] && [ -x "$python" ]; then
        exec "$python" -I -B "$folder/bin/plugin.py" assist-welcome
    fi
done
printf '%s\n' 'Select the Python from your existing Hermes installation.' >&2
printf 'Hermes Python path: ' >&2
IFS= read -r python
[ -x "$python" ] || exit 1
exec "$python" -I -B "$folder/bin/plugin.py" assist-welcome
