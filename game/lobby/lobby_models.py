from dataclasses import dataclass, asdict
import time

from lobby.lobby_enums import LobbyStatus

@dataclass
class Lobby:
    code: str
    name: str
    owner: str
    host_addr: str

    max_players: int

    status: LobbyStatus = LobbyStatus.OPEN

    created_at: float = 0.0
    version: float = 0.0

    source: str = ""

    def __post_init__(self):
        if not self.created_at:
            self.created_at = time.time()

        if not self.version:
            self.version = time.time()

    def to_dict(self) -> dict:
        data = asdict(self)
        data["status"] = self.status.value
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "Lobby":
        return cls(
            code=data["code"],
            name=data["name"],
            owner=data["owner"],
            host_addr=data["host_addr"],
            max_players=data["max_players"],
            status=LobbyStatus(data.get("status", "open")),
            created_at=data.get("created_at", 0.0),
            version=data.get("version", 0.0),
            source=data.get("source", ""),
        )


@dataclass
class LobbyMember:
    node_id: str
    username: str

    grpc_addr: str

    ready: bool = False
    joined_at: float = 0.0

    def __post_init__(self):
        if not self.joined_at:
            self.joined_at = time.time()

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "LobbyMember":
        return cls(
            node_id=data["node_id"],
            username=data["username"],
            grpc_addr=data.get("grpc_addr", ""),
            ready=data.get("ready", False),
            joined_at=data.get("joined_at", 0.0),
        )

@dataclass
class LobbySession:
    code:       str  = ""
    is_host:    bool = False
    ready:      bool = False
    host_addr:  str  = ""
    lobby_state: Lobby | None = None

    def reset(self):
        self.code      = ""
        self.is_host   = False
        self.ready     = False
        self.host_addr = ""
        self.Lobby     = None

    @property
    def active(self) -> bool:
        return bool(self.code)