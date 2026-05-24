#!/usr/bin/env bash
# run_unit_tests.sh — corre os testes unitários (sem rede)
# Uso: ./tests/scripts/run_unit_tests.sh
# Não precisa de nenhum servidor ou lobby activo.
# Testa GameState e LocalPlayer em modo offline puro.

source "$(dirname "$0")/_lib.sh"

hr
step "Testes unitários — GameState / LocalPlayer"
info "Modo offline: sem gRPC nem Kademlia."
hr

cd "$GAME_ROOT"
uv run pytest tests/test_game_state.py -v

ok "Testes unitários concluídos."