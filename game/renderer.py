"""
game/renderer.py
Renderização do estado do jogo no terminal com ANSI escape codes.

render_frame(state) imprime (ou devolve como string) o ecrã completo:

  ┌──────── DEATHMATCH ─ 08:47 ────────┐
  │ mapa 20×10 com jogadores           │
  └────────────────────────────────────┘
  HP  ████████████░░░░  45/60   Fighter
  
  [Eventos]
  [10:23] Alice atacou com Longsword → 8 dano
  ...
  
  [Jogadores]   Alice 3K/1D   Bob 1K/2D
  
  [Ações]  [A]tacar  [M]over  [1]Second Wind  [2]Action Surge  [Q]uit
"""

import os
import time
from typing import Optional

from game_state import GameState, MatchPhase, MAP_W, MAP_H, MATCH_DURATION

# ── CORES ANSI ────────────────────────────────────────────────────

RESET  = "\033[0m"
BOLD   = "\033[1m"
RED    = "\033[31m"
GREEN  = "\033[32m"
YELLOW = "\033[33m"
CYAN   = "\033[36m"
WHITE  = "\033[37m"
DIM    = "\033[2m"


def _clear():
    os.system("cls" if os.name == "nt" else "clear")


# ── HP BAR ────────────────────────────────────────────────────────

def render_hp_bar(current: int, maximum: int, width: int = 20) -> str:
    if maximum == 0:
        return "░" * width
    filled = int(width * current / maximum)
    bar_color = GREEN if current / maximum > 0.5 else (YELLOW if current / maximum > 0.25 else RED)
    bar = bar_color + "█" * filled + DIM + "░" * (width - filled) + RESET
    return f"{bar}  {BOLD}{current}/{maximum}{RESET}"


# ── MAPA ──────────────────────────────────────────────────────────

def render_map(state: GameState) -> str:
    """
    Gera o mapa ASCII 20×10.
    Cada célula é 2 chars de largura para melhor legibilidade.
    
    Símbolos:
      ☺  → jogador local (vivo)
      ✝  → jogador local (morto)
      A-Z → outros jogadores (inicial do username)
      ·  → célula vazia
      #  → parede/borda
    """
    # Grid vazio
    grid = [["· " for _ in range(MAP_W)] for _ in range(MAP_H)]

    # Outros jogadores
    for p in state.others.values():
        if 0 <= p.x < MAP_W and 0 <= p.y < MAP_H:
            initial = p.username[0].upper()
            color = GREEN if p.alive else RED
            grid[p.y][p.x] = f"{color}{initial} {RESET}"

    # Jogador local (por cima de tudo)
    lp = state.local
    if 0 <= lp.x < MAP_W and 0 <= lp.y < MAP_H:
        symbol = f"{BOLD}{CYAN}☺ {RESET}" if lp.alive else f"{RED}✝ {RESET}"
        grid[lp.y][lp.x] = symbol

    # Bordas
    border_h = "─" * (MAP_W * 2 + 2)
    lines = [f"┌{border_h}┐"]
    for row in grid:
        lines.append("│ " + "".join(row) + "│")
    lines.append(f"└{border_h}┘")
    return "\n".join(lines)


# ── TIMER ─────────────────────────────────────────────────────────

def render_timer(state: GameState) -> str:
    secs = state.time_remaining()
    m, s = divmod(secs, 60)
    color = RED if secs < 60 else (YELLOW if secs < 120 else GREEN)
    return f"{color}⏱  {m:02d}:{s:02d}{RESET}"


# ── SCOREBOARD ────────────────────────────────────────────────────

def render_scoreboard(state: GameState) -> str:
    rows = state.scoreboard()
    parts = []
    for username, kills, deaths in rows:
        is_local = username == state.local.username
        color = BOLD + CYAN if is_local else WHITE
        parts.append(f"{color}{username}{RESET} {GREEN}{kills}K{RESET}/{RED}{deaths}D{RESET}")
    return "  ".join(parts)


# ── AÇÕES DISPONÍVEIS ─────────────────────────────────────────────

def render_actions(state: GameState) -> str:
    """Mostra as ações disponíveis e indica cooldowns em vermelho."""
    local = state.local
    lines = [f"{BOLD}[Ações]{RESET}"]

    lines.append(f"  {CYAN}[A]{RESET} Atacar   {CYAN}[M]{RESET} Mover")

    for i, ability in enumerate(local.dnd_class.abilities, start=1):
        ready = local.is_ability_ready(ability.name)
        color = GREEN if ready else RED
        cd_info = "" if ready else f"  (cooldown)"
        lines.append(f"  {color}[{i}]{RESET} {ability.name}{DIM}{cd_info}{RESET}")

    lines.append(f"  {RED}[Q]{RESET} Sair")
    return "\n".join(lines)


# ── FRAME COMPLETO ────────────────────────────────────────────────

def render_frame(state: GameState, clear: bool = True) -> str:
    """
    Devolve o ecrã completo como string.
    Se clear=True, também chama _clear() para limpar o terminal.
    """
    if clear:
        _clear()

    lp = state.local
    parts = []

    # ── Cabeçalho ────
    phase_label = {
        MatchPhase.LOBBY:    "LOBBY",
        MatchPhase.STARTING: "INICIANDO…",
        MatchPhase.ACTIVE:   "DEATHMATCH",
        MatchPhase.ENDED:    "FIM DE JOGO",
    }[state.phase]

    timer = render_timer(state) if state.phase == MatchPhase.ACTIVE else ""
    header = f"{BOLD}{YELLOW}{'─'*10} {phase_label} {timer} {'─'*10}{RESET}"
    parts.append(header)

    # ── Mapa ────
    parts.append(render_map(state))

    # ── HP bar ────
    status_str = ""
    if lp.status_effects:
        status_str = f"  {YELLOW}" + ", ".join(lp.status_effects) + RESET
    hp_line = (
        f"HP  {render_hp_bar(lp.hp, lp.max_hp)}   "
        f"{BOLD}{lp.dnd_class.name}{RESET}  "
        f"AC:{lp.armor_class}  Pos:({lp.x},{lp.y}){status_str}"
    )
    parts.append(hp_line)

    # ── Scoreboard ────
    parts.append(f"\n{BOLD}Jogadores:{RESET}  {render_scoreboard(state)}")

    # ── Event log ────
    if state.event_log:
        parts.append(f"\n{BOLD}Eventos:{RESET}")
        for entry in state.event_log[-6:]:
            parts.append(f"  {entry}")

    # ── Ações ────
    parts.append("")
    parts.append(render_actions(state))
    parts.append("")

    output = "\n".join(parts)
    if clear:
        print(output)
    return output