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

if [ ! -d "$ROOT_DIR/.venv-sdn" ]; then
  "$UV" venv --python 3.12 --seed "$ROOT_DIR/.venv-sdn"
fi

export PYTHONPATH="$ROOT_DIR/ryu_build_compat${PYTHONPATH:+:$PYTHONPATH}"
"$UV" pip install \
  --python "$ROOT_DIR/.venv-sdn/bin/python" \
  -r "$ROOT_DIR/requirements-sdn.txt"
"$UV" pip install \
  --python "$ROOT_DIR/.venv-sdn/bin/python" \
  -r "$ROOT_DIR/requirements-chat.txt"

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  echo "Chat and Ryu environment ready."
  echo "Activate it with: source $ROOT_DIR/.venv-sdn/bin/activate"
  echo "If Mininet needs xterm, keep this shell PATH set: export PATH=\"$HOME/.local/bin:\$PATH\""
  echo "and export LD_LIBRARY_PATH=\"$HOME/.local/opt/xterm/usr/lib/x86_64-linux-gnu\${LD_LIBRARY_PATH:+:\$LD_LIBRARY_PATH}\""
  echo "Controller: $ROOT_DIR/.venv-sdn/bin/python sdn/chat_qos_controller.py --ofp-tcp-listen-port 6653 sdn/chat_qos_controller.py"
else
  echo "Environment ready in this shell."
  echo "Use: source $ROOT_DIR/.venv-sdn/bin/activate"
fi
