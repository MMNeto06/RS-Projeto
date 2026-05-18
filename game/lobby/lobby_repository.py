import json

from dht_keys import (
    lobby_meta_key,
    lobby_member_key,
    peer_lobby_key,
)

from lobby.lobby_models import Lobby, LobbyMember

class LobbyRepository:
    def __init__(self, kad):
        self.kad = kad

    async def save_lobby(self, lobby: Lobby):
        await self.kad.set(lobby_meta_key(lobby.code), json.dumps(lobby.to_dict()))

    async def get_lobby(self, code: str) -> Lobby | None:
        raw = await self.kad.get(lobby_meta_key(code))

        if not raw:
            return None

        try:
            data = json.loads(raw)
            return Lobby.from_dict(data)
        except Exception:
            return None

    async def announce_lobby(self, code: str):
        payload = {
            "code": code,
            "meta_key": lobby_meta_key(code)
        }

        await self.kad.set(f"lobby:announce:{code}", json.dumps(payload))

    async def save_member(self, lobby_code: str, member: LobbyMember):
        await self.kad.set(lobby_member_key(lobby_code, member.node_id), json.dumps(member.to_dict()))

    async def get_member(self,lobby_code: str, node_id: str) -> LobbyMember | None:

        raw = await self.kad.get(lobby_member_key(lobby_code, node_id))

        if not raw:
            return None

        try:
            data = json.loads(raw)
            return LobbyMember.from_dict(data)
        except Exception:
            return None

    async def remove_member(self, lobby_code: str, node_id: str):
        await self.kad.set(lobby_member_key(lobby_code, node_id), "")

    async def list_members(self, lobby_code: str, candidate_ids: set[str]) -> list[LobbyMember]:
        tasks = [self.get_member(lobby_code, nid) for nid in candidate_ids]

        results = await __import__("asyncio").gather(*tasks,return_exceptions=True)

        members = []

        for result in results:
            if isinstance(result, Exception):
                continue

            if result is not None:
                members.append(result)

        return members

    async def save_peer_lobby(self, owner_id: str, info: dict):
        await self.kad.set(peer_lobby_key(owner_id), json.dumps(info))

    async def get_peer_lobby(self, owner_id: str) -> dict | None:
        raw = await self.kad.get(peer_lobby_key(owner_id))

        if not raw:
            return None

        try:
            return json.loads(raw)
        except Exception:
            return None
    
