"""
game/main_game.py  —  entry point do jogo multiplayer

Uso:
  # Primeiro node (bootstrap):
  uv run main_game.py --username Alice --class fighter \\
                      --grpc-port 50051 --kad-port 8468

  # Nodes seguintes (ligam ao bootstrap):
  uv run main_game.py --username Bob --class wizard --grpc-port 50052 --kad-port 8469 --bootstrap-host 127.0.0.1 --bootstrap-kad-port 8468

  # Sem especificar classe (escolhe-se no menu):
  uv run main_game.py --username Carol \\
                      --grpc-port 50053 --kad-port 8470 \\
                      --bootstrap-host 127.0.0.1 --bootstrap-kad-port 8468

Classes disponíveis: fighter, wizard, rogue, cleric, ranger, sorcerer, paladin, monk, warlock

Fluxo completo:
  1. Menu  →  escolher classe
  2. Menu  →  lobby create <nome> <max>   ou   lobby join <código>
  3. Menu  →  ready  (quando prontos)
  4. Menu  →  lobby start  (só o host; arranca o jogo para toda a gente)
  5. Jogo

"""

import asyncio
import argparse
import sys
import os

import aioconsole

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dnd_classes import CLASSES, get_class, list_classes, DnDClass
from game_node   import GameNode

import logging


# ── CORES ANSI (usadas só no menu) ────────────────────────────────

_R  = "\033[0m"
_B  = "\033[1m"
_Y  = "\033[33m"
_C  = "\033[36m"
_G  = "\033[32m"
_RE = "\033[31m"
_D  = "\033[2m"
_M  = "\033[35m"

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


def _render_menu(username: str, current_class: DnDClass | None, lobby_code: str | None):
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
            f"  {current_class.weapon} ({current_class.damage_dice})"
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

    if lobby_code:
        print(f"\n  {_M}Lobby activo : {_B}{lobby_code}{_R}")
        print(
            f"  {_D}  ready · unready · lobby status · lobby leave{_R}\n"
            f"  {_D}  lobby start  (só o host, quando todos prontos){_R}"
        )
    else:
        print(
            f"\n  {_M}🎮 Lobbies{_R}\n"
            f"  {_D}  lobby create <nome> <max>   · lobby join <código>{_R}\n"
            f"  {_D}  lobby list{_R}"
        )



async def run_menu(
    username: str,
    initial_class_name: str | None,
    node: "GameNode",
) -> DnDClass | None:
    """
    Menu interactivo antes de entrar no jogo.
 
    Devolve a DnDClass escolhida quando o jogo deve começar,
    ou None se o utilizador saiu.
    """
    current_class: DnDClass | None = get_class(initial_class_name) if initial_class_name else None
    _render_menu(username, current_class, node.lobby.current_lobby)

    _game_started = asyncio.Event()

    async def _lobby_watcher():
        import json
        from dht_keys import lobby_meta_key
        while not _game_started.is_set():
            await asyncio.sleep(1.5)
            if not node.lobby.current_lobby:
                continue
            try:
                raw_meta = await node.lobby.kad.get(
                    lobby_meta_key(node.lobby.current_lobby)
                )
                if raw_meta:
                    meta = json.loads(raw_meta)
                    if meta.get("status") == "started":
                        _game_started.set()
                        return
            except Exception:
                pass

    watcher_task = asyncio.create_task(_lobby_watcher())
    
    try: 
        while True:
            input_task = asyncio.create_task(
                aioconsole.ainput(f"{_B}menu{_R}> ")
            )

            done, pending = await asyncio.wait(
                [input_task, asyncio.create_task(_game_started.wait())],
                return_when=asyncio.FIRST_COMPLETED,
            )

            for t in pending:
                t.cancel()

            if _game_started.is_set():
                if not current_class:
                    current_class = get_class("fighter")
                print(f"\n  {_G}[Lobby] O jogo foi iniciado! A entrar…{_R}")
                await asyncio.sleep(0.5)
                return current_class
                
            try:
                raw = input_task.result()
            except Exception:
                return None

    
            parts = raw.strip().split()
            if not parts:
                _render_menu(username, current_class, node.lobby.current_lobby)
                continue
    
            cmd = parts[0].lower()
    
            # ── quit ──────────────────────────────────────────────────
            if cmd in ("quit", "q", "exit"):
                print("\nAté à próxima!")
                return None
    
            # ── class <nome> ──────────────────────────────────────────
            elif cmd == "class":
                if len(parts) < 2:
                    print(f"  {_RE}[!]{_R} Uso: class <nome>  (ex: class fighter)")
                    continue
                c = get_class(parts[1].lower())
                if c:
                    current_class = c
                    _render_menu(username, current_class, node.lobby.current_lobby)
                    print(f"  {_G}✓{_R} Classe definida para {_B}{c.name}{_R}")
                else:
                    print(f"  {_RE}[!]{_R} Classe desconhecida. Disponíveis: {', '.join(CLASSES)}")
    
            # ── info ──────────────────────────────────────────────────
            elif cmd == "info":
                if not current_class:
                    print(f"  {_RE}[!]{_R} Sem classe seleccionada.")
                else:
                    print(f"\n  {_B}{current_class.name}{_R}")
                    print(
                        f"  HP {current_class.hp_base}  AC {current_class.armor_class_base}  "
                        f"Arma: {current_class.weapon}  Dano: {current_class.damage_dice}\n"
                        f"  Ataque mod: +{current_class.attack_modifier}  "
                        f"Dano mod: +{current_class.damage_modifier}\n"
                    )
                    for ab in current_class.abilities:
                        spell = " [magia]" if ab.is_spell else ""
                        cd    = f"  cooldown {ab.cooldown_secs}t" if ab.cooldown_secs else "  sempre disponível"
                        print(f"  • {_B}{ab.name}{_R}{spell}  —  {ab.description}{_D}{cd}{_R}")
                    print()
    
            elif cmd == "lobby":
                if len(parts) < 2:
                    print(
                        f"  Subcomandos de lobby:\n"
                        f"    lobby create <nome> <max>   — criar lobby\n"
                        f"    lobby join <código>          — entrar num lobby\n"
                        f"    lobby list                   — ver lobbies abertos\n"
                        f"    lobby leave                  — sair do lobby\n"
                        f"    lobby status                 — ver quem está pronto\n"
                        f"    lobby start                  — iniciar (só o host)\n"
                    )
                    continue
    
                sub = parts[1].lower()
                my_addr = f"{node.host}:{node.grpc_port}"
    
                if sub == "create":
                    if not current_class:
                        print(f"  {_RE}[!]{_R} Escolhe uma classe antes de criar um lobby.")
                        continue
                    if len(parts) < 4:
                        print("  Uso: lobby create <nome> <maxJogadores>")
                        continue
                    try:
                        max_p = int(parts[3])
                    except ValueError:
                        print("  [!] maxJogadores deve ser um número inteiro.")
                        continue
                    await node.lobby.create(parts[2], max_p, my_addr)
                    #_render_menu(username, current_class, node.lobby.current_lobby)
    
                elif sub == "join":
                    if not current_class:
                        print(f"  {_RE}[!]{_R} Escolhe uma classe antes de entrar num lobby.")
                        continue
                    if len(parts) < 3:
                        print("  Uso: lobby join <código>")
                        continue
                    await node.lobby.join(parts[2].upper(), my_addr)
                    _render_menu(username, current_class, node.lobby.current_lobby)
    
                elif sub == "list":
                    await node.lobby.list_open()
    
                elif sub == "leave":
                    await node.lobby.leave()
                    _render_menu(username, current_class, node.lobby.current_lobby)
    
                elif sub == "status":
                    await node.lobby.status()
    
                elif sub == "start":
                    # Quando o host faz start, os non-hosts ficam a saber via:
                    #   1. LobbyNotification gRPC (imediato)
                    #   2. sync_loop a correr em background (polling DHT de 3 em 3s)
                    # Em ambos os casos, o menu deles recebe sinal para avançar.
                    if not current_class:
                        print(f"  {_RE}[!]{_R} Escolhe uma classe primeiro.")
                        continue
                    ok = await node.lobby.start()
                    if ok:
                        # Host avança directamente para o jogo
                        return current_class
    
                else:
                    print(f"  {_RE}[!]{_R} Subcomando desconhecido: '{sub}'")
    
            
            elif cmd == "ready":
                await node.lobby.set_ready(True)
    
            elif cmd == "unready":
                await node.lobby.set_ready(False)
    
            # ── desconhecido ──────────────────────────────────────────
            else:
                print(
                    f"  {_RE}[!]{_R} Comando desconhecido. "
                    f"Usa: class | info | lobby ... | ready | join | quit"
                )
    
            if node.lobby.current_lobby:
                import json
                try:
                    from dht_keys import lobby_meta_key
                    raw_meta = await node.lobby.kad.get(
                        lobby_meta_key(node.lobby.current_lobby)
                    )
                    if raw_meta:
                        meta = json.loads(raw_meta)
                        if meta.get("status") == "started" and current_class:
                            print(
                                f"\n  {_G}[Lobby] O jogo foi iniciado! A entrar…{_R}"
                            )
                            await asyncio.sleep(1)
                            return current_class
                except Exception:
                    pass
    finally:
        watcher_task.cancel()
        try:
            await watcher_task
        except asyncio.CancelledError:
            pass


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
 
    bootstrap = None
    if args.bootstrap_host and args.bootstrap_kad_port:
        bootstrap = (args.bootstrap_host, args.bootstrap_kad_port)
 
    # O GameNode é criado e a infra (Kademlia + gRPC) é arrancada ANTES do menu,
    # de forma a que os comandos de lobby no menu possam comunicar com a DHT.
    # Usamos uma classe placeholder; a classe real é injectada abaixo.
    placeholder_class = get_class(args.dnd_class) or get_class("fighter")
 
    node = GameNode(
        username=args.username,
        dnd_class=placeholder_class,
        host=args.host,
        grpc_port=args.grpc_port,
        kad_port=args.kad_port,
        bootstrap=bootstrap,
        match_id=args.match_id,
    )
 
    logging.getLogger("kademlia").setLevel(logging.ERROR)
 
    await node.start_kademlia()
    await node.start_grpc_server()
    await node.register_self()
    asyncio.create_task(node._refresh_loop())
 
    chosen_class = await run_menu(args.username, args.dnd_class, node)
    if chosen_class is None:
        if node.grpc_server:
            await node.grpc_server.stop(grace=2)
        return
 
    # Actualizar a classe do jogador com a escolha final do menu
    node.state.local.dnd_class = chosen_class
    node.state.local.hp        = chosen_class.hp_base
 
    # Se viemos de um lobby, sincronizar a fase e o match_id antes de entrar
    # no input_loop, para que o broadcast de MATCH_STARTED não seja redundante
    # com o que o lobby já anunciou, e para que o timer de partida esteja certo.
    if node.lobby.current_lobby:
        import time
        from game_state import MatchPhase
        node.state.phase      = MatchPhase.ACTIVE
        node.state.start_time = time.time()
        node.state.match_id   = node.lobby.current_lobby
 
    await node.input_loop()
 
    # Notificar peers da saída
    import game_pb2
    leave_req = game_pb2.LeaveNotification(username=args.username)
    tasks = [
        node._send(p["grpc_addr"], lambda s, r: s.Leave(r), leave_req)
        for nid, p in node.peers.items()
        if nid != node.node_id
    ]
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
 
    if node.grpc_server:
        await node.grpc_server.stop(grace=2)
    print("\n[Jogo] Saíste. Até à próxima!")



if __name__ == "__main__":
    asyncio.run(main())