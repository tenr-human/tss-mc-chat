#!/usr/bin/env bash
set -e

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

export PATH="$HOME/.local/bin:$PATH"

if [ -d "$HOME/.local/opt/xterm/usr/lib/x86_64-linux-gnu" ]; then
  export LD_LIBRARY_PATH="$HOME/.local/opt/xterm/usr/lib/x86_64-linux-gnu${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
fi

if ! command -v uv >/dev/null 2>&1; then
  python3 -m pip install --user --break-system-packages uv
fi
UV="$(command -v uv)"

if ! "$UV" python find 3.12 >/dev/null 2>&1; then
  "$UV" python install 3.12
fi

VENV_DIR="$ROOT_DIR/.venv-sdn"
if [ ! -d "$VENV_DIR" ]; then
  "$UV" venv --python 3.12 --seed "$VENV_DIR"
elif [ ! -x "$VENV_DIR/bin/python" ] ||
  { [ -x "$VENV_DIR/bin/osken-manager" ] &&
    [ "$(head -n 1 "$VENV_DIR/bin/osken-manager")" != "#!$VENV_DIR/bin/python" ]; }; then
  "$UV" venv --clear --python 3.12 --seed "$VENV_DIR"
fi

"$UV" pip install \
  --python "$VENV_DIR/bin/python" \
  -r "$ROOT_DIR/requirements-sdn.txt"
"$UV" pip install \
  --python "$VENV_DIR/bin/python" \
  -r "$ROOT_DIR/requirements-chat.txt"

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  echo "Chat and OS-Ken environment ready."
  echo "Activate it with: source $ROOT_DIR/.venv-sdn/bin/activate"
  echo "Controller: $ROOT_DIR/.venv-sdn/bin/osken-manager --ofp-tcp-listen-port 6653 sdn/chat_qos_controller.py"
else
  echo "Environment ready in this shell."
  echo "Use: source $ROOT_DIR/.venv-sdn/bin/activate"
fi
