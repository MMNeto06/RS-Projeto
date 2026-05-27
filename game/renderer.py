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

_PLAYER_PALETTE = [GREEN, YELLOW, MAGENTA, BLUE, WHITE,
                   "\033[92m", "\033[93m", "\033[95m"]


def _player_color(username: str) -> str:
    idx = sum(ord(c) for c in username) % len(_PLAYER_PALETTE)
    return _PLAYER_PALETTE[idx]


_HOME        = "\033[H"   
_CLEAR_EOL   = "\033[K"  
_CLEAR_BELOW = "\033[J"   


def render_hp_bar(current: int, maximum: int, width: int = 20) -> str:
    if maximum == 0:
        return "░" * width
    ratio  = current / maximum
    filled = int(width * ratio)
    color  = GREEN if ratio > 0.5 else (YELLOW if ratio > 0.25 else RED)
    bar    = color + "█" * filled + DIM + "░" * (width - filled) + RESET
    return f"{bar}  {BOLD}{current}/{maximum}{RESET}"


def render_map(state: GameState) -> str:
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

    border_h = "─" * (MAP_W * 2 + 2)
    lines = [f"┌{border_h}┐"]
    for row in grid:
        lines.append("│ " + "".join(row) + " │")
    lines.append(f"└{border_h}┘")
    return "\n".join(lines)


def render_timer(state: GameState) -> str:
    secs = state.time_remaining()
    m, s = divmod(secs, 60)
    color = RED if secs < 60 else (YELLOW if secs < 120 else GREEN)
    return f"{color}⏱  {m:02d}:{s:02d}{RESET}"


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


def render_frame(
    state: GameState,
    clear: bool = True,
    input_line: str = "> ",
) -> str:
    lp = state.local

    parts: list[str] = []

    phase_label = {
        MatchPhase.LOBBY:    "LOBBY",
        MatchPhase.STARTING: "INICIANDO…",
        MatchPhase.ACTIVE:   "DEATHMATCH",
        MatchPhase.ENDED:    "FIM DE JOGO",
    }[state.phase]

    timer  = render_timer(state) if state.phase == MatchPhase.ACTIVE else ""
    header = f"{BOLD}{YELLOW}{'─'*10} {phase_label} {timer} {'─'*10}{RESET}"
    parts.append(header)

    parts.append(render_map(state))

    status_str = ""
    if lp.status_effects:
        status_str = f"  {YELLOW}" + ", ".join(lp.status_effects) + RESET
    hp_line = (
        f"HP  {render_hp_bar(lp.hp, lp.max_hp)}   "
        f"{BOLD}{lp.dnd_class.name}{RESET}  "
        f"AC:{lp.armor_class}  Pos:({lp.x},{lp.y}){status_str}"
    )
    parts.append(hp_line)

    parts.append(f"\n{BOLD}Jogadores:{RESET}  {render_scoreboard(state)}")

    if state.event_log:
        parts.append(f"\n{BOLD}Eventos:{RESET}")
        for entry in state.event_log[-6:]:
            parts.append(f"  {entry}")

    parts.append("")
    parts.append(render_actions(state))

    output = "\n".join(parts)

    if clear:
        buf = _HOME
        for line in output.split("\n"):
            buf += line + _CLEAR_EOL + "\n"
        buf += "\n" + input_line + _CLEAR_EOL + _CLEAR_BELOW
        sys.stdout.write(buf)
        sys.stdout.flush()

    return output


def list_classes() -> str:
    lines = ["\n[Classes disponíveis]"]
    for key, c in CLASSES.items():
        lines.append(
            f"  {BOLD}{c.name:<10}{RESET}  HP:{c.hp_base:>3}  "
            f"AC:{c.armor_class_base:>2}  Range:{c.attack_range:>2}  "
            f"Arma: {c.weapon} ({c.damage_dice})"
        )
    return "\n".join(lines) + "\n"