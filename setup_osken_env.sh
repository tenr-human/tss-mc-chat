#!/usr/bin/env bash
set -e

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

export PATH="$HOME/.local/bin:$PATH"

if [ -d "$HOME/.local/opt/xterm/usr/lib/x86_64-linux-gnu" ]; then
  export LD_LIBRARY_PATH="$HOME/.local/opt/xterm/usr/lib/x86_64-linux-gnu${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
fi

UV_VERSION="0.13.7"
if ! command -v uv >/dev/null 2>&1 ||
  [ "$(uv --version | awk '{print $2}')" != "$UV_VERSION" ]; then
  python3 -m pip install --user --break-system-packages --upgrade \
    "uv==$UV_VERSION"
fi
UV="$(command -v uv)"

if ! "$UV" python find 3.12.16 >/dev/null 2>&1; then
  "$UV" python install 3.12.16
fi

VENV_DIR="$ROOT_DIR/.venv-sdn"
if [ ! -x "$VENV_DIR/bin/python" ]; then
  "$UV" venv --clear --python 3.12.16 --seed "$VENV_DIR"
elif [ "$(
    "$VENV_DIR/bin/python" -c 'import platform; print(platform.python_version())'
  )" != "3.12.16" ]; then
  "$UV" venv --clear --python 3.12.16 --seed "$VENV_DIR"
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
  echo "Controller: $ROOT_DIR/.venv-sdn/bin/python sdn/run_controller.py --ofp-tcp-listen-port 6653"
else
  echo "Environment ready in this shell."
  echo "Use: source $ROOT_DIR/.venv-sdn/bin/activate"
fi
