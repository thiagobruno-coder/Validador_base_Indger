#!/usr/bin/env bash
# Abre o painel da busca ANEEL (Linux/macOS). No Windows, use iniciar_painel.bat.
set -e
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  echo "Primeira execução: preparando o ambiente (leva alguns minutos)…"
  python3 -m venv .venv
  .venv/bin/python -m pip install --upgrade pip
  .venv/bin/python -m pip install -r requirements.txt || { rm -rf .venv; echo "Falha ao instalar as dependências."; exit 1; }
fi
exec .venv/bin/python -m aneel_busca painel "$@"
