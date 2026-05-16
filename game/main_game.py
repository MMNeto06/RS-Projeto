"""
game/main_game.py  —  entry point do jogo multiplayer

─── Modo automático (recomendado para jogar na mesma rede) ───────────────────

  # Primeiro a arrancar (torna-se bootstrap, anuncia presença na LAN):
  uv run main_game.py --username Alice

  # Todos os outros (descobrem o bootstrap via broadcast UDP):
  uv run main_game.py --username Bob

  # Sem flags — pede o username interactivamente:
  uv run main_game.py

O IP local é detectado automaticamente. As portas gRPC e Kademlia são
escolhidas pelo SO (livres). O bootstrap é descoberto via beacon UDP na LAN.

─── Modo manual (para testes / forwarding / WAN) ────────────────────────────

  # Bootstrap:
  uv run main_game.py --username Alice --host 192.168.1.10 \
                      --grpc-port 50051 --kad-port 8468

  # Outros nodes:
  uv run main_game.py --username Bob --host 192.168.1.20 \
                      --grpc-port 50052 --kad-port 8469 \
                      --bootstrap-host 192.168.1.10 --bootstrap-kad-port 8468

─── Flags úteis ─────────────────────────────────────────────────────────────

  --first          Salta a descoberta de bootstrap e assume logo o papel
                   de primeiro node (útil se o timeout de 4s for lento).
  --discovery-timeout N   Segundos à espera de beacon (default: 4).

─── Como funciona o --host ──────────────────────────────────────────────────

  O servidor gRPC escuta em 0.0.0.0 (todas as interfaces), mas o endereço
  que fica registado na DHT é "{host}:{grpc_port}". Os outros peers lêem
  esse endereço da DHT para te contactar. Se --host for 127.0.0.1, cada peer
  tenta ligar ao seu próprio loopback — e falha. Na LAN tens de anunciar o
  teu IP 192.168.1.xxx. Este script detecta-o automaticamente.

─── Classes disponíveis ─────────────────────────────────────────────────────

  fighter, wizard, rogue, cleric, ranger, sorcerer, paladin, monk, warlock

"""

import asyncio
import argparse
import sys
import os
import socket

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


# ── AUTO-DETECÇÃO DE REDE ─────────────────────────────────────────

def get_lan_ip() -> str:
    """
    Devolve o IP LAN desta máquina (ex: 192.168.1.42).

    Truque: abre um socket UDP para um endereço externo sem enviar nada.
    O SO escolhe a interface de saída correcta e podemos ler o IP local
    do socket. Funciona em Linux, macOS e Windows.
    """
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def find_free_port() -> int:
    """
    Pede ao SO uma porta livre (bind em :0) e devolve-a.
    Funciona para TCP e UDP — os protocolos do jogo não partilham portas.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("", 0))
        return s.getsockname()[1]


# ── DESCOBERTA DE BOOTSTRAP VIA UDP BROADCAST ─────────────────────

_BEACON_PORT  = 47777          # porta de descoberta (não usada pelo jogo)
_BEACON_MAGIC = b"DNDGAME_V1"  # evita conflitos com outro tráfego broadcast


async def _beacon_loop(kad_port: int):
    """
    Corre no background no node bootstrap.
    Faz broadcast a cada 2 s: "DNDGAME_V1:<kad_port>"
    Os outros nodes ouvem este beacon e usam-no para o bootstrap.
    """
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


async def discover_bootstrap(timeout: float = 4.0):
    """
    Escuta beacons UDP na LAN durante `timeout` segundos.
    Devolve (host_ip, kad_port) do primeiro bootstrap encontrado, ou None.

    Implementação non-blocking: usa asyncio.sleep(0.05) em vez de
    sock.settimeout, para não bloquear o event loop.
    """
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        # SO_REUSEPORT permite que vários jogadores na mesma máquina
        # ouçam ao mesmo tempo (útil em testes locais).
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except (AttributeError, OSError):
            pass  # Windows não tem SO_REUSEPORT
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


# ── VERIFICAÇÃO DE USERNAME ────────────────────────────────────────

async def _username_is_free(node: "GameNode", name: str) -> bool:
    """
    Verifica se um username está disponível na DHT.

    1. Consulta a chave username:<name> na DHT.
    2. Se não existir → livre.
    3. Se existir, tenta fazer Ping ao node correspondente.
       - Sem resposta → entrada obsoleta → consideramos livre.
       - Com resposta → username realmente em uso.
    """
    import game_pb2
    import game_pb2_grpc
    import grpc
    from dht_keys import username_key, addr_key

    existing_id = await node.kad.get(username_key(name))
    if not existing_id:
        return True

    # Mesmo node_id → somos nós próprios (mudança de username para o mesmo valor)
    if existing_id == node.node_id:
        return True

    addr = await node.kad.get(addr_key(existing_id))
    if not addr:
        return True  # node_id sem endereço → entrada obsoleta

    try:
        async with grpc.aio.insecure_channel(addr) as ch:
            stub = game_pb2_grpc.GameServiceStub(ch)
            await asyncio.wait_for(
                stub.Ping(game_pb2.PingRequest(from_user=node.username)),
                timeout=2.0,
            )
        return False  # respondeu → username em uso
    except Exception:
        return True   # não respondeu → entrada obsoleta


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
    """
    Menu interactivo antes de entrar no jogo.
    Devolve a DnDClass escolhida ou None se o utilizador saiu.
    """
    current_class: DnDClass | None = get_class(initial_class_name) if initial_class_name else get_class("fighter")
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
                if node.lobby.current_lobby:
                    print(f"  {_RE}[!]{_R} Não podes mudar o nome enquanto estás num lobby.")
                    continue
                print(f"  {_D}A verificar disponibilidade…{_R}")
                if not await _username_is_free(node, new_name):
                    print(f"  {_RE}[!]{_R} Username '{new_name}' já está em uso por outro jogador.")
                    continue
                username = new_name
                node.username = new_name
                node.state.local.username = new_name
                await node.register_self()
                _render_menu(username, current_class, node.lobby.current_lobby)

            elif cmd == "class":
                if len(parts) < 2:
                    print(f"  {_RE}[!]{_R} Uso: class <nome>  (ex: class fighter)")
                    continue
                c = get_class(parts[1].lower())
                if c:
                    current_class = c
                    _render_menu(username, current_class, node.lobby.current_lobby)
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
                    _render_menu(username, current_class, node.lobby.current_lobby)

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
                            print(f"\n  {_G}[Lobby] O jogo foi iniciado! A entrar…{_R}")
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
        epilog=(
            "Modo simples (LAN): basta --username (ou omite — será pedido interactivamente).\n"
            "Modo manual: especifica --host, --grpc-port, --kad-port, --bootstrap-host, etc."
        ),
    )

    # ── Username (opcional — pedido interactivamente se omitido) ──
    p.add_argument(
        "--username", required=False, default=None,
        help="O teu username (único na rede). Se omitido, é pedido no arranque.",
    )

    # ── Classe (opcional; pode escolher-se no menu) ───────────────
    p.add_argument(
        "--class", dest="dnd_class", default=None,
        help="Classe DnD inicial (pode ser mudada no menu)",
    )

    # ── Rede — tudo opcional; auto-detectado se omitido ───────────
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

    # ── Bootstrap manual ──────────────────────────────────────────
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

    # ── Flags de comportamento ────────────────────────────────────
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

    # ── Outros ───────────────────────────────────────────────────
    p.add_argument(
        "--match-id", default="match1",
        help="Identificador da partida (default: match1)",
    )

    return p.parse_args()


# ── MAIN ──────────────────────────────────────────────────────────

async def main():
    args = parse_args()

    # ── 0. Username (pedido interactivamente se não foi passado via flag) ──
    if not args.username:
        while True:
            args.username = input("Username: ").strip()
            if args.username:
                break
            print("Username não pode ser vazio.")

    # ── 1. Resolver o IP anunciado ────────────────────────────────
    if args.host:
        host = args.host
        print(f"[Rede] Host manual: {host}")
    else:
        host = get_lan_ip()
        print(f"[Rede] Host auto-detectado: {host}")

    # ── 2. Resolver as portas ─────────────────────────────────────
    grpc_port = args.grpc_port if args.grpc_port else find_free_port()
    kad_port  = args.kad_port  if args.kad_port  else find_free_port()
    print(f"[Rede] gRPC:{grpc_port}  Kademlia:{kad_port}")

    # ── 3. Resolver o bootstrap ───────────────────────────────────
    bootstrap = None

    if args.bootstrap_host and args.bootstrap_kad_port:
        # Modo manual: bootstrap explícito
        bootstrap = (args.bootstrap_host, args.bootstrap_kad_port)
        print(f"[DHT] Bootstrap manual: {bootstrap[0]}:{bootstrap[1]}")

    elif args.first:
        # --first: salta descoberta, torna-se o primeiro node
        print("[DHT] Modo --first: este node é o bootstrap.")
        bootstrap = None

    else:
        # Modo automático: ouvir beacon UDP durante discovery_timeout segundos
        print(
            f"[DHT] À procura de outros jogadores na LAN "
            f"({args.discovery_timeout:.0f}s)…"
        )
        result = await discover_bootstrap(timeout=args.discovery_timeout)
        if result:
            bootstrap = result
            print(f"[DHT] Bootstrap encontrado: {result[0]}:{result[1]}")
        else:
            print("[DHT] Nenhum bootstrap encontrado → este node é o primeiro.")
            bootstrap = None

    # ── 4. Criar e arrancar o GameNode ────────────────────────────
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

    # ── 4a. Verificar disponibilidade do username ─────────────────
    # Feito após start_kademlia + start_grpc_server para podermos
    # fazer Ping aos peers e também responder a pings durante a espera.
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

    # ── 5. Se somos o bootstrap, anunciar presença na LAN ─────────
    # O beacon corre em background enquanto o jogo está activo.
    # Outros nodes que arranquem depois vão ouvir este beacon e
    # usar este node como ponto de entrada na DHT.
    beacon_task = None
    if bootstrap is None:
        print(f"[DHT] A emitir beacon na LAN (porta {_BEACON_PORT})…")
        beacon_task = asyncio.create_task(_beacon_loop(kad_port))

    # ── 6. Menu e jogo ────────────────────────────────────────────
    chosen_class = await run_menu(node.username, args.dnd_class, node)
    if chosen_class is None:
        if node.grpc_server:
            await node.grpc_server.stop(grace=2)
        if beacon_task:
            beacon_task.cancel()
        return

    node.state.local.dnd_class = chosen_class
    node.state.local.hp        = chosen_class.hp_base

    if node.lobby.current_lobby:
        import time
        from game_state import MatchPhase
        node.state.phase      = MatchPhase.ACTIVE
        node.state.start_time = time.time()
        node.state.match_id   = node.lobby.current_lobby

    await node.input_loop()

    # ── 7. Notificar peers da saída ───────────────────────────────
    import game_pb2
    # Usa node.username (pode ter mudado em relação a args.username original)
    leave_req = game_pb2.LeaveNotification(username=node.username)
    tasks = [
        node._send(p["grpc_addr"], lambda s, r: s.Leave(r), leave_req)
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