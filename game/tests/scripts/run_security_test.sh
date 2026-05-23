#!/usr/bin/env bash
# run_security_test.sh — testes de segurança automáticos
#
# Inicia dois jogadores (alice + hacker) num lobby, arranca o jogo
# e corre o security_test.py contra o servidor de alice.
#
# Uso:
#   ./tests/scripts/run_security_test.sh
#
# O que valida:
#   [AT] Dano absurdo      → attack_roll=999, base_damage=999; servidor deve limitar
#   [AT] Fora de alcance   → effective_range=1; servidor deve rejeitar (hit=False)
#   [AT] Target fantasma   → target inexistente; servidor não deve crashar
#   [AT] Coords negativas  → attacker_x/y negativos; servidor não deve crashar
#   [MV] Coords negativas  → x=-1, y=-1; servidor deve rejeitar (applied=False)
#   [MV] Coords excessivas → x=999, y=999; servidor deve rejeitar (applied=False)
#   [MV] Mover outro user  → hacker tenta mover alice; servidor deve ignorar
#
# Resultados suspeitos:
#   damage_dealt=999 no teste de dano absurdo  → sem validação de dano
#   hit=True no fora de alcance                → sem validação de range
#   applied=True nos testes de move inválido   → sem validação de coords

source "$(dirname "$0")/_lib.sh"

# ── Portas fixas ──────────────────────────────────────────────────
ALICE_GRPC=50051; ALICE_KAD=50052
HACKER_GRPC=50053; HACKER_KAD=50054

# ── Setup ─────────────────────────────────────────────────────────
hr
step "Testes de Segurança gRPC"
hr

setup_two_player_game \
    "alice"  "$ALICE_GRPC"  "$ALICE_KAD"  \
    "hacker" "$HACKER_GRPC" "$HACKER_KAD" \
    "sectest"

# Pausa para o estado do jogo estabilizar
sleep 2

# ── Teste ─────────────────────────────────────────────────────────
hr
step "A correr security_test.py…"
hr

cd "$GAME_ROOT"
uv run python tests/security_test.py "127.0.0.1:${ALICE_GRPC}"

ok "Testes de segurança concluídos."
info "Revê os resultados acima: procura hit=True ou applied=True onde não deveria."