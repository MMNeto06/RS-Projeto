#!/usr/bin/env bash
# _lib.sh — funções partilhadas pelos scripts de teste
# Não correr directamente; fazer source deste ficheiro.
#
# Uso nos scripts filhos:
#   source "$(dirname "$0")/_lib.sh"

set -euo pipefail

# ── Cores ──────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; RESET='\033[0m'

info()  { echo -e "${CYAN}[INFO]${RESET}  $*" 2>/dev/null || true; }
ok()    { echo -e "${GREEN}[ OK ]${RESET}  $*" 2>/dev/null || true; }
warn()  { echo -e "${YELLOW}[WARN]${RESET}  $*" 2>/dev/null || true; }
err()   { echo -e "${RED}[ERRO]${RESET}  $*" >&2 2>/dev/null || true; }
step()  { echo -e "\n${BOLD}▶ $*${RESET}" 2>/dev/null || true; }
hr()    { echo -e "${BOLD}$(printf '─%.0s' {1..60})${RESET}" 2>/dev/null || true; }

# ── Directório de trabalho ─────────────────────────────────────────
# Aponta para a raiz do projecto independentemente de onde está o script.
GAME_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

# ── Temporários (limpos no EXIT) ───────────────────────────────────
TMPDIR_TEST="$(mktemp -d /tmp/dnd_test_XXXXXX)"
GAME_PIDS=()
OPEN_FDS=()

cleanup_all() {
    local exit_code=$?
    step "A limpar processos e ficheiros temporários…"

    for pid in "${GAME_PIDS[@]:-}"; do
        if kill -0 "$pid" 2>/dev/null; then
            kill "$pid" 2>/dev/null || true
            local i=0
            while kill -0 "$pid" 2>/dev/null && [ $i -lt 20 ]; do
                sleep 0.1; i=$((i + 1))
            done
            kill -9 "$pid" 2>/dev/null || true
        fi
    done

    for fd in "${OPEN_FDS[@]:-}"; do
        # Fechar o FD com segurança; pode já estar fechado se o processo saiu
        # `OPEN_FDS` armazena números de descritor (ex: 10). Tentar usar
        # `exec {fd}>&-` aqui falha porque `{fd}` é tratado como nome de
        # variável de descritor (não o número). Em vez disso, fechamos o
        # FD pelo seu número com `eval` — seguro porque validamos que é
        # composto somente por dígitos.
        if [[ -n "$fd" && "$fd" =~ ^[0-9]+$ ]]; then
            eval "exec ${fd}>&- 2>/dev/null" || true
        fi
    done

    if [ -n "${KEEP_TEST_LOGS:-}" ]; then
        info "KEEP_TEST_LOGS set — preservando logs em: $TMPDIR_TEST"
    else
        rm -rf "$TMPDIR_TEST"
    fi

    if [ $exit_code -eq 0 ]; then
        ok "Limpeza concluída."
    else
        warn "Script terminou com código $exit_code. Limpeza concluída."
    fi
    exit "$exit_code"
}

trap cleanup_all EXIT INT TERM

# ── Esperar que uma porta TCP esteja à escuta ──────────────────────
# Uso: wait_for_port <host> <port> [timeout_s=20]
wait_for_port() {
    local host="$1" port="$2" timeout="${3:-20}"
    local elapsed=0
    info "À espera que $host:$port fique disponível…"
    while ! (echo > /dev/tcp/"$host"/"$port") 2>/dev/null; do
        sleep 0.5
        elapsed=$(echo "$elapsed + 0.5" | bc)
        if (( $(echo "$elapsed >= $timeout" | bc -l) )); then
            err "Timeout (${timeout}s) a aguardar $host:$port"
            return 1
        fi
    done
    ok "$host:$port disponível."
}

# ── Esperar que um padrão apareça num ficheiro de log ─────────────
# Usa sed para remover ANSI antes de fazer grep.
# Uso: wait_for_log <ficheiro> <padrão_grep> [timeout_s=30]
wait_for_log() {
    local logfile="$1" pattern="$2" timeout="${3:-30}"
    local elapsed=0
    info "À espera de '${pattern}' em $(basename "$logfile")…"
    while ! sed 's/\x1b\[[0-9;]*[mKHJ]//g' "$logfile" 2>/dev/null \
              | grep -qiE "$pattern"; do
        sleep 0.5
        elapsed=$(echo "$elapsed + 0.5" | bc)
        if (( $(echo "$elapsed >= $timeout" | bc -l) )); then
            err "Timeout (${timeout}s) à espera de '${pattern}'"
            err "Últimas 15 linhas do log:"
            tail -15 "$logfile" | sed 's/\x1b\[[0-9;]*[mKHJ]//g' >&2 || true
            return 1
        fi
    done
    ok "Padrão '${pattern}' encontrado."
}

# ── Lançar um processo de jogo via FIFO ───────────────────────────
# Uso: start_game <nome> [args_main_game.py...]
#
# Exporta após a chamada:
#   PIPE_<NOME>  — caminho FIFO para enviar comandos
#   LOG_<NOME>   — caminho do log de stdout/stderr
#   PID_<NOME>   — PID do processo
#   FD_<NOME>    — FD do write-end do FIFO (mantido aberto)
start_game() {
    local name="$1"; shift
    local uname="${name^^}"
    local pipe_path="$TMPDIR_TEST/pipe_${name}"
    local log_path="$TMPDIR_TEST/log_${name}.txt"

    mkfifo "$pipe_path"
    touch "$log_path"

    # Abrir o FIFO em modo leitura+escrita (<>) para evitar deadlock.
    #
    # PORQUÊ <>: abrir um FIFO em modo escrita puro (>) bloqueia até
    # haver um leitor — mas o processo do jogo ainda não foi lançado,
    # causando um deadlock. O modo <> (O_RDWR) é não-bloqueante e
    # mantém o write-end aberto para que ecos subsequentes não causem EOF.
    #
    # A sintaxe {fd}<> aloca automaticamente um FD livre ≥ 10 (bash 4.1+).
    local fd
    exec {fd}<>"$pipe_path"
    OPEN_FDS+=("$fd")

    (cd "$GAME_ROOT" && uv run python main_game.py "$@") \
        < "$pipe_path" \
        > "$log_path" 2>&1 &

    local pid=$!
    GAME_PIDS+=("$pid")

    eval "PIPE_${uname}='${pipe_path}'"
    eval "LOG_${uname}='${log_path}'"
    eval "PID_${uname}='${pid}'"
    eval "FD_${uname}='${fd}'"

    info "Processo '${name}' iniciado (PID=${pid}) — log: $(basename "$log_path")"
}

# ── Enviar comando ao stdin de um processo ────────────────────────
# Uso: send_cmd <nome> "comando" [delay_após_s=1]
send_cmd() {
    local uname="${1^^}"
    local cmd="$2"
    local delay="${3:-1}"
    local fd_var="FD_${uname}"
    local fd="${!fd_var}"

    info "→ [${uname}] '${cmd}'"
    echo "$cmd" >&"$fd"; sleep "$delay"
}

# ── Extrair código do lobby do log de um jogador ──────────────────
# Procura a linha "Lobby activo : CÓDIGO" gerada por _render_menu.
# Uso: extract_lobby_code <nome_jogador>
# Devolve o código (echo) ou falha com exit 1.
extract_lobby_code() {
    local name="${1^^}"
    local log_var="LOG_${name}"
    local logfile="${!log_var}"

    # Remove ANSI escape codes antes de processar
    local clean
    clean=$(sed 's/\x1b\[[0-9;]*[mKHJ]//g' "$logfile" 2>/dev/null)

    local code=""

    # Padrão 1: "Lobby activo : CODIGO" (linha gerada por _render_menu)
    code=$(echo "$clean" \
        | grep -iE 'Lobby activo\s*:' \
        | grep -oE '[A-Za-z0-9]{3,16}' \
        | grep -viE '^(lobby|activo|active)$' \
        | tail -1)

    # Padrão 2: "código : CODIGO" ou "code : CODIGO"
    if [ -z "$code" ]; then
        code=$(echo "$clean" \
            | grep -iE '(código|code)\s*:\s*[A-Za-z0-9]{3,16}' \
            | grep -oE '[A-Za-z0-9]{3,16}' \
            | grep -viE '^(código|code)$' \
            | tail -1)
    fi

    # Padrão 3: qualquer token alfanumérico de 4-12 chars em linhas com "Lobby activo"
    # (mais restrito que antes para não apanhar tokens do texto de ajuda do menu)
    if [ -z "$code" ]; then
        code=$(echo "$clean" \
            | grep -iE 'Lobby activo' \
            | grep -oE '[A-Za-z0-9]{4,12}' \
            | grep -viE '^(lobby|activo|active)$' \
            | tail -1)
    fi

    if [ -z "$code" ]; then
        err "Não foi possível extrair o código do lobby do log de ${name}."
        err "Verifica: $logfile"
        err "Linhas com 'Lobby' encontradas no log (sem ANSI):"
        echo "$clean" | grep -i 'lobby' | head -10 >&2 || true
        return 1
    fi

    echo "$code"
}

# ── Setup reutilizável: dois jogadores num lobby ───────────────────
# Inicia dois jogadores, cria lobby, ambos ficam prontos, jogo começa.
#
# Uso: setup_two_player_game \
#        <host_name> <host_grpc> <host_kad> \
#        <peer_name> <peer_grpc> <peer_kad> \
#        [lobby_name=testlobby]
#
# Após retorno: LOBBY_CODE contém o código do lobby criado.
setup_two_player_game() {
    local host_name="$1" host_grpc="$2" host_kad="$3"
    local peer_name="$4" peer_grpc="$5" peer_kad="$6"
    local lobby_name="${7:-testlobby}"
    local host_up="${host_name^^}"
    local peer_up="${peer_name^^}"

    # ── 1. Iniciar host (bootstrap) ───────────────────────────────
    step "A iniciar ${host_name} (host, bootstrap)…"
    start_game "$host_name" \
        --username "$host_name" \
        --host 127.0.0.1 \
        --grpc-port "$host_grpc" \
        --kad-port  "$host_kad" \
        --first

    wait_for_port 127.0.0.1 "$host_grpc" 25

    # Aguardar que o DHT e menu estejam prontos
    local log_host
    eval "log_host=\$LOG_${host_up}"
    wait_for_log "$log_host" "verificar disponibilidade|username|DHT" 20

    # ── 2. Configurar host ────────────────────────────────────────
    step "A configurar ${host_name} no menu…"
    send_cmd "$host_name" "class fighter" 2
    send_cmd "$host_name" "lobby create ${lobby_name} 4" 4

    # Aguardar que o lobby apareça renderizado no menu.
    # IMPORTANTE: não usar "código|code" aqui — esse texto aparece no menu de ajuda
    # inicial ("lobby join <código>") e causaria um match prematuro antes de o lobby
    # estar criado. "Lobby activo :" só aparece quando há um lobby activo.
    wait_for_log "$log_host" "Lobby activo\s*:" 15
    LOBBY_CODE=$(extract_lobby_code "$host_name")
    ok "Código do lobby: ${LOBBY_CODE}"

    send_cmd "$host_name" "ready" 1

    # ── 3. Iniciar peer ───────────────────────────────────────────
    step "A iniciar ${peer_name}…"
    start_game "$peer_name" \
        --username "$peer_name" \
        --host 127.0.0.1 \
        --grpc-port "$peer_grpc" \
        --kad-port  "$peer_kad" \
        --bootstrap-host     127.0.0.1 \
        --bootstrap-kad-port "$host_kad"

    wait_for_port 127.0.0.1 "$peer_grpc" 25

    local log_peer
    eval "log_peer=\$LOG_${peer_up}"
    wait_for_log "$log_peer" "verificar disponibilidade|username|DHT" 20

    # ── 4. Peer entra no lobby e fica pronto ──────────────────────
    step "A configurar ${peer_name} no menu…"
    send_cmd "$peer_name" "class fighter" 2
    send_cmd "$peer_name" "lobby join ${LOBBY_CODE}" 3
    send_cmd "$peer_name" "ready" 1

    # ── 5. Host inicia o jogo ─────────────────────────────────────
    step "${host_name} a iniciar o jogo…"
    # Dar tempo ao host para receber o "ready" do peer
    sleep 2
    send_cmd "$host_name" "lobby start" 3

    wait_for_log "$log_host" "jogo.*iniciado|A entrar|ACTIVE|game.*start" 20
    ok "Jogo iniciado com sucesso!"
}

# ── Mostrar log de um jogador (útil para debug) ───────────────────
show_log() {
    local uname="${1^^}"
    local log_var="LOG_${uname}"
    local logfile="${!log_var}"
    echo -e "\n${YELLOW}── Log de ${uname} ──${RESET}"
    sed 's/\x1b\[[0-9;]*[mKHJ]//g' "$logfile"
    echo -e "${YELLOW}── fim ──${RESET}\n"
}

# ── Port allocation helpers (ephemeral ports) ─────────────────────
# Use the project's virtualenv python if available, otherwise system python3.
get_python() {
    if [ -x "${GAME_ROOT}/.venv/bin/python3" ]; then
        printf '%s' "${GAME_ROOT}/.venv/bin/python3"
    else
        printf '%s' "python3"
    fi
}

# Returns an available TCP port number
get_free_tcp_port() {
    local py
    py=$(get_python)
    "$py" - <<'PY'
import socket
s=socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.bind(('', 0))
print(s.getsockname()[1])
s.close()
PY
}

# Returns an available UDP port number
get_free_udp_port() {
    local py
    py=$(get_python)
    "$py" - <<'PY'
import socket
s=socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.bind(('', 0))
print(s.getsockname()[1])
s.close()
PY
}

# Allocate a pair of grpc/udp ports for two players and export variables
allocate_pair_ports() {
    # Usage: allocate_pair_ports ALICE_PREFIX BOB_PREFIX
    local a="$1" b="$2"
    eval "${a}_GRPC=$(get_free_tcp_port)"
    eval "${a}_KAD=$(get_free_udp_port)"
    eval "${b}_GRPC=$(get_free_tcp_port)"
    eval "${b}_KAD=$(get_free_udp_port)"
}