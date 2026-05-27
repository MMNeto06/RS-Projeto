import hashlib
import json
import time
import asyncio
import random

import game_pb2

from dnd_classes import max_damage_for_class

from lobby.lobby_repository import LobbyRepository
from lobby.lobby_models import Lobby, LobbyMember, LobbySession
from lobby.lobby_enums import LobbyStatus

class LobbyManager:
    def __init__(self, kad, node_id: str, username: str, peers: dict, send_fn, host, kad_port):
        self.kad      = kad
        self.username = username
        self.peers    = peers
        self._send    = send_fn 
        self.send_rpc = send_fn
        self.host = host
        self.kad_port = kad_port    
        self.session: LobbySession | None = None
        self.known_lobbies: dict[str, tuple[Lobby, float]] = {}
        self.repo = LobbyRepository(kad)
        self._local_member: "LobbyMember | None" = None

    def _dbg(self, msg: str):
        role = "host" if self.is_host else "client"
        print(f"[DBG][Lobby:{self.username}][{role}] {msg}")

    @property
    def node_id(self) -> str:
        try:
            return self.kad.node.id.hex()
        except AttributeError:
            return ""

    def _generate_code(self) -> str:
        raw = f"{self.node_id}{time.time()}"
        return hashlib.sha256(raw.encode()).hexdigest()[:6].upper()
    
    def _make_member_info(self, grpc_addr: str = "") -> LobbyMember: 
        return LobbyMember(node_id=self.node_id, username=self.username, grpc_addr=grpc_addr, ready=False)
    
    async def _gossip_lobby(self, lobby: Lobby):
        if not self.peers:
            return
        
        sample = random.sample(
            list(self.peers.values()),
            k=min(3, len(self.peers))   # não spamma a rede
        )

        msg = json.dumps(lobby.to_dict())

        tasks = []
        snapshot = list(sample) #to avoid race conditions
        for p in snapshot:
            if not p.get("grpc_addr"):
                continue

            tasks.append(
                self.send_rpc(p["grpc_addr"], 'LobbyGossip', game_pb2.LobbyGossipMessage(payload=msg))
            )

        if tasks:
            self._dbg(f"gossip lobby={lobby.code} targets={len(tasks)}")
            await asyncio.gather(*tasks, return_exceptions=True)


    async def create(self, name: str, max_players: int, my_grpc_addr: str) -> str:
        self._dbg(f"create name={name} max={max_players} my_addr={my_grpc_addr}")
        code = self._generate_code()

        lobby = Lobby(
            code=code,
            name=name,
            owner=self.node_id,
            host_addr=my_grpc_addr,
            max_players=max_players,
        )

        member = self._make_member_info(grpc_addr=my_grpc_addr)

        await self.repo.save_lobby(lobby)
        await self.repo.save_member(code, member)
        await self.repo.announce_lobby(code)
        self._local_member = member 

        lobby_info = {
            "code": code, "name": name,
            "max_players": max_players,
            "current_players": 1,
            "status": "open",
            "host_addr": my_grpc_addr,
        }

        await self.repo.save_peer_lobby(self.node_id, lobby_info)
 
        self.session = LobbySession(
            code = code, 
            is_host = True, 
            host_addr = my_grpc_addr, 
            ready = False, 
            lobby_state = lobby
        )

        lobby.source = self.node_id
        await self._gossip_lobby(lobby)
 
        print(f"[Lobby] Lobby '{name}' criado com código \033[1m{code}\033[0m")
        print(f"[Lobby] Partilha este código com os teus amigos.")
        print(f"[Lobby] Juntando ao lobby...")
        self._dbg(f"create done code={code} peers={len(self.peers)}")

        return code


    async def _probe_lobby_codes(self, candidates: set[str]) -> set[str]:
        discovered = set()

        for nid in candidates:
            info = await self.repo.get_peer_lobby(nid)
            if info:
                code = info.get("code")
                if code:
                    discovered.add(code)

        return discovered

    async def list_open(self):
        lobbies: dict[str, Lobby] = {}

        # 1. gossip cache (rápido)
        for code, (lobby, ts) in self.known_lobbies.items():
            if time.time() - ts > 30:
                continue
            if lobby.status == LobbyStatus.OPEN:
                lobbies[code] = lobby

        dht_codes = await self._probe_lobby_codes(set(self.peers.keys()))
        pending_codes = set()
        for code in dht_codes:
            if code not in lobbies:
                pending_codes.add(code)

        for code in pending_codes:
            lobby = await self.repo.get_lobby(code)
            if not lobby:
                lobbies.pop(code, None)
                continue

            if lobby.status != LobbyStatus.OPEN:
                lobbies.pop(code, None)
            else:
                lobbies[code] = lobby


        if not lobbies:
            print("[Lobby] Nenhum lobby disponível.")
            return

        print("\n┌─ Lobbies abertos ─" + "─" * 40)
        for l in lobbies.values():
            print(f"│ [{l.code}] {l.name} — {l.max_players}")
        print("└" + "─" * 50)


    async def _count_members(self, code: str) -> int:
        return len(await self._fetch_members(code))

    async def _update_player_count(self, lobby: Lobby, count: int):
        owner_id = lobby.owner
        
        if not owner_id:
            return
        
        info = await self.repo.get_peer_lobby(owner_id)

        if info:
            info["current_players"] = count
            await self.repo.save_peer_lobby(owner_id, info)

    async def join(self, code: str, my_grpc_addr: str):
        self._dbg(f"join code={code} my_addr={my_grpc_addr}")
        if self.session:
            print(f"[Lobby] Já estás no lobby {self.session.code}. Sai primeiro com /lobby leave.")
            return
 
        lobby = await self.repo.get_lobby(code)
        if not lobby:
            print(f"[Lobby] Lobby '{code}' não encontrado na DHT.")
            return
 
        if lobby.status != LobbyStatus.OPEN:
            print(f"[Lobby] O lobby '{code}' já não está aberto ({lobby.status}).")
            return
 
        member_count = await self._count_members(code)
        if member_count >= lobby.max_players:
            print(f"[Lobby] Lobby '{code}' está cheio ({member_count}/{lobby.max_players}).")
            return
 
        member_info = self._make_member_info(grpc_addr=my_grpc_addr)
        await self.repo.save_member(code, member_info)
        self._local_member = member_info  # cache local
        await self._notify_peers(code, game_pb2.LobbyNotification.PLAYER_JOINED)
 
        self.session = LobbySession(
            code=code,
            is_host=False,
            host_addr=lobby.host_addr,
            ready=False,
            lobby_state=lobby
        )
 
        print(f"[Lobby] Entraste no lobby \033[1m{code}\033[0m — {lobby.name}")
        print(f"[Lobby] Usa /ready quando estiveres pronto.")
        self._dbg(
            f"join done code={code} host_addr={lobby.host_addr} members_before={member_count}"
        )
 
        await self._update_player_count(lobby, member_count + 1)


    async def _notify_peers(self, lobby_code: str, notify_type: int):
        notification = game_pb2.LobbyNotification()
        notification.type       = notify_type
        notification.lobby_code = lobby_code
        notification.username   = self.username
        
        tasks = [
            self.send_rpc(p["grpc_addr"], 'LobbyNotify', notification)
            for p in self.peers.values()
            if p.get("grpc_addr")
        ]

        host_addr = self.host_addr if self.session else ""
        if host_addr and all(p.get("grpc_addr") != host_addr for p in self.peers.values()):
            tasks.append(self.send_rpc(host_addr, 'LobbyNotify', notification))

        self._dbg(
            f"notify type={notify_type} code={lobby_code} peers={len(self.peers)} targets={len(tasks)} "
            f"host_addr={host_addr or '-'}"
        )

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


    async def leave(self):
        if not self.session:
            print("[Lobby] Não estás em nenhum lobby.")
            return
 
        code = self.session.code

        await self._notify_peers(code, game_pb2.LobbyNotification.PLAYER_LEFT) 
        await self.repo.remove_member(code, self.node_id)

        if self.is_host:
            lobby = await self.repo.get_lobby(code)
            if lobby:
                lobby.status = LobbyStatus.ABANDONED
                lobby.version = time.time()
                await self.repo.save_lobby(lobby)
                await self._notify_peers(code, game_pb2.LobbyNotification.LOBBY_CLOSED)
                print("[Lobby] Eras o host — o lobby foi encerrado.")
                
        else:
            print(f"[Lobby] Saíste do lobby {code}.")

        if self.session.lobby_state:
            count = max(0, await self._count_members(code) - 1)
            await self._update_player_count(self.session.lobby_state, count)

        self.session.reset()


    async def set_ready(self, ready: bool):
        if not self.session:
            print("[Lobby] Não estás em nenhum lobby.")
            return
 
        self.session.ready = ready
        member = await self.repo.get_member(self.session.code, self.node_id)

        if member:
            member.ready = ready
            await self.repo.save_member(self.session.code, member)

        if self._local_member:
            self._local_member.ready = ready
 
        status = "\033[32mPRONTO\033[0m" if ready else "\033[33mNÃO PRONTO\033[0m"
        print(f"[Lobby] Estás {status}")

        notify_type = (game_pb2.LobbyNotification.PLAYER_READY if ready
            else game_pb2.LobbyNotification.PLAYER_UNREADY)
        
        await self._notify_peers(self.session.code, notify_type)


    async def _fetch_members(self, code: str) -> list[LobbyMember]:
        candidate_ids = set(self.peers.keys()) | {self.node_id}
        dht_members = await self.repo.list_members(code, candidate_ids)

        if self._local_member and self._local_member.node_id == self.node_id:
            dht_ids = {m.node_id for m in dht_members}
            if self.node_id not in dht_ids:
                dht_members = [self._local_member] + dht_members

        self._dbg(
            f"fetch_members code={code} candidates={len(candidate_ids)} found={len(dht_members)}"
        )

        return dht_members

    
    def _get_owner_id(self) -> str | None:
        if not self.session: 
            return None

        if self.is_host:
            return self.node_id
        
        if self.session.lobby_state:
            return self.session.lobby_state.owner
        
        return None

    
    async def status(self):
        if not self.session:
            print("[Lobby] Não estás em nenhum lobby.")
            return

        self._dbg(f"status code={self.session.code}")
 
        members = await self._fetch_members(self.session.code)
        if not members:
            print("[Lobby] Lobby vazio ou não encontrado.")
            return
 
        lobby = await self.repo.get_lobby(self.session.code)

        if not lobby: 
            print("[Lobby] Metadados do lobby não encontrados.")
            return 
 
        print(f"\n┌─ Lobby [{self.session.code}] {lobby.name} " + "─" * 30)

        all_ready = True

        for m in members:
            ready_icon = "\033[32m✔\033[0m" if m.ready else "\033[31m✘\033[0m"
            host_tag   = " 👑" if m.node_id == self._get_owner_id() else ""

            print(f"│  {ready_icon} {m.username}{host_tag}")

            if not m.ready:
                all_ready = False

        print("└" + "─" * 50)
 
        if all_ready and len(members) > 1:
            print("[Lobby] Todos prontos! O host pode usar /lobby start.")
        
    async def _notify_and_wait_start(self, timeout: float = 5.0):
        notification = game_pb2.LobbyNotification()
        notification.type       = game_pb2.LobbyNotification.LOBBY_STARTED
        notification.lobby_code = self.session.code
        notification.username   = self.username

        peers_with_addr = [
            p for p in self.peers.values() if p.get("grpc_addr")
        ]

        if not peers_with_addr:
            self._dbg("notify_start no peers_with_addr")
            return 

        tasks = [
            self.send_rpc(p["grpc_addr"], 'LobbyNotify', notification)
            for p in peers_with_addr
        ]

        results = await asyncio.gather(*tasks, return_exceptions=True)

        errors = [r for r in results if isinstance(r, Exception)]
        if errors:
            print(f"[Lobby] {len(errors)} peer(s) não responderam — a avançar na mesma.")
        self._dbg(f"notify_start sent={len(tasks)} errors={len(errors)}")

    async def start(self) -> bool:
        if not self.session:
            print("[Lobby] Não estás em nenhum lobby.")
            return False
 
        if not self.is_host:
            print("[Lobby] Só o dono do lobby pode iniciá-lo.")
            return False
 
        lobby = await self.repo.get_lobby(self.session.code)
        if lobby is None:
            lobby = self.session.lobby_state
            if lobby:
                await self.repo.save_lobby(lobby)
        if not lobby:
            print("[Lobby] Metadados do lobby não encontrados.")
            return False
 
        members = await self._fetch_members(self.session.code)
        self._dbg(f"start requested members={len(members)}")
        not_ready = [m.username for m in members
                     if not m.ready and m.node_id != self.node_id]
                     
        if not_ready:
            print(f"[Lobby] Ainda não estão prontos: {', '.join(not_ready)}")
            print("[Lobby] Usa /lobby status para ver o estado de cada jogador.")
            return False
 
        lobby.status = LobbyStatus.STARTED
        lobby.version = time.time()
        lobby.source = self.node_id

        await self.repo.save_lobby(lobby)
        await self._gossip_lobby(lobby)
        await self._notify_and_wait_start()

        print(f"\033[1m[Lobby] A iniciar o jogo! Boa sorte a todos.\033[0m")

        return True

    def validate_attack(self, attacker_class: str, damage: int,
                        weapon: str, is_spell: bool) -> tuple[bool, str]:

        max_dmg = max_damage_for_class(attacker_class)

        if damage < 0:
            return False, f"Dano negativo não permitido ({damage})."

        if damage > max_dmg:
            return False, (
                f"Dano {damage} excede o máximo permitido para {attacker_class} "
                f"({max_dmg}). Possível trapaça!"
            )

        return True, "ok"
    
    async def request_validation(self, attack_req: "game_pb2.AttackRequest") -> bool:
        if not self.session or not self.host_addr:
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
            resp = await self.send_rpc(
                self.host_addr,
                'ValidateAttack',
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
        while self.session:
            await asyncio.sleep(interval)
            if not self.session:
                break

            try:
                await self.refresh_lobby_state()
                lobby = await self.repo.get_lobby(self.session.code)

                if not lobby:
                    continue

                if lobby.status == LobbyStatus.STARTED:
                    print("\n\033[1m[Lobby] O host iniciou o jogo! A entrar…\033[0m")
                    self.session.reset()
                    return lobby.code
                
                if lobby.status == LobbyStatus.ABANDONED:
                    print("\n[Lobby] O host saiu — lobby encerrado.")
                    self.session.reset()
                    return None
                
            except Exception:
                pass

        return None
    
    async def refresh_lobby_state(self):
        if not self.session:
            return

        lobby = await self.repo.get_lobby(self.session.code)
        
        if lobby:
            self.session.lobby_state = lobby
    @property
    def is_host(self) -> bool:
        return bool(self.session and self.session.is_host)
    
    @property
    def host_addr(self) -> str:
        return self.session.host_addr if self.session else ""

    def peer_is_host(self, peer) -> bool:
        if not isinstance(peer, dict):
            return False
        owner = self._get_owner_id()
        if not owner:
            return False
        return peer.get("node_id") == owner or peer.get("grpc_addr") == self.host_addr