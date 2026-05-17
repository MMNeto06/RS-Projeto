"""
command_dispatcher.py
Dispatcher de comandos do jogador durante a partida.

Desacoplado do GameNode: recebe o node como dependência mas
concentra aqui toda a lógica de parsing e routing de comandos,
deixando o input_loop com apenas o ciclo de leitura.

Uso:
    dispatcher = CommandDispatcher(node)
    running = await dispatcher.dispatch(text)   # False → utilizador quer sair
"""

from __future__ import annotations
from typing import TYPE_CHECKING
from game_state import MatchPhase

if TYPE_CHECKING:
    from game_node import GameNode


_MOVES: dict[str, tuple[int, int]] = {
    "w": (0, -1),
    "s": (0,  1),
    "a": (-1, 0),
    "d": (1,  0),
}


class CommandDispatcher:
    """
    Recebe uma linha de texto e executa o comando correspondente no node.

    dispatch(text) → bool
        True  → continuar o loop
        False → sair (comando 'q')
    """

    def __init__(self, node: "GameNode"):
        self._node = node

    async def dispatch(self, text: str) -> bool:
        """Processa uma linha de input. Devolve False se o utilizador quer sair."""
        text = text.strip()
        if not text:
            return True

        parts = text.split()
        cmd   = parts[0].lower()

        # ── Sair ──────────────────────────────────────────────────
        if cmd == "q":
            return False

        # ── Ataque básico ─────────────────────────────────────────
        elif cmd == "at":
            if len(parts) < 2:
                self._node.state.log_event("[!] Uso: at <username>")
            else:
                await self._node._combat.send_attack(parts[1])
                await self._node.broadcast_state()

        # ── Movimento ─────────────────────────────────────────────
        elif cmd in _MOVES:
            await self._node.move(*_MOVES[cmd])

        # ── Abilidades (1, 2, 3…) ─────────────────────────────────
        elif cmd.isdigit():
            idx    = int(cmd) - 1
            target = parts[1] if len(parts) > 1 else None
            used   = await self._node._combat.use_ability(idx, target)
            if used:
                await self._node.broadcast_state()

        # ── Scoreboard ────────────────────────────────────────────
        elif cmd == "sc":
            self._node.state.log_event("── Scoreboard ──")
            for uname, k, d in self._node.state.scoreboard():
                self._node.state.log_event(f"  {uname}: {k}K / {d}D")

        # ── Lobby ─────────────────────────────────────────────────
        elif cmd == "lobby":
            await self._handle_lobby(parts)

        elif cmd == "ready":
            await self._node.lobby.set_ready(True)

        elif cmd == "unready":
            await self._node.lobby.set_ready(False)

        # ── Desconhecido ──────────────────────────────────────────
        else:
            self._node.state.log_event(
                "[!] Comandos: at <user> | w/a/s/d | 1/2/3 [user] | sc | q"
            )

        return True

    # ── LOBBY SUB-COMANDOS ────────────────────────────────────────

    async def _handle_lobby(self, parts: list[str]) -> None:
        node = self._node

        if len(parts) < 2:
            self._print_lobby_help()
            return

        sub = parts[1].lower()

        if sub == "create":
            if len(parts) < 4:
                print("Uso: lobby create <nome> <maxJogadores>")
                return
            name = parts[2]
            try:
                max_players = int(parts[3])
            except ValueError:
                print("[!] maxJogadores deve ser um número inteiro.")
                return
            my_addr = f"{node.host}:{node.grpc_port}"
            await node.lobby.create(name, max_players, my_addr)

        elif sub == "list":
            await node.lobby.list_open()

        elif sub == "join":
            if len(parts) < 3:
                print("Uso: lobby join <Código>")
                return
            my_addr = f"{node.host}:{node.grpc_port}"
            await node.lobby.join(parts[2].upper(), my_addr)

        elif sub == "leave":
            await node.lobby.leave()

        elif sub == "start":
            ok = await node.lobby.start()
            if ok:
                node.state.phase = MatchPhase.ACTIVE
                node.state.log_event("🎮 Jogo iniciado pelo host!")

        elif sub == "status":
            await node.lobby.status()

        else:
            self._print_lobby_help()

    # ── AJUDA DO LOBBY ────────────────────────────────────────────

    @staticmethod
    def _print_lobby_help() -> None:
        print("""
            [Lobby] Comandos disponíveis:
            lobby create <nome> <maxJogadores>  → Criar um novo lobby
            lobby list                          → Listar lobbies abertos
            lobby join <Código>                 → Entrar num lobby
            lobby leave                         → Sair do lobby actual
            lobby start                         → Iniciar o jogo (só o host)
            lobby status                        → Ver quem está pronto
            ready                               → Marcar-se como pronto
            unready                             → Retirar estado de pronto
        """)