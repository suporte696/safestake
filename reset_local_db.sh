#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

USE_DEMO_DATA="false"
FORCE="false"

for arg in "$@"; do
  case "$arg" in
    --demo)
      USE_DEMO_DATA="true"
      ;;
    --force)
      FORCE="true"
      ;;
    *)
      echo "Argumento inválido: $arg"
      echo "Uso: ./reset_local_db.sh [--demo] [--force]"
      exit 1
      ;;
  esac
done

if [[ "$FORCE" != "true" ]]; then
  echo "ATENCAO: este comando APAGA todos os dados do banco configurado no DATABASE_URL."
  read -r -p "Digite RESET para confirmar: " CONFIRMATION
  if [[ "$CONFIRMATION" != "RESET" ]]; then
    echo "Operação cancelada."
    exit 0
  fi
fi

if [[ -f "$ROOT_DIR/venv/bin/activate" ]]; then
  # shellcheck disable=SC1091
  source "$ROOT_DIR/venv/bin/activate"
fi

if [[ "$USE_DEMO_DATA" == "true" ]]; then
  python3 "$ROOT_DIR/seed.py" --with-demo-data
else
  python3 "$ROOT_DIR/seed.py"
fi

echo "Reset local concluído."
