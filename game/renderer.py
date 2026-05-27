"""
game/renderer.py
Renderização do estado do jogo no terminal com ANSI escape codes.

render_frame(state) imprime o ecrã completo:

  -------- DEATHMATCH - 08:47 --------
  mapa 20×10 com jogadores
  HP  ████████████░░░░  45/60   Fighter
  
  [Eventos]
  [10:23] Alice atacou com Longsword → 8 dano
  ...
  
  [Jogadores]   Alice 3K/1D   Bob 1K/2D
  
  [Ações]  [At]acar  Mover(WASD)  [1]Second Wind  [Q]uit

  > █                    <- cursor fica sempre aqui

Estratégia de rendering sem flickering e com input sempre visível
------------------------------------------------------------------
Em vez de guardar/restaurar o cursor (que falha quando o frame
cresceu desde o último render), fazemos:

  1. Cursor para topo do ecrã  (\033[H)
  2. Cada linha do frame seguida de  \033[K  (limpa até ao fim da linha)
  3. Linha do prompt  "> <buffer>"  seguida de  \033[K
  4. \033[J  — apaga tudo abaixo (remove linhas antigas)
  5. Cursor fica exactamente no fim do texto digitado pelo utilizador.

O renderer recebe `input_line` (str) que vem de InputReader.prompt_line.
"""

import sys
from game_state import GameState, MatchPhase, MAP_W, MAP_H
from dnd_classes import CLASSES

# -- CORES ANSI ----------------------------------------------------

RESET   = "\033[0m"
BOLD    = "\033[1m"
RED     = "\033[31m"
GREEN   = "\033[32m"
YELLOW  = "\033[33m"
CYAN    = "\033[36m"
WHITE   = "\033[37m"
DIM     = "\033[2m"
MAGENTA = "\033[35m"
BLUE    = "\033[34m"

# Paleta de cores para diferenciar jogadores remotos.
# Excluímos RED (mortos) e CYAN (jogador local).
_PLAYER_PALETTE = [GREEN, YELLOW, MAGENTA, BLUE, WHITE,
                   "\033[92m", "\033[93m", "\033[95m"]


def _player_color(username: str) -> str:
    """Cor ANSI determinística baseada no username."""
    idx = sum(ord(c) for c in username) % len(_PLAYER_PALETTE)
    return _PLAYER_PALETTE[idx]


# -- CONSTANTES DE CURSOR ------------------------------------------

_HOME        = "\033[H"   # cursor para linha 1, coluna 1
_CLEAR_EOL   = "\033[K"   # apaga do cursor até ao fim da linha
_CLEAR_BELOW = "\033[J"   # apaga do cursor até ao fim do ecrã


# -- HP BAR --------------------------------------------------------

def render_hp_bar(current: int, maximum: int, width: int = 20) -> str:
    if maximum == 0:
        return "░" * width
    ratio  = current / maximum
    filled = int(width * ratio)
    color  = GREEN if ratio > 0.5 else (YELLOW if ratio > 0.25 else RED)
    bar    = color + "█" * filled + DIM + "░" * (width - filled) + RESET
    return f"{bar}  {BOLD}{current}/{maximum}{RESET}"


# -- MAPA ----------------------------------------------------------

def render_map(state: GameState) -> str:
    """
    Mapa ASCII 20×10.  Cada célula = 2 chars de largura.

    Símbolos:
      ☺  → jogador local vivo      (cyan/bold)
      ✝  → jogador local morto     (vermelho)
      A-Z → inicial do username de outros jogadores
      ·  → célula vazia
    """
    grid = [["· " for _ in range(MAP_W)] for _ in range(MAP_H)]

    for p in state.others.values():
        if 0 <= p.x < MAP_W and 0 <= p.y < MAP_H:
            initial = p.username[0].upper()
            color = _player_color(p.username) if p.alive else RED
            grid[p.y][p.x] = f"{color}{initial} {RESET}"

    lp = state.local
    if 0 <= lp.x < MAP_W and 0 <= lp.y < MAP_H:
        symbol = f"{BOLD}{CYAN}☺ {RESET}" if lp.alive else f"{RED}✝ {RESET}"
        grid[lp.y][lp.x] = symbol

    border_h = "—" * (MAP_W * 2 + 2)
    lines = [f"┌{border_h}┐"]
    for row in grid:
        lines.append("│ " + "".join(row) + " │")
    lines.append(f"└{border_h}┘")
    return "\n".join(lines)


# -- TIMER ---------------------------------------------------------

def render_timer(state: GameState) -> str:
    secs = state.time_remaining()
    m, s = divmod(secs, 60)
    color = RED if secs < 60 else (YELLOW if secs < 120 else GREEN)
    return f"{color}⏱  {m:02d}:{s:02d}{RESET}"


# -- SCOREBOARD ----------------------------------------------------

def render_scoreboard(state: GameState) -> str:
    rows = state.scoreboard()
    parts = []
    for username, kills, deaths in rows:
        is_local = (username == state.local.username)
        color = BOLD + CYAN if is_local else _player_color(username)
        parts.append(
            f"{color}{username}{RESET} "
            f"{GREEN}{kills}K{RESET}/{RED}{deaths}D{RESET}"
        )
    return "  ".join(parts)


# -- AÇÕES ---------------------------------------------------------

def render_actions(state: GameState) -> str:
    local = state.local
    lines = [f"{BOLD}[Ações]{RESET}"]
    lines.append(f"  {CYAN}[AT]{RESET} Atacar   {CYAN}[WASD]{RESET} Mover   {CYAN}[SC]{RESET} Scoreboard")
    for i, ability in enumerate(local.dnd_class.abilities, start=1):
        ready = local.is_ability_ready(ability.name)
        color = GREEN if ready else RED
        cd    = "" if ready else f"{DIM}  (cooldown){RESET}"
        lines.append(f"  {color}[{i}]{RESET} {ability.name}{cd}")
    lines.append(f"  {RED}[Q]{RESET} Sair")
    return "\n".join(lines)


# -- FRAME COMPLETO ------------------------------------------------

def render_frame(
    state: GameState,
    clear: bool = True,
    input_line: str = "> ",
) -> str:
    """
    Gera e (opcionalmente) escreve o frame completo no terminal.

    Parâmetros
    ----------
    state      : estado actual do jogo
    clear      : se True, escreve no terminal com a estratégia
                 home + clear-per-line + clear-below
    input_line : linha de prompt a mostrar no fundo (vem de InputReader.prompt_line)
                 ex: "> a Bob"   <- o utilizador está a escrever "a Bob"

    O cursor termina no fim de `input_line`, por isso o utilizador
    continua a ver e editar o seu input normalmente.
    """
    lp = state.local

    parts: list[str] = []

    # -- Cabeçalho ----
    phase_label = {
        MatchPhase.LOBBY:    "LOBBY",
        MatchPhase.STARTING: "INICIANDO…",
        MatchPhase.ACTIVE:   "DEATHMATCH",
        MatchPhase.ENDED:    "FIM DE JOGO",
    }[state.phase]

    timer  = render_timer(state) if state.phase == MatchPhase.ACTIVE else ""
    header = f"{BOLD}{YELLOW}{'-'*10} {phase_label} {timer} {'-'*10}{RESET}"
    parts.append(header)

    # -- Mapa ----
    parts.append(render_map(state))

    # -- HP bar ----
    status_str = ""
    if lp.status_effects:
        status_str = f"  {YELLOW}" + ", ".join(lp.status_effects) + RESET
    hp_line = (
        f"HP  {render_hp_bar(lp.hp, lp.max_hp)}   "
        f"{BOLD}{lp.dnd_class.name}{RESET}  "
        f"AC:{lp.armor_class}  Pos:({lp.x},{lp.y}){status_str}"
    )
    parts.append(hp_line)

    # -- Scoreboard ----
    parts.append(f"\n{BOLD}Jogadores:{RESET}  {render_scoreboard(state)}")

    # -- Event log ----
    if state.event_log:
        parts.append(f"\n{BOLD}Eventos:{RESET}")
        for entry in state.event_log[-6:]:
            parts.append(f"  {entry}")

    # -- Ações ----
    parts.append("")
    parts.append(render_actions(state))

    output = "\n".join(parts)

    if clear:
        # -- Estratégia de render sem flickering ------------------
        # 1. Cursor para o topo do ecrã
        # 2. Cada linha do frame + \033[K  (evita lixo à direita)
        # 3. Linha de input (prompt + buffer) + \033[K
        # 4. \033[J  (apaga linhas antigas abaixo do frame actual)
        # 5. Cursor fica no fim do texto digitado → utilizador continua
        #    a ver exactamente o que escreveu.
        buf = _HOME
        for line in output.split("\n"):
            buf += line + _CLEAR_EOL + "\n"
        # Linha de input — sempre visível no fundo do frame
        buf += "\n" + input_line + _CLEAR_EOL + _CLEAR_BELOW
        sys.stdout.write(buf)
        sys.stdout.flush()

    return output

# -- LISTA DE CLASSES ----------------------------------------------

def list_classes() -> str:
    """Lista formatada de todas as classes disponíveis, para o menu pré-jogo."""
    lines = ["\n[Classes disponíveis]"]
    for key, c in CLASSES.items():
        lines.append(
            f"  {BOLD}{c.name:<10}{RESET}  HP:{c.hp_base:>3}  "
            f"AC:{c.armor_class_base:>2}  Range:{c.attack_range:>2}  "
            f"Arma: {c.weapon} ({c.damage_dice})"
        )
    return "\n".join(lines) + "\n"