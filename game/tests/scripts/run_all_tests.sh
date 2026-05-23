#!/usr/bin/env bash
# run_all_tests.sh — corre todos os testes em sequência
#
# Uso:
#   ./tests/scripts/run_all_tests.sh [--skip-load] [--skip-security]
#                                    [--skip-cross]  [--cross-scenario a|b]
#                                    [--load-clients N] [--load-rounds N]
#
# Ordem de execução:
#   1. Testes unitários    (offline, rápido)
#   2. Isolamento de lobby (1 ou 2 lobbies)
#   3. Testes de segurança (inputs maliciosos)
#   4. Teste de carga      (throughput gRPC)
#
# Cada teste de integração levanta os seus próprios processos de jogo
# e limpa tudo quando termina. Os processos de um teste não interferem
# com o seguinte porque cada um usa portas diferentes e espera que as
# portas da ronda anterior fiquem livres.
#
# Flags de skip úteis para desenvolvimento:
#   --skip-load      Omite o teste de carga (mais lento)
#   --skip-security  Omite o teste de segurança
#   --skip-cross     Omite o teste de isolamento

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ── Cores (directo, sem source de _lib.sh para evitar o trap global) ──
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; RESET='\033[0m'

info()  { echo -e "${CYAN}[INFO]${RESET}  $*"; }
ok()    { echo -e "${GREEN}[ OK ]${RESET}  $*"; }
warn()  { echo -e "${YELLOW}[WARN]${RESET}  $*"; }
err()   { echo -e "${RED}[ERRO]${RESET}  $*" >&2; }
step()  { echo -e "\n${BOLD}▶ $*${RESET}"; }
hr()    { echo -e "${BOLD}$(printf '─%.0s' {1..60})${RESET}"; }

# ── Parse de argumentos ───────────────────────────────────────────
SKIP_UNIT=false
SKIP_CROSS=false
SKIP_SECURITY=false
SKIP_LOAD=false
CROSS_SCENARIO="a"
LOAD_CLIENTS=20
LOAD_ROUNDS=10

while [[ $# -gt 0 ]]; do
    case "$1" in
        --skip-unit)      SKIP_UNIT=true;  shift ;;
        --skip-cross)     SKIP_CROSS=true; shift ;;
        --skip-security)  SKIP_SECURITY=true; shift ;;
        --skip-load)      SKIP_LOAD=true;  shift ;;
        --cross-scenario) CROSS_SCENARIO="${2,,}"; shift 2 ;;
        --load-clients)   LOAD_CLIENTS="$2"; shift 2 ;;
        --load-rounds)    LOAD_ROUNDS="$2";  shift 2 ;;
        -h|--help)
            grep '^#' "$0" | sed 's/^# \{0,1\}//'
            exit 0 ;;
        *) echo "Argumento desconhecido: $1"; exit 1 ;;
    esac
done

# ── Registo de resultados ─────────────────────────────────────────
declare -A RESULTS   # nome → "PASS" | "FAIL" | "SKIP"
declare -A DURATIONS # nome → segundos

run_test() {
    local name="$1"; shift
    local label="$1"; shift
    # restantes args são o comando a executar

    if [ "${RESULTS[$name]:-}" = "SKIP" ]; then
        warn "[SKIP] ${label}"
        return 0
    fi

    hr
    step "${label}"
    hr

    local t0
    t0=$(date +%s)

    # Run the test and capture its exit code so we can report why it failed.
    "$@"
    local status=$?
    if [ $status -eq 0 ]; then
        RESULTS[$name]="PASS"
    else
        RESULTS[$name]="FAIL"
        err "Test '${label}' exited with status: ${status}"
    fi

    DURATIONS[$name]=$(( $(date +%s) - t0 ))

    # Aguardar que as portas TCP sejam libertadas antes do próximo teste
    info "A aguardar libertação de portas (3s)…"
    sleep 3
}

mark_skip() {
    local name="$1"
    RESULTS[$name]="SKIP"
}

# ── Marcar testes a saltar ────────────────────────────────────────
$SKIP_UNIT     && mark_skip "unit"
$SKIP_CROSS    && mark_skip "cross"
$SKIP_SECURITY && mark_skip "security"
$SKIP_LOAD     && mark_skip "load"

# ─────────────────────────────────────────────────────────────────
# EXECUÇÃO DOS TESTES
# ─────────────────────────────────────────────────────────────────

hr
echo -e "${BOLD}  DnD Deathmatch — Suite de Testes Completa${RESET}"
hr

# 1. Testes unitários
run_test "unit" "Testes Unitários (offline)" \
    bash "$SCRIPT_DIR/run_unit_test.sh"

# 2. Isolamento de lobby
run_test "cross" "Isolamento de Lobby (cenário ${CROSS_SCENARIO^^})" \
    bash "$SCRIPT_DIR/run_cross_lobby_test.sh" --scenario "$CROSS_SCENARIO"

# 3. Segurança
run_test "security" "Testes de Segurança" \
    bash "$SCRIPT_DIR/run_security_test.sh"

# 4. Carga
run_test "load" "Teste de Carga (${LOAD_CLIENTS} clientes × ${LOAD_ROUNDS} rounds)" \
    bash "$SCRIPT_DIR/run_load_test.sh" \
        --clients "$LOAD_CLIENTS" \
        --rounds  "$LOAD_ROUNDS"

# ─────────────────────────────────────────────────────────────────
# SUMÁRIO FINAL
# ─────────────────────────────────────────────────────────────────

hr
echo -e "${BOLD}  SUMÁRIO${RESET}"
hr

PASS=0; FAIL=0; SKIP=0
TOTAL_SECS=0

print_row() {
    local name="$1" label="$2"
    local status="${RESULTS[$name]:-SKIP}"
    local secs="${DURATIONS[$name]:-0}"

    case "$status" in
        PASS) echo -e "  ${GREEN}✓ PASS${RESET}  ${label}  ${CYAN}(${secs}s)${RESET}"; ((PASS++)) ;;
        FAIL) echo -e "  ${RED}✗ FAIL${RESET}  ${label}  ${CYAN}(${secs}s)${RESET}"; ((FAIL++)) ;;
        SKIP) echo -e "  ${YELLOW}– SKIP${RESET}  ${label}" ; ((SKIP++)) ;;
    esac
    TOTAL_SECS=$(( TOTAL_SECS + secs ))
}

print_row "unit"     "Testes Unitários"
print_row "cross"    "Isolamento de Lobby  (cenário ${CROSS_SCENARIO^^})"
print_row "security" "Testes de Segurança"
print_row "load"     "Teste de Carga"

hr
echo -e "  ${BOLD}Total: ${PASS} PASS  ${FAIL} FAIL  ${SKIP} SKIP${RESET}  (${TOTAL_SECS}s)"
hr

if [ "$FAIL" -gt 0 ]; then
    err "${FAIL} teste(s) falharam. Revê os logs acima."
    exit 1
else
    ok "Todos os testes concluídos com sucesso."
    exit 0
fi