#!/bin/bash
# Подключение компьютера к агенту (macOS, Linux):
#   curl -fsSL __SERVER__/connect.sh | bash -s -- <ключ>
# Скачивает мост в ~/.agentkit и запускает первую настройку: выбор папки и автозапуск.
set -e
SERVER="__SERVER__"
KEY="${1:-$AGENTKIT_BRIDGE_KEY}"
if [ -z "$KEY" ]; then echo "Нужен ключ: скопируйте команду целиком из чата (Настройки → Компьютер)."; exit 1; fi
DIR="$HOME/.agentkit"
mkdir -p "$DIR"
echo "→ Скачиваю мост с $SERVER"
curl -fsSL "$SERVER/bridge.py" -o "$DIR/bridge.py"

PY=""
for c in python3 /usr/bin/python3 /usr/local/bin/python3 /opt/homebrew/bin/python3; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; assert sys.version_info >= (3, 9)' >/dev/null 2>&1; then
    PY="$(command -v "$c")"; break
  fi
done
if [ -z "$PY" ]; then
  if [ "$(uname)" = "Darwin" ]; then
    echo "→ Нужен Python 3 — macOS сейчас предложит установить «Инструменты разработчика»."
    echo "  Нажмите «Установить», дождитесь окончания и выполните эту же команду ещё раз."
    xcode-select --install >/dev/null 2>&1 || true
  else
    echo "→ Нужен Python 3.9+: sudo apt install python3  (или dnf install python3) — и запустите команду ещё раз."
  fi
  exit 1
fi
echo "→ Python: $PY"
exec "$PY" "$DIR/bridge.py" --server "$SERVER" --key "$KEY" --setup </dev/tty
