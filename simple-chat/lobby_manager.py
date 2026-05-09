"""
lobby_manager.py
Gere a criação, listagem, entrada e saída de lobbies via DHT.

LobbyManager é instanciado pelo ChatNode e recebe uma referência
ao kad (Kademlia) e ao dict de peers.
"""

import hashlib
import json
import time

from dht_keys import (
    lobby_meta_key,
    lobby_member_key,
    lobby_index_key,
    peer_lobby_key,
)


class LobbyManager:
    """
    Toda a lógica de lobby num só lugar.

    Parameters
    ----------
    kad        : KademliaNode  — instância da DHT
    node_id    : str           — hex do ID deste node
    username   : str
    peers      : dict          — referência ao dict de peers do ChatNode
    """

    def __init__(self, kad, node_id: str, username: str, peers: dict):
        self.kad      = kad
        self.node_id  = node_id
        self.username = username
        self.peers    = peers

        self.current_lobby: str | None = None   # código do lobby em que estamos
        self._ready = False

    # ── CRIAR ──────────────────────────────────────────────────────

    def _generate_code(self) -> str:
        raw = f"{self.node_id}{time.time()}"
        return hashlib.sha256(raw.encode()).hexdigest()[:6].upper()

    async def create(self, name: str, max_players: int) -> str:
        """Cria um novo lobby e anuncia-o na DHT. Devolve o código."""
        code = self._generate_code()

        lobby_meta = {
            "code":           code,
            "name":           name,
            "owner":          self.node_id,
            "max_players":    max_players,
            "status":         "open",
            "created_at":     time.time(),
        }
        member_info = {
            "node_id":   self.node_id,
            "username":  self.username,
            "ready":     False,
            "joined_at": time.time(),
        }

        await self.kad.set(lobby_meta_key(code), json.dumps(lobby_meta))
        await self.kad.set(lobby_member_key(code, self.node_id), json.dumps(member_info))
        await self.kad.set(lobby_index_key(code), "1")
        # Anuncia no registo pessoal do peer (para listagem)
        await self.kad.set(peer_lobby_key(self.node_id), json.dumps({
            "code": code, "name": name,
            "max_players": max_players, "current_players": 1, "status": "open",
        }))

        self.current_lobby = code
        self._ready = False
        print(f"[Lobby] Lobby '{name}' criado com código \033[1m{code}\033[0m")
        return code

    # ── LISTAR ─────────────────────────────────────────────────────

    async def list_open(self):
        """Imprime os lobbies abertos conhecidos pelos peers actuais."""
        lobbies = []
        for node_id in self.peers:
            raw = await self.kad.get(peer_lobby_key(node_id))
            if raw:
                try:
                    lobby = json.loads(raw)
                    if lobby.get("status") == "open":
                        lobbies.append(lobby)
                except json.JSONDecodeError:
                    pass

        if not lobbies:
            print("[Lobby] Nenhum lobby disponível de momento.")
            return

        print("\n[Lobbies abertos]")
        for l in lobbies:
            print(f"  • [{l['code']}] {l['name']} — "
                  f"{l['current_players']}/{l['max_players']} jogadores")
        print()

    # ── ENTRAR ─────────────────────────────────────────────────────

    async def join(self, code: str):
        """Entra num lobby existente."""
        raw = await self.kad.get(lobby_meta_key(code))
        if not raw:
            print(f"[Lobby] Lobby '{code}' não encontrado na DHT.")
            return

        meta = json.loads(raw)
        if meta.get("status") != "open":
            print(f"[Lobby] O lobby '{code}' já não está aberto.")
            return

        member_info = {
            "node_id":   self.node_id,
            "username":  self.username,
            "ready":     False,
            "joined_at": time.time(),
        }
        await self.kad.set(lobby_member_key(code, self.node_id), json.dumps(member_info))
        self.current_lobby = code
        self._ready = False
        print(f"[Lobby] Entraste no lobby \033[1m{code}\033[0m ({meta['name']})")

    # ── SAIR ───────────────────────────────────────────────────────

    async def leave(self):
        if not self.current_lobby:
            print("[Lobby] Não estás em nenhum lobby.")
            return
        # Apagar o registo de membro (simula saída — Kademlia não tem delete real,
        # mas sobrescrever com string vazia serve de tombstone)
        await self.kad.set(
            lobby_member_key(self.current_lobby, self.node_id), ""
        )
        print(f"[Lobby] Saíste do lobby {self.current_lobby}")
        self.current_lobby = None
        self._ready = False

    # ── PRONTO / NÃO PRONTO ───────────────────────────────────────

    async def set_ready(self, ready: bool):
        if not self.current_lobby:
            print("[Lobby] Não estás em nenhum lobby.")
            return
        self._ready = ready
        # Actualizar registo de membro na DHT
        raw = await self.kad.get(lobby_member_key(self.current_lobby, self.node_id))
        if raw:
            try:
                info = json.loads(raw)
                info["ready"] = ready
                await self.kad.set(
                    lobby_member_key(self.current_lobby, self.node_id),
                    json.dumps(info),
                )
            except json.JSONDecodeError:
                pass
        status = "\033[32mPRONTO\033[0m" if ready else "\033[33mNÃO PRONTO\033[0m"
        print(f"[Lobby] Estás {status}")

    # ── INICIAR (só o owner) ──────────────────────────────────────

    async def start(self):
        if not self.current_lobby:
            print("[Lobby] Não estás em nenhum lobby.")
            return
        raw = await self.kad.get(lobby_meta_key(self.current_lobby))
        if not raw:
            print("[Lobby] Metadados do lobby não encontrados.")
            return
        meta = json.loads(raw)
        if meta.get("owner") != self.node_id:
            print("[Lobby] Só o dono do lobby pode iniciá-lo.")
            return
        meta["status"] = "started"
        await self.kad.set(lobby_meta_key(self.current_lobby), json.dumps(meta))
        print(f"[Lobby] Lobby \033[1m{self.current_lobby}\033[0m iniciado! Boa sorte a todos.")