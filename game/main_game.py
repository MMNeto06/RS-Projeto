import asyncio
import argparse
import sys
import os
import socket
import time
import aioconsole

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dnd_classes import CLASSES, get_class, DnDClass
from renderer import list_classes
from game_node import GameNode
from dht_keys import username_key, addr_key
import game_pb2
from lobby.lobby_enums import LobbyStatus

import logging


_R  = "\033[0m"
_B  = "\033[1m"
_Y  = "\033[33m"
_C  = "\033[36m"
_G  = "\033[32m"
_RE = "\033[31m"
_D  = "\033[2m"
_M  = "\033[35m"

def get_lan_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("", 0))
        return s.getsockname()[1]


_BEACON_PORT  = 47777        
_BEACON_MAGIC = b"DNDGAME_V1"  


async def _beacon_loop(kad_port: int):
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        msg = f"DNDGAME_V1:{kad_port}".encode()
        while True:
            try:
                sock.sendto(msg, ("<broadcast>", _BEACON_PORT))
            except Exception:
                pass
            await asyncio.sleep(2.0)
    except asyncio.CancelledError:
        pass
    finally:
        try:
            sock.close()
        except Exception:
            pass


async def _beacon_failover_loop(kad_port: int, initial_bootstrap: bool):
    if initial_bootstrap:
        await _beacon_loop(kad_port)
        return

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

    sock.bind(("", _BEACON_PORT))
    sock.setblocking(False)

    loop = asyncio.get_running_loop()
    last_heard = loop.time()

    try:
        while True:
            try:
                data, _ = sock.recvfrom(256)
                if data.startswith(_BEACON_MAGIC + b":"):
                    last_heard = loop.time()
            except Exception:
                pass

            if (loop.time() - last_heard) > 6.0:
                print("\nBeacon died, starting up again in this node")
                sock.close()
                await _beacon_loop(kad_port)
                return

            await asyncio.sleep(0.5)
    except Exception:
        pass
    finally:
        try:
            sock.close()
        except Exception:
            pass


async def discover_bootstrap(timeout: float = 4.0):
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except (AttributeError, OSError):
            pass  
        sock.bind(("", _BEACON_PORT))
        sock.setblocking(False)

        loop     = asyncio.get_event_loop()
        deadline = loop.time() + timeout

        while loop.time() < deadline:
            try:
                data, addr = sock.recvfrom(256)
                if data.startswith(_BEACON_MAGIC + b":"):
                    try:
                        kad_port = int(data.split(b":")[1])
                        return (addr[0], kad_port)
                    except (IndexError, ValueError):
                        pass
            except BlockingIOError:
                await asyncio.sleep(0.05)
            except Exception:
                await asyncio.sleep(0.05)

        return None

    except Exception as e:
        print(f"  {_D}[Auto] Erro a ouvir beacons: {e}{_R}")
        return None
    finally:
        try:
            sock.close()
        except Exception:
            pass

async def _username_is_free(node: "GameNode", name: str) -> bool:
    existing_id = await node.kad.get(username_key(name))
    if not existing_id:
        return True

    if existing_id == node.node_id:
        return True

    addr = await node.kad.get(addr_key(existing_id))
    if not addr:
        return True  

    try:
        try:
            stub = await node.get_or_create_grpc_stub(addr)
            if stub is None:
                return True
            await asyncio.wait_for(
                stub.Ping(game_pb2.PingRequest(from_user=node.username)),
                timeout=2.0,
            )
            return False  
        except Exception:
            return True
    except Exception:
        return True  


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
    _clear()
    print(BANNER)

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
            cd = f"  {_D}(cooldown {ab.cooldown_secs}s){_R}" if ab.cooldown_secs else ""
            print(f"    • {_B}{ab.name}{_R} — {ab.description}{cd}")
    else:
        print(f"  Classe  : {_RE}(nenhuma — usa 'class <nome>' para escolher){_R}")

    print()
    print(list_classes())

    if lobby_code:
        print(
            f"  {_B}Comandos:{_R}\n"
            f"    {_C}name <novo>{_R}           — mudar o teu username\n"
            f"    {_C}class <nome>{_R}          — escolher/mudar classe\n"
            f"    {_C}info{_R}                  — detalhes da classe actual\n"
            f"\n"
            f"  {_B}{_M}Lobby activo : {lobby_code}{_R}\n"
            f"    {_G}ready{_R}                 — marcar como pronto\n"
            f"    {_G}unready{_R}               — cancelar prontidão\n"
            f"    {_C}lobby status{_R}          — ver quem está pronto\n"
            f"    {_C}lobby start{_R}           — {_B}iniciar jogo{_R} (só o host, quando todos prontos)\n"
            f"    {_RE}lobby leave{_R}          — sair do lobby\n"
            f"\n"
            f"    {_RE}quit{_R}                 — sair do jogo\n"
        )
    else:
        print(
            f"  {_B}Comandos:{_R}\n"
            f"    {_C}name <novo>{_R}               — mudar o teu username\n"
            f"    {_C}class <nome>{_R}              — escolher/mudar classe\n"
            f"    {_C}info{_R}                      — detalhes da classe actual\n"
            f"\n"
            f"  {_B}{_M}🎮 Lobbies:{_R}\n"
            f"    {_G}lobby create <nome> <max>{_R} — criar um novo lobby\n"
            f"    {_G}lobby join <código>{_R}       — entrar num lobby existente\n"
            f"    {_C}lobby list{_R}                — listar lobbies abertos\n"
            f"\n"
            f"    {_RE}quit{_R}                      — sair do jogo\n"
        )


async def run_menu(
    username: str,
    initial_class_name: str | None,
    node: "GameNode",
) -> DnDClass | None:
    current_class: DnDClass | None = get_class(initial_class_name) if initial_class_name else get_class("fighter")
    _render_menu(username, current_class, node.lobby.session.code if node.lobby.session else None)

    _game_started = asyncio.Event()

    async def _lobby_watcher():
        await node.lobby_started_event.wait()
        _game_started.set()

        while not _game_started.is_set():
            if not node.lobby.session:
                continue

            lobby = await node.lobby.repo.get_lobby(node.lobby.session.code)
            if lobby and lobby.status == LobbyStatus.STARTED:
                _game_started.set()
                return 

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
                _render_menu(username, current_class, node.lobby.session.code if node.lobby.session else None)
                continue

            cmd = parts[0].lower()

            if cmd in ("quit", "q", "exit"):
                print("\nAté à próxima!")
                return None

            elif cmd == "name":
                if len(parts) < 2:
                    print(f"  {_RE}[!]{_R} Uso: name <novo_username>")
                    continue
                new_name = parts[1].strip()
                if not new_name:
                    print(f"  {_RE}[!]{_R} Username não pode ser vazio.")
                    continue
                if node.lobby.session:
                    print(f"  {_RE}[!]{_R} Não podes mudar o nome enquanto estás num lobby.")
                    continue
                print(f"  {_D}A verificar disponibilidade…{_R}")
                if not await _username_is_free(node, new_name):
                    print(f"  {_RE}[!]{_R} Username '{new_name}' já está em uso por outro jogador.")
                    continue
                username = new_name
                node.username = new_name
                node.state.local.username = new_name
                await node.register_self(force=True)
                _render_menu(username, current_class, node.lobby.session.code if node.lobby.session else None)

            elif cmd == "class":
                if len(parts) < 2:
                    print(f"  {_RE}[!]{_R} Uso: class <nome>  (ex: class fighter)")
                    continue
                c = get_class(parts[1].lower())
                if c:
                    current_class = c
                    node.state.local.dnd_class = c
                    await node.register_self(force=True)
                    _render_menu(username, current_class, node.lobby.session.code if node.lobby.session else None)
                else:
                    print(f"  {_RE}[!]{_R} Classe desconhecida: '{parts[1]}'")
                    print(list_classes())

            elif cmd == "info":
                if not current_class:
                    print(f"  {_RE}[!]{_R} Nenhuma classe seleccionada.")
                else:
                    print(
                        f"\n  {_B}{current_class.name}{_R}\n"
                        f"  HP: {current_class.hp_base}  AC: {current_class.armor_class_base}\n"
                        f"  Arma: {current_class.weapon} ({current_class.damage_dice})\n"
                        f"  Ataque mod: +{current_class.attack_modifier}  "
                        f"Dano mod: +{current_class.damage_modifier}\n"
                    )
                    for ab in current_class.abilities:
                        spell = " [magia]" if ab.is_spell else ""
                        cd    = f"  cooldown {ab.cooldown_secs}s" if ab.cooldown_secs else "  sempre disponível"
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
                    await asyncio.sleep(1.5)
                    _render_menu(username, current_class, node.lobby.session.code if node.lobby.session else None)

                elif sub == "join":
                    if not current_class:
                        print(f"  {_RE}[!]{_R} Escolhe uma classe antes de entrar num lobby.")
                        continue
                    if len(parts) < 3:
                        print("  Uso: lobby join <código>")
                        continue
                    await node.lobby.join(parts[2].upper(), my_addr)
                    _render_menu(username, current_class, node.lobby.session.code if node.lobby.session else None)

                elif sub == "list":
                    await node.lobby.list_open()

                elif sub == "leave":
                    await node.lobby.leave()
                    _render_menu(username, current_class, node.lobby.session.code if node.lobby.session else None)

                elif sub == "status":
                    await node.lobby.status()

                elif sub == "start":
                    if not current_class:
                        print(f"  {_RE}[!]{_R} Escolhe uma classe primeiro.")
                        continue
                    ok = await node.lobby.start()
                    if ok:
                        return current_class

                else:
                    print(f"  {_RE}[!]{_R} Subcomando desconhecido: '{sub}'")

            elif cmd == "ready":
                await node.lobby.set_ready(True)

            elif cmd == "unready":
                await node.lobby.set_ready(False)

            else:
                print(
                    f"  {_RE}[!]{_R} Comando desconhecido. "
                    f"Usa: name | class | info | lobby ... | ready | quit"
                )

            if node.lobby.session:
                lobby = await node.lobby.repo.get_lobby(node.lobby.session.code)
                if lobby and lobby.status == LobbyStatus.STARTED and current_class:
                    print(f"\n  {_G}[Lobby] O jogo foi iniciado! A entrar…{_R}")
                    await asyncio.sleep(1)
                    return current_class

    finally:
        watcher_task.cancel()
        try:
            await watcher_task
        except asyncio.CancelledError:
            pass

def parse_args():
    p = argparse.ArgumentParser(
        description="DnD Deathmatch — P2P gRPC + Kademlia",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Modo simples (LAN): basta --username (ou omite — será pedido interactivamente).\n"
            "Modo manual: especifica --host, --grpc-port, --kad-port, --bootstrap-host, etc."
        ),
    )

    p.add_argument(
        "--username", required=False, default=None,
        help="O teu username (único na rede). Se omitido, é pedido no arranque.",
    )

    p.add_argument(
        "--class", dest="dnd_class", default=None,
        help="Classe DnD inicial (pode ser mudada no menu)",
    )

    p.add_argument(
        "--host", default=None,
        help=(
            "IP anunciado na DHT para outros peers contactarem o teu gRPC. "
            "Se omitido, é auto-detectado (IP LAN via socket UDP)."
        ),
    )
    p.add_argument(
        "--grpc-port", type=int, default=None,
        help="Porta gRPC deste node (default: porta livre escolhida pelo SO).",
    )
    p.add_argument(
        "--kad-port", type=int, default=None,
        help="Porta Kademlia deste node (default: porta livre escolhida pelo SO).",
    )

    p.add_argument(
        "--bootstrap-host", default=None,
        help=(
            "IP do node bootstrap. Se omitido, é descoberto via beacon UDP na LAN. "
            "Se não houver bootstrap na rede, este node torna-se o primeiro."
        ),
    )
    p.add_argument(
        "--bootstrap-kad-port", type=int, default=None,
        help="Porta Kademlia do node bootstrap (necessário com --bootstrap-host).",
    )

    p.add_argument(
        "--first", action="store_true",
        help=(
            "Salta a descoberta de bootstrap e assume imediatamente o papel "
            "de primeiro node. Útil se o timeout de descoberta for lento ou "
            "para garantir que este node é o bootstrap."
        ),
    )
    p.add_argument(
        "--discovery-timeout", type=float, default=4.0, metavar="SECS",
        help="Segundos à espera de um beacon de bootstrap (default: 4).",
    )

    p.add_argument(
        "--match-id", default="match1",
        help="Identificador da partida (default: match1)",
    )

    return p.parse_args()


async def main():
    args = parse_args()

    if not args.username:
        while True:
            args.username = input("Username: ").strip()
            if args.username:
                break
            print("Username não pode ser vazio.")

    if args.host:
        host = args.host
        print(f"[Rede] Host manual: {host}")
    else:
        host = get_lan_ip()
        print(f"[Rede] Host auto-detectado: {host}")

    grpc_port = args.grpc_port if args.grpc_port else find_free_port()
    kad_port  = args.kad_port  if args.kad_port  else find_free_port()
    print(f"[Rede] gRPC:{grpc_port}  Kademlia:{kad_port}")

    bootstrap = None

    if args.bootstrap_host and args.bootstrap_kad_port:
        bootstrap = (args.bootstrap_host, args.bootstrap_kad_port)
        print(f"[DHT] Bootstrap manual: {bootstrap[0]}:{bootstrap[1]}")

    elif args.first:
        print("[DHT] Modo --first: este node é o bootstrap.")
        bootstrap = None

    else:
        print(
            f"[DHT] À procura de outros jogadores na LAN "
            f"({args.discovery_timeout:.0f}s)…"
        )
        result = await discover_bootstrap(timeout=args.discovery_timeout)
        if result:
            bootstrap = result
            print(f"[DHT] Bootstrap encontrado: {result[0]}:{result[1]}")
        else:
            print("[DHT] Nenhum bootstrap encontrado -> este node é o primeiro.")
            bootstrap = None

    placeholder_class = get_class(args.dnd_class) if args.dnd_class else get_class("fighter")

    node = GameNode(
        username=args.username,
        dnd_class=placeholder_class,
        host=host,
        grpc_port=grpc_port,
        kad_port=kad_port,
        bootstrap=bootstrap,
        match_id=args.match_id,
    )

    logging.getLogger("kademlia").setLevel(logging.ERROR)

    await node.start_kademlia()
    await node.start_grpc_server()

    print(f"[DHT] A verificar disponibilidade do username '{node.username}'…")
    while not await _username_is_free(node, node.username):
        print(f"[!] Username '{node.username}' já está em uso por outro jogador.")
        while True:
            new_name = input("Escolhe outro username: ").strip()
            if new_name:
                break
            print("Username não pode ser vazio.")
        node.username = new_name
        node.state.local.username = new_name
        args.username = new_name

    await node.register_self()
    asyncio.create_task(node._refresh_loop())

    is_initial_bootstrap = (bootstrap is None)
    if is_initial_bootstrap:
        print(f"[DHT] A emitir beacon na LAN (porta {_BEACON_PORT})…")
    
    beacon_task = asyncio.create_task(
        _beacon_failover_loop(kad_port, is_initial_bootstrap)
    )

    chosen_class = await run_menu(node.username, args.dnd_class, node)
    if chosen_class is None:
        if node.grpc_server:
            await node.grpc_server.stop(grace=2)
        if beacon_task:
            beacon_task.cancel()
        return

    node.state.local.dnd_class = chosen_class
    node.state.local.hp        = chosen_class.hp_base
    await node.register_self(force=True)

    if node.lobby.session:
        from game_state import MatchPhase
        node.state.phase      = MatchPhase.ACTIVE
        node.state.start_time = time.time()
        node.state.match_id   = node.lobby.session.code

    await node.input_loop()

    leave_req = game_pb2.LeaveNotification(username=node.username)
    tasks = [
        node.send_rpc(p["grpc_addr"], 'Leave', leave_req)
        for nid, p in node.peers.items()
        if nid != node.node_id
    ]
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)

    if node.grpc_server:
        await node.grpc_server.stop(grace=2)
    if beacon_task:
        beacon_task.cancel()
    print("\n[Jogo] Saíste. Até à próxima!")


if __name__ == "__main__":
    asyncio.run(main())