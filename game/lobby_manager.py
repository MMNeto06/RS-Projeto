"""
lobby_manager.py
Gere a criação, listagem, entrada e saída de lobbies via DHT.

Fluxo de validação:
  - Quando um jogador quer atacar, envia primeiro um ValidateAttack ao host.
  - O host verifica se o ataque é legítimo (dano dentro dos limites da classe).
  - Só se o host aprovar é que o ataque é enviado ao alvo
"""

import hashlib
import json
import time
import asyncio
import grpc


from dht_keys import (
    lobby_meta_key,
    lobby_member_key,
    lobby_index_key,
    peer_lobby_key,
)

import game_pb2
import game_pb2_grpc

# ── Limites de dano por classe (usados pelo host na validação) ────────────────
# Valores máximos teóricos por ataque (incluindo bónus de abilidades)
MAX_DAMAGE_BY_CLASS: dict[str, int] = {
    "fighter": 40,   # 2× (Action Surge) de 1d8+5 ~ 26, margem de segurança
    "wizard":  30,   # Magic Missile 3×(1d4+1) = 15 base; Mage Armor não faz dano
    "rogue":   35,   # 1d6+3 + 2d6 Sneak Attack
    "cleric":  20,   # Sacred Flame 1d8 + Cure Wounds não é ataque
    "ranger":  25,   # 1d8+4 + 1d6 Hunter's Mark
}
# Fallback quando a classe é desconhecida
DEFAULT_MAX_DAMAGE = 50


class LobbyManager:
    """
    Toda a lógica de lobby num só lugar.
 
    Parameters
    ----------
    kad         : KademliaNode  — instância da DHT
    node_id     : str           — hex do ID deste node
    username    : str
    peers       : dict          — referência ao dict de peers do ChatNode/GameNode
    send_fn     : coroutine     — helper para enviar gRPC (igual ao GameNode._send)
    """

    def __init__(self, kad, node_id: str, username: str, peers: dict, send_fn):
        self.kad      = kad
        self.username = username
        self.peers    = peers
        self._send    = send_fn 

        self.current_lobby: str | None = None   # código do lobby em que estamos
        self._ready = False
        self._is_host  = False 
        self._host_addr: str | None = None
        self._lobby_state: dict | None = None
        self._local_lobby_cache: dict[str, dict] = {} 

    @property
    def node_id(self) -> str:
        """
        Lê o node_id directamente do objecto Kademlia em tempo real.
        Evita o problema de ficar desactualizado ("") quando o LobbyManager
        é construído antes do kad.listen() ter corrido.
        """
        try:
            return self.kad.node.id.hex()
        except AttributeError:
            return ""


    # ── CRIAR ──────────────────────────────────────────────────────

    def _generate_code(self) -> str:
        raw = f"{self.node_id}{time.time()}"
        return hashlib.sha256(raw.encode()).hexdigest()[:6].upper()
    
    
    def _make_member_info(self, grpc_addr: str = "") -> dict:
        return {
            "node_id":   self.node_id,
            "username":  self.username,
            "ready":     False,
            "joined_at": time.time(),
            "grpc_addr": grpc_addr,
        }


    async def create(self, name: str, max_players: int, my_grpc_addr: str) -> str:
        code = self._generate_code()

        lobby_meta = {
            "code":           code,
            "name":           name,
            "owner":          self.node_id,
            "host_addr":      my_grpc_addr,
            "max_players":    max_players,
            "status":         "open",
            "created_at":     time.time(),
        }

        member_info = self._make_member_info(grpc_addr=my_grpc_addr)
 
        await self.kad.set(lobby_meta_key(code), json.dumps(lobby_meta))
        await self.kad.set(lobby_member_key(code, self.node_id),
                           json.dumps(member_info))
        await self.kad.set(lobby_index_key(code), "1")
        lobby_info = {
            "code": code, "name": name,
            "max_players": max_players,
            "current_players": 1,
            "status": "open",
            "host_addr": my_grpc_addr,
        }
        await self.kad.set(peer_lobby_key(self.node_id), json.dumps(lobby_info))
        self._local_lobby_cache[self.node_id] = lobby_info 
 
        self.current_lobby = code
        self._ready        = False
        self._is_host      = True
        self._host_addr    = my_grpc_addr
 
        print(f"[Lobby] Lobby '{name}' criado com código \033[1m{code}\033[0m")
        print(f"[Lobby] Partilha este código com os teus amigos.")

        return code


    # ── LISTAR ─────────────────────────────────────────────────────

    async def list_open(self):
        lobbies: list[dict] = []
        all_ids = set(self.peers.keys()) | {self.node_id}

        fetch_tasks = [self.kad.get(peer_lobby_key(nid)) for nid in all_ids]
        results = await asyncio.gather(*fetch_tasks, return_exceptions=True)

        for nid, raw in zip(all_ids, results):
            # Se a DHT falhou, tenta a cache local
            if not raw or isinstance(raw, Exception):
                raw = json.dumps(self._local_lobby_cache[nid]) \
                    if nid in self._local_lobby_cache else None
            if not raw:
                continue
            try:
                lobby = json.loads(raw)
                if lobby.get("status") == "open":
                    lobbies.append(lobby)
            except json.JSONDecodeError:
                pass
 
        # Remover duplicados pelo código (pode haver vários peers no mesmo lobby)
        seen: set[str] = set()
        unique: list[dict] = []
        for l in lobbies:
            if l["code"] not in seen:
                seen.add(l["code"])
                unique.append(l)
 
        if not unique:
            print("[Lobby] Nenhum lobby disponível de momento.")
            return
 
        print("\n┌─ Lobbies abertos " + "─" * 40)
        for l in unique:
            players_str = f"{l.get('current_players', '?')}/{l['max_players']}"
            print(f"│  [{l['code']}] \033[1m{l['name']}\033[0m — {players_str} jogadores")
        print("└" + "─" * 50)


    # ── ENTRAR ─────────────────────────────────────────────────────

    async def _count_members(self, code: str) -> int:
        return len(await self._fetch_members(code))

    async def _update_player_count(self, code: str, meta: dict, count: int):
        """Actualiza o peer_lobby_key do owner com o novo contador."""
        owner_id = meta.get("owner")
        if not owner_id:
            return
        raw = await self.kad.get(peer_lobby_key(owner_id))
        if raw:
            try:
                info = json.loads(raw)
                info["current_players"] = count
                await self.kad.set(peer_lobby_key(owner_id), json.dumps(info))
            except json.JSONDecodeError:
                pass


    async def join(self, code: str, my_grpc_addr: str):
        """Entra num lobby existente."""
        if self.current_lobby:
            print(f"[Lobby] Já estás no lobby {self.current_lobby}. Sai primeiro com /lobby leave.")
            return
 
        raw = await self.kad.get(lobby_meta_key(code))
        if not raw:
            print(f"[Lobby] Lobby '{code}' não encontrado na DHT.")
            return
 
        try:
            meta = json.loads(raw)
        except json.JSONDecodeError:
            print("[Lobby] Erro ao ler metadados do lobby.")
            return
 
        if meta.get("status") != "open":
            print(f"[Lobby] O lobby '{code}' já não está aberto ({meta.get('status')}).")
            return
 
        # Verificar número máximo de jogadores
        member_count = await self._count_members(code)
        if member_count >= meta.get("max_players", 99):
            print(f"[Lobby] Lobby '{code}' está cheio ({member_count}/{meta['max_players']}).")
            return
 
        member_info = self._make_member_info(grpc_addr=my_grpc_addr)
        await self.kad.set(lobby_member_key(code, self.node_id),
                           json.dumps(member_info))
 
        self.current_lobby = code
        self._ready        = False
        self._is_host      = False
        self._host_addr    = meta.get("host_addr")  
        self._lobby_state  = meta
 
        print(f"[Lobby] Entraste no lobby \033[1m{code}\033[0m — {meta['name']}")
        print(f"[Lobby] Usa /ready quando estiveres pronto.")
 
        # Avisar os outros membros via broadcast DHT (actualiza contador)
        await self._update_player_count(code, meta, member_count + 1)


    # ── SAIR ───────────────────────────────────────────────────────

    async def _notify_peers(self, lobby_code: str, notify_type: int):
        """
        Envia LobbyNotification a todos os peers conhecidos via gRPC.
        Usa self._send (guardado no __init__) em vez de abrir canais directamente.
        Os peers recebem a notificação em GameServicer.LobbyNotify e mostram-na
        no terminal imediatamente, sem precisar de polling à DHT.
        """
        notification = game_pb2.LobbyNotification()
        notification.type       = notify_type
        notification.lobby_code = lobby_code
        notification.username   = self.username
        
        tasks = [
            self._send(p["grpc_addr"], lambda s, r: s.LobbyNotify(r), notification)
            for p in self.peers.values()
            if p.get("grpc_addr")
        ]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


    async def leave(self):
        if not self.current_lobby:
            print("[Lobby] Não estás em nenhum lobby.")
            return
 
        code = self.current_lobby

        await self._notify_peers(code, game_pb2.LobbyNotification.PLAYER_LEFT)
 
        # Tombstone: sobrescrever com string vazia (Kademlia não tem delete real)
        await self.kad.set(lobby_member_key(code, self.node_id), "")
 
        # Se éramos o host, tentar transferir ownership (não trivial em P2P puro;
        # aqui apenas marcamos o lobby como "abandoned")
        if self._is_host:
            raw = await self.kad.get(lobby_meta_key(code))
            if raw:
                try:
                    meta = json.loads(raw)
                    meta["status"] = "abandoned"
                    await self.kad.set(lobby_meta_key(code), json.dumps(meta))
                    print("[Lobby] Eras o host — o lobby foi encerrado.")
                except json.JSONDecodeError:
                    pass
        else:
            print(f"[Lobby] Saíste do lobby {code}.")
 
        self.current_lobby = None
        self._ready        = False
        self._is_host      = False
        self._host_addr    = None


    # ── PRONTO / NÃO PRONTO ───────────────────────────────────────

    async def set_ready(self, ready: bool):
        if not self.current_lobby:
            print("[Lobby] Não estás em nenhum lobby.")
            return
 
        self._ready = ready
        raw = await self.kad.get(
            lobby_member_key(self.current_lobby, self.node_id)
        )
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

        notify_type = (
            game_pb2.LobbyNotification.PLAYER_READY
            if ready
            else game_pb2.LobbyNotification.PLAYER_UNREADY
        )
        await self._notify_peers(self.current_lobby, notify_type)

 
        # Mostrar snapshot do estado actual do lobby
        await self.status()

    # ── LOBBY STATUS ──────────────────────────────────────


    async def _fetch_members(self, code: str) -> list[dict]:
        members: list[dict] = []
        candidate_ids = set(self.peers.keys()) | {self.node_id}
        tasks = [self.kad.get(lobby_member_key(code, nid)) for nid in candidate_ids]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for raw in results:
            if not raw or isinstance(raw, Exception):
                continue
            try:
                m = json.loads(raw)
                if m:
                    members.append(m)
            except (json.JSONDecodeError, TypeError):
                pass
        
        return members
    
    def _get_owner_id(self) -> str | None:
        """Devolve o node_id do owner. Se formos o host, sabemos logo."""
        if self._is_host:
            return self.node_id
        if self._lobby_state:
            return self._lobby_state.get("owner")
        return None

    
    async def status(self):
        """Mostra quem está no lobby e o estado de ready de cada um."""
        if not self.current_lobby:
            print("[Lobby] Não estás em nenhum lobby.")
            return
 
        members = await self._fetch_members(self.current_lobby)
        if not members:
            print("[Lobby] Lobby vazio ou não encontrado.")
            return
 
        raw_meta = await self.kad.get(lobby_meta_key(self.current_lobby))
        name = "?"
        if raw_meta:
            try:
                name = json.loads(raw_meta).get("name", "?")
            except json.JSONDecodeError:
                pass
 
        print(f"\n┌─ Lobby [{self.current_lobby}] {name} " + "─" * 30)
        all_ready = True
        for m in members:
            ready_icon = "\033[32m✔\033[0m" if m.get("ready") else "\033[31m✘\033[0m"
            host_tag   = " 👑" if m.get("node_id") == self._get_owner_id() else ""
            print(f"│  {ready_icon} {m['username']}{host_tag}")
            if not m.get("ready"):
                all_ready = False
        print("└" + "─" * 50)
 
        if all_ready and len(members) > 1:
            print("[Lobby] Todos prontos! O host pode usar /lobby start.")


    # ── INICIAR (só o owner) ──────────────────────────────────────

    async def start(self) -> bool:
        """
        Inicia o lobby. Só o host pode chamar este método.
        Devolve True se o arranque foi bem-sucedido (o chamador deve lançar o GameNode).
        """
        if not self.current_lobby:
            print("[Lobby] Não estás em nenhum lobby.")
            return False
 
        if not self._is_host:
            print("[Lobby] Só o dono do lobby pode iniciá-lo.")
            return False
 
        raw = await self.kad.get(lobby_meta_key(self.current_lobby))
        if not raw:
            print("[Lobby] Metadados do lobby não encontrados.")
            return False
 
        try:
            meta = json.loads(raw)
        except json.JSONDecodeError:
            print("[Lobby] Erro ao ler metadados.")
            return False
 
        # Verificar se todos estão prontos
        members = await self._fetch_members(self.current_lobby)
        not_ready = [m["username"] for m in members
                     if not m.get("ready") and m["node_id"] != self.node_id]
        if not_ready:
            print(f"[Lobby] Ainda não estão prontos: {', '.join(not_ready)}")
            print("[Lobby] Usa /lobby status para ver o estado de cada jogador.")
            return False
 
        # Marcar como started na DHT
        meta["status"] = "started"
        await self.kad.set(lobby_meta_key(self.current_lobby), json.dumps(meta))
 
        print(f"\033[1m[Lobby] A iniciar o jogo! Boa sorte a todos.\033[0m")
        return True

    def validate_attack(self, attacker_class: str, damage: int,
                        weapon: str, is_spell: bool) -> tuple[bool, str]:
        """
        Chamado no host para validar um ataque antes de este ser aplicado.
 
        Returns
        -------
        (approved: bool, reason: str)
        """
        class_lower  = attacker_class.lower()
        max_dmg      = MAX_DAMAGE_BY_CLASS.get(class_lower, DEFAULT_MAX_DAMAGE)
 
        if damage < 0:
            return False, f"Dano negativo não permitido ({damage})."
 
        if damage > max_dmg:
            return False, (
                f"Dano {damage} excede o máximo permitido para {attacker_class} "
                f"({max_dmg}). Possível trapaça!"
            )
 
        # Aqui podem ser adicionadas validações adicionais:
        # - cooldowns (se o host guardar estado dos timers)
        # - weapons válidas por classe
        # - etc. -> MIGUEL pls help me
 
        return True, "ok"
    
    async def request_validation(self, attack_req: "game_pb2.AttackRequest") -> bool:
        """
        Enviado pelo atacante ao host antes de mandar o ataque ao alvo.
        Devolve True se o host aprovou.
        """
        if not self._host_addr:
            print("[Lobby] Host desconhecido — ataque bloqueado por segurança.")
            return False
 
        try:
            val_req = game_pb2.ValidateAttackRequest(
                attacker=attack_req.attacker,
                attacker_class=attack_req.attacker_class,
                weapon=attack_req.weapon,
                damage=attack_req.base_damage_roll,
                is_spell=attack_req.is_spell,
                target=attack_req.target,
            )
            resp = await self._send(
                self._host_addr,
                lambda s, r: s.ValidateAttack(r),
                val_req,
                timeout=2.0,
            )
            if resp is None:
                print("[Lobby] Host não respondeu à validação — ataque bloqueado.")
                return False
            if not resp.approved:
                print(f"[Lobby] \033[31mAtaque rejeitado pelo host:\033[0m {resp.reason}")
            return resp.approved
        except Exception as e:
            print(f"[Lobby] Erro de validação: {e}")
            return False
        
    async def sync_loop(self, interval: float = 5.0):
        """
        Corre em background: actualiza o estado do lobby na DHT
        e detecta se o host mudou o status para 'started'.
 
        Devolve o match_id quando o jogo arrancar, para o chamador
        poder fazer a transição lobby → GameNode.
        """
        while self.current_lobby:
            await asyncio.sleep(interval)
            if not self.current_lobby:
                break
            try:
                raw = await self.kad.get(lobby_meta_key(self.current_lobby))
                if not raw:
                    continue
                meta = json.loads(raw)
                if meta.get("status") == "started":
                    print("\n\033[1m[Lobby] O host iniciou o jogo! A entrar…\033[0m")
                    return self.current_lobby   # usado como match_id
                if meta.get("status") == "abandoned":
                    print("\n[Lobby] O host saiu — lobby encerrado.")
                    self.current_lobby = None
                    self._is_host      = False
                    self._host_addr    = None
                    return None
            except Exception:
                pass
        return None

 


