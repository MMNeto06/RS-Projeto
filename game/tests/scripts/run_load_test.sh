#!/usr/bin/env bash
# run_load_test.sh — teste de carga automático
#
# Inicia dois jogadores (alice + loadbot) num lobby, arranca o jogo
# e corre o load_test.py contra o servidor de alice.
#
# Uso:
#   ./tests/scripts/run_load_test.sh [--clients N] [--rounds N]
#
# Opções:
#   --clients N   Número de clientes simultâneos (default: 20)
#   --rounds  N   Requests por cliente (default: 10)
#
# O que mede:
#   - Throughput (RPS) do servidor gRPC sob carga paralela
#   - Taxa de erro (timeouts / excepções gRPC)
#   - Se RPS cai com mais clientes → servidor não escala bem
#   - Se erros > 0 com poucos clientes → bottleneck no handler

source "$(dirname "$0")/_lib.sh"

# ── Argumentos ────────────────────────────────────────────────────
CLIENTS=20
ROUNDS=10
while [[ $# -gt 0 ]]; do
    case "$1" in
        --clients) CLIENTS="$2"; shift 2 ;;
        --rounds)  ROUNDS="$2";  shift 2 ;;
        *) err "Argumento desconhecido: $1"; exit 1 ;;
    esac
done

# ── Portas (ephemeral) ───────────────────────────────────────────
# Allocate ephemeral ports to avoid collisions between runs
allocate_pair_ports "ALICE" "BOT"

# Now ALICE_GRPC, ALICE_KAD, BOT_GRPC, BOT_KAD are set

# ── Setup ─────────────────────────────────────────────────────────
hr
step "Teste de Carga gRPC"
info "Clientes: ${CLIENTS}  Rounds: ${ROUNDS}  Total: $((CLIENTS * ROUNDS)) requests"
hr

setup_two_player_game \
    "alice"   "$ALICE_GRPC" "$ALICE_KAD" \
    "loadbot" "$BOT_GRPC"   "$BOT_KAD"  \
    "loadtest"

# Pequena pausa para o estado do jogo estabilizar
sleep 2

# ── Teste ─────────────────────────────────────────────────────────
hr
step "A correr load_test.py…"
hr

cd "$GAME_ROOT"
uv run python tests/load_test.py \
    "127.0.0.1:${ALICE_GRPC}" \
    --clients "$CLIENTS" \
    --rounds  "$ROUNDS"

ok "Teste de carga concluído."
exit 0