#!/usr/bin/env bash
# run_unit_test.sh — corre os testes unitários
# Uso: ./tests/scripts/run_unit_test.sh
# Não precisa de nenhum servidor ou lobby activo.
# Testa GameState, LocalPlayer e CombatEngine em modo offline puro.

source "$(dirname "$0")/_lib.sh"

hr
step "Testes unitários — GameState / LocalPlayer / CombatEngine"
info "Modo offline: sem gRPC nem Kademlia."
hr

cd "$GAME_ROOT"
uv run pytest tests/test_game_state.py tests/test_combat_engine.py -v

ok "Testes unitários concluídos."