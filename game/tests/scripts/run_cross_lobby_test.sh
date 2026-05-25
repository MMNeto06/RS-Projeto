#!/usr/bin/env bash
# run_cross_lobby_test.sh — testa isolamento entre lobbies

# Uso: ./tests/scripts/run_cross_lobby_test.sh [--scenario a|b]

# Cenário A (default)
#   Apenas 1 lobby real. O teste envia um ataque de "charlie"
#   (nome desconhecido pelo host de alice). Deve ser rejeitado.
#
# Cenário B 
#   2 lobbies reais em simultâneo (alice+bob e charlie+dave).
#   O teste envia um ataque do host do lobby B para o host do lobby A.
#   Deve ser rejeitado porque charlie não está no lobby de alice.

# Resultados esperados:
#   PASS: hit=False && damage_dealt=0
#   FAIL: hit=True || damage_dealt>0

source "$(dirname "$0")/_lib.sh"

# Argumentos
SCENARIO="a"
while [[ $# -gt 0 ]]; do
    case "$1" in
        --scenario) SCENARIO="${2,,}"; shift 2 ;;
        *) err "Argumento desconhecido: $1"; exit 1 ;;
    esac
done

# Portas
allocate_pair_ports "ALICE" "BOB"
allocate_pair_ports "CHARLIE" "DAVE"

# CENÁRIO A — 1 lobby, atacante desconhecido ("charlie")
run_scenario_a() {
    hr
    step "Cenário A — 1 lobby, atacante desconhecido"
    info "alice e bob jogam juntos. O teste ataca como 'charlie' (fora do lobby)."
    hr

    setup_two_player_game \
        "alice" "$ALICE_GRPC" "$ALICE_KAD" \
        "bob"   "$BOB_GRPC"   "$BOB_KAD"   \
        "lobbyA"

    sleep 2

    hr
    step "A correr test_cross_lobby.py (Cenário A)…"
    hr

    cd "$GAME_ROOT"
    uv run python tests/test_cross_lobby.py \
        "127.0.0.1:${ALICE_GRPC}" \
        --target  "alice" \
        --attacker "charlie"
}

# CENÁRIO B — 2 lobbies reais em simultâneo
run_scenario_b() {
    hr
    step "Cenário B — 2 lobbies reais em simultâneo"
    info "Lobby A: alice + bob.  Lobby B: charlie + dave."
    info "O teste tenta atacar alice a partir do charlie (lobby diferente)."
    hr

    # Lobby A: alice + bob
    step "A iniciar Lobby A (alice + bob)…"
    start_game "alice" \
        --username alice \
        --host 127.0.0.1 \
        --grpc-port "$ALICE_GRPC" \
        --kad-port  "$ALICE_KAD" \
        --first

    wait_for_port 127.0.0.1 "$ALICE_GRPC" 25
    wait_for_log "$LOG_ALICE" "verificar disponibilidade|DHT" 20

    send_cmd alice "class fighter" 2
    send_cmd alice "lobby create lobbyA 4" 4
    wait_for_log "$LOG_ALICE" "Lobby activo\s*:" 15
    LOBBY_A=$(extract_lobby_code alice)
    ok "Lobby A: ${LOBBY_A}"
    send_cmd alice "ready" 1

    start_game "bob" \
        --username bob \
        --host 127.0.0.1 \
        --grpc-port "$BOB_GRPC" \
        --kad-port  "$BOB_KAD" \
        --bootstrap-host     127.0.0.1 \
        --bootstrap-kad-port "$ALICE_KAD"

    wait_for_port 127.0.0.1 "$BOB_GRPC" 25
    wait_for_log "$LOG_BOB" "verificar disponibilidade|DHT" 20
    send_cmd bob "class fighter" 2
    send_cmd bob "lobby join ${LOBBY_A}" 3
    send_cmd bob "ready" 1

    # Lobby B: charlie + dave
    step "A iniciar Lobby B (charlie + dave)…"
    start_game "charlie" \
        --username charlie \
        --host 127.0.0.1 \
        --grpc-port "$CHARLIE_GRPC" \
        --kad-port  "$CHARLIE_KAD" \
        --bootstrap-host     127.0.0.1 \
        --bootstrap-kad-port "$ALICE_KAD"

    wait_for_port 127.0.0.1 "$CHARLIE_GRPC" 25
    wait_for_log "$LOG_CHARLIE" "verificar disponibilidade|DHT" 20
    send_cmd charlie "class fighter" 2
    send_cmd charlie "lobby create lobbyB 4" 4
    wait_for_log "$LOG_CHARLIE" "Lobby activo\s*:" 15
    LOBBY_B=$(extract_lobby_code charlie)
    ok "Lobby B: ${LOBBY_B}"
    send_cmd charlie "ready" 1

    start_game "dave" \
        --username dave \
        --host 127.0.0.1 \
        --grpc-port "$DAVE_GRPC" \
        --kad-port  "$DAVE_KAD" \
        --bootstrap-host     127.0.0.1 \
        --bootstrap-kad-port "$ALICE_KAD"

    wait_for_port 127.0.0.1 "$DAVE_GRPC" 25
    wait_for_log "$LOG_DAVE" "verificar disponibilidade|DHT" 20
    send_cmd dave "class fighter" 2
    send_cmd dave "lobby join ${LOBBY_B}" 3
    send_cmd dave "ready" 1

    # Iniciar ambos os jogos
    # IMPORTANTE: disparar os dois 'lobby start' antes de esperar por qualquer um deles.
    # Se esperarmos pela confirmação de alice antes de iniciar charlie, o broadcast
    # MATCH_STARTED de alice chega a charlie via DHT e coloca-o no game loop antes
    # do script enviar 'lobby start' ao charlie — o padrão de confirmação nunca aparece.
    step "A iniciar os dois jogos em simultâneo…"
    sleep 2
    send_cmd alice   "lobby start" 1
    send_cmd charlie "lobby start" 1

    wait_for_log "$LOG_ALICE"   "iniciou.*jogo|jogo.*iniciado|A entrar|ACTIVE|game.*start" 30
    ok "Lobby A iniciado."
    wait_for_log "$LOG_CHARLIE" "iniciou.*jogo|jogo.*iniciado|A entrar|ACTIVE|game.*start" 30
    ok "Lobby B iniciado."

    sleep 2

    # Teste
    hr
    step "A correr test_cross_lobby.py (Cenário B)…"
    hr

    cd "$GAME_ROOT"
    uv run python tests/test_cross_lobby.py \
        "127.0.0.1:${ALICE_GRPC}" \
        --target       "alice" \
        --attacker     "charlie" \
        --attacker-addr "127.0.0.1:${CHARLIE_GRPC}"
}

# Executar o cenário escolhido
case "$SCENARIO" in
    a) run_scenario_a ;;
    b) run_scenario_b ;;
    *) err "Cenário inválido: '${SCENARIO}'. Usa 'a' ou 'b'."; exit 1 ;;
esac

ok "Teste de isolamento de lobby concluído."
exit 0