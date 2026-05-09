"""
game/main_game.py  —  entry point do jogo multiplayer

Uso:
  # Primeiro node (bootstrap):
  uv run main_game.py --username Alice --class fighter \\
                      --grpc-port 50051 --kad-port 8468

  # Nodes seguintes (ligam ao bootstrap):
  uv run main_game.py --username Bob   --class wizard  \\
                      --grpc-port 50052 --kad-port 8469 \\
                      --bootstrap-host 127.0.0.1 --bootstrap-kad-port 8468

  # Sem especificar classe (escolhe-se no menu):
  uv run main_game.py --username Carol \\
                      --grpc-port 50053 --kad-port 8470 \\
                      --bootstrap-host 127.0.0.1 --bootstrap-kad-port 8468

Classes disponíveis: fighter, wizard, rogue, cleric, ranger

Menu inicial
────────────
  class <nome>   — mudar de classe
  info           — ver detalhes da classe actual
  join           — entrar no jogo (limpa o ecrã e inicia)
  quit / q       — sair
"""

import asyncio
import argparse
import sys
import os

import aioconsole

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dnd_classes import CLASSES, get_class, list_classes, DnDClass
from game_node   import GameNode


# ── CORES ANSI (usadas só no menu) ────────────────────────────────

_R  = "\033[0m"
_B  = "\033[1m"
_Y  = "\033[33m"
_C  = "\033[36m"
_G  = "\033[32m"
_RE = "\033[31m"
_D  = "\033[2m"


# ── MENU ──────────────────────────────────────────────────────────

BANNER = f"""\
{_B}{_Y}
  ██████╗ ███╗   ██╗██████╗     ██████╗ ███████╗ █████╗ ████████╗██╗  ██╗
  ██╔══██╗████╗  ██║██╔══██╗    ██╔══██╗██╔════╝██╔══██╗╚══██╔══╝██║  ██║
  ██║  ██║██╔██╗ ██║██║  ██║    ██║  ██║█████╗  ███████║   ██║   ███████║
  ██║  ██║██║╚██╗██║██║  ██║    ██║  ██║██╔══╝  ██╔══██║   ██║   ██╔══██║
  ██████╔╝██║ ╚████║██████╔╝    ██████╔╝███████╗██║  ██║   ██║   ██║  ██║
  ╚═════╝ ╚═╝  ╚═══╝╚═════╝     ╚═════╝ ╚══════╝╚═╝  ╚═╝   ╚═╝   ╚═╝  ╚═╝
{_R}"""


def _clear():
    sys.stdout.write("\033[H\033[J")
    sys.stdout.flush()


def _render_menu(username: str, current_class: DnDClass | None):
    """Redesenha o menu completo."""
    _clear()
    print(BANNER)

    # Jogador actual
    print(f"  Jogador : {_B}{_C}{username}{_R}")
    if current_class:
        print(
            f"  Classe  : {_B}{_G}{current_class.name}{_R}"
            f"  HP:{current_class.hp_base}"
            f"  AC:{current_class.armor_class_base}"
            f"  {current_class.weapon} ({current_class.damage_dice} {current_class.damage_type})"
        )
        print(f"  Abilidades:")
        for ab in current_class.abilities:
            cd = f"  {_D}(cooldown {ab.cooldown_secs} turnos){_R}" if ab.cooldown_secs else ""
            print(f"    • {_B}{ab.name}{_R} — {ab.description}{cd}")
    else:
        print(f"  Classe  : {_RE}(nenhuma — usa 'class <nome>' para escolher){_R}")

    print()
    print(list_classes())
    print(
        f"  {_B}Comandos:{_R}\n"
        f"    {_C}class <nome>{_R}  — escolher/mudar classe\n"
        f"    {_C}info{_R}          — detalhes da classe actual\n"
        f"    {_G}join{_R}          — {_B}entrar no jogo{_R}\n"
        f"    {_RE}quit{_R}          — sair\n"
    )


async def run_menu(
    username: str,
    initial_class_name: str | None,
) -> DnDClass | None:
    """
    Menu interactivo antes de entrar no jogo.
    Devolve a DnDClass escolhida, ou None se o utilizador saiu.

    Usa aioconsole.ainput (stdin normal) — o InputReader só é activado
    depois de entrar no jogo.
    """
    current_class: DnDClass | None = get_class(initial_class_name) if initial_class_name else None
    _render_menu(username, current_class)

    while True:
        try:
            raw = await aioconsole.ainput(f"{_B}menu{_R}> ")
        except (EOFError, KeyboardInterrupt):
            return None

        parts = raw.strip().split()
        if not parts:
            _render_menu(username, current_class)
            continue

        cmd = parts[0].lower()

        # ── quit ──────────────────────────────────────────────
        if cmd in ("quit", "q", "exit"):
            print("\nAté à próxima!")
            return None

        # ── class <nome> ──────────────────────────────────────
        elif cmd == "class":
            if len(parts) < 2:
                print(f"  {_RE}[!]{_R} Uso: class <nome>  (ex: class fighter)")
                continue
            name = parts[1].lower()
            c = get_class(name)
            if c:
                current_class = c
                _render_menu(username, current_class)
                print(f"  {_G}✓{_R} Classe definida para {_B}{c.name}{_R}")
            else:
                available = ", ".join(CLASSES.keys())
                print(f"  {_RE}[!]{_R} Classe '{name}' desconhecida.  Disponíveis: {available}")

        # ── info ──────────────────────────────────────────────
        elif cmd == "info":
            if not current_class:
                print(f"  {_RE}[!]{_R} Sem classe seleccionada.")
            else:
                print(
                    f"\n  {_B}{current_class.name}{_R}\n"
                    f"  HP {current_class.hp_base}  AC {current_class.armor_class_base}  "
                    f"Arma: {current_class.weapon}  "
                    f"Dano: {current_class.damage_dice} {current_class.damage_type}\n"
                    f"  Ataque mod: +{current_class.attack_modifier}  "
                    f"Dano mod: +{current_class.damage_modifier}\n"
                )
                for ab in current_class.abilities:
                    spell = " [magia]" if ab.is_spell else ""
                    cd    = f"  cooldown {ab.cooldown_secs}t" if ab.cooldown_secs else "  sempre disponível"
                    print(f"  • {_B}{ab.name}{_R}{spell}  —  {ab.description}{_D}{cd}{_R}")
                print()

        # ── join ──────────────────────────────────────────────
        elif cmd == "join":
            if not current_class:
                print(f"  {_RE}[!]{_R} Escolhe uma classe primeiro: class <nome>")
                continue
            _clear()
            return current_class

        # ── desconhecido ──────────────────────────────────────
        else:
            print(
                f"  {_RE}[!]{_R} Comando desconhecido. "
                f"Usa: class <nome> | info | join | quit"
            )


# ── ARGS ──────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(
        description="DnD Deathmatch — P2P gRPC + Kademlia",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--username",           required=True,
                   help="O teu username (único na rede)")
    p.add_argument("--class",              dest="dnd_class", default=None,
                   help="Classe DnD inicial (pode ser mudada no menu)")
    p.add_argument("--host",               default="127.0.0.1",
                   help="IP público / endereço gRPC anunciado (default: 127.0.0.1)")
    p.add_argument("--grpc-port",          type=int, required=True,
                   help="Porta gRPC deste node")
    p.add_argument("--kad-port",           type=int, required=True,
                   help="Porta Kademlia deste node")
    p.add_argument("--bootstrap-host",     default=None,
                   help="IP do node bootstrap (omitir se és o primeiro)")
    p.add_argument("--bootstrap-kad-port", type=int, default=None,
                   help="Porta Kademlia do node bootstrap")
    p.add_argument("--match-id",           default="match1",
                   help="Identificador da partida (default: match1)")
    return p.parse_args()


# ── MAIN ──────────────────────────────────────────────────────────

async def main():
    args = parse_args()

    # Menu interactivo — devolve a classe escolhida ou None (sair)
    chosen_class = await run_menu(args.username, args.dnd_class)
    if chosen_class is None:
        return

    bootstrap = None
    if args.bootstrap_host and args.bootstrap_kad_port:
        bootstrap = (args.bootstrap_host, args.bootstrap_kad_port)

    print(
        f"A iniciar {args.username} ({chosen_class.name}) "
        f"na porta gRPC {args.grpc_port} / Kad {args.kad_port}…\n"
    )

    node = GameNode(
        username=args.username,
        dnd_class=chosen_class,
        host=args.host,
        grpc_port=args.grpc_port,
        kad_port=args.kad_port,
        bootstrap=bootstrap,
        match_id=args.match_id,
    )
    await node.run()


if __name__ == "__main__":
    asyncio.run(main())