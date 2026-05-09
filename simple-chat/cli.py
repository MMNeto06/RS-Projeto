"""
cli.py
Input loop assíncrono e parsing de comandos do terminal.

A função run_cli() é chamada pelo ChatNode.run() e:
  1. Lê input do utilizador sem bloquear (aioconsole)
  2. Despacha comandos (/help, /members, /ping, /lobby …)
  3. Faz broadcast de mensagens normais
"""

import aioconsole


HELP_TEXT = """
Comandos disponíveis:
  /members                         → lista todos os peers conhecidos
  /ping <node_id>                  → verifica se um peer está online
  /lobby create <nome> <maxJog>    → cria um lobby
  /lobby list                      → lista lobbies abertos
  /lobby join <código>             → entra num lobby
  /lobby leave                     → sai do lobby actual
  /lobby start                     → inicia o lobby (só o dono)
  /ready                           → anuncia que estás pronto
  /unready                         → retira o estado de pronto
  /help                            → mostra esta ajuda
  /quit  /exit                     → sair
"""


async def handle_command(text: str, node) -> bool | None:
    """
    Processa um comando.

    Returns
    -------
    True   → comando tratado, continuar
    False  → não era um comando (enviar como mensagem)
    None   → sair (/quit ou /exit)
    """
    if not text.startswith("/"):
        return False

    parts = text.split()
    cmd   = parts[0].lower()

    # ── /members ──────────────────────────────────────────────────
    if cmd == "/members":
        members = await node.get_members()
        print("\n[Peers conhecidos]")
        for nid, peer in members.items():
            marker = " ← (tu)" if nid == node.node_id else ""
            print(f"  • {peer['username']} ({peer['grpc_addr']}){marker}")
        print()

    # ── /ping ─────────────────────────────────────────────────────
    elif cmd == "/ping":
        if len(parts) < 2:
            print("Uso: /ping <node_id>")
        else:
            target = parts[1]
            print(f"[~] A fazer ping a {target}…")
            online = await node.ping_peer(target)
            status = "\033[32mONLINE\033[0m" if online else "\033[31mOFFLINE\033[0m"
            print(f"[~] {target} está {status}")

    # ── /help ─────────────────────────────────────────────────────
    elif cmd == "/help":
        print(HELP_TEXT)

    # ── /quit /exit ───────────────────────────────────────────────
    elif cmd in ("/quit", "/exit"):
        return None

    # ── /ready /unready ───────────────────────────────────────────
    elif cmd == "/ready":
        await node.lobby.set_ready(True)

    elif cmd == "/unready":
        await node.lobby.set_ready(False)

    # ── /lobby ───────────────────────────────────────────────────
    elif cmd == "/lobby":
        if len(parts) < 2:
            print("Uso: /lobby create|list|join|leave|start")
            return True

        sub = parts[1].lower()

        if sub == "create":
            if len(parts) < 4:
                print("Uso: /lobby create <nome> <maxJogadores>")
            else:
                name = parts[2]
                try:
                    max_p = int(parts[3])
                    max_p = max(2, min(max_p, 16))
                except ValueError:
                    max_p = 8
                await node.lobby.create(name, max_p)

        elif sub == "list":
            await node.lobby.list_open()

        elif sub == "join":
            if len(parts) < 3:
                print("Uso: /lobby join <código>")
            else:
                await node.lobby.join(parts[2].upper())

        elif sub == "leave":
            await node.lobby.leave()

        elif sub == "start":
            await node.lobby.start()

        else:
            print(f"[!] Subcomando desconhecido: {sub}")

    # ── desconhecido ──────────────────────────────────────────────
    else:
        print(f"[!] Comando desconhecido: {cmd}. Usa /help.")

    return True


async def run_cli(node):
    """Loop principal de input. Bloqueia até o utilizador sair."""
    print(f"\n\033[1mBem-vindo, {node.username}!\033[0m")
    print("Escreve uma mensagem ou usa /help para ver os comandos.\n")

    while True:
        try:
            text = await aioconsole.ainput("> ")
            text = text.strip()
            if not text:
                continue

            result = await handle_command(text, node)

            if result is None:
                break            # /quit
            elif result is False:
                await node.broadcast(text)   # mensagem normal

        except (KeyboardInterrupt, EOFError):
            break

    print("\n[*] A sair…")
    await node.shutdown()