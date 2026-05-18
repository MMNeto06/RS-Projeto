from enum import Enum

class LobbyStatus(str, Enum):
    OPEN = "open"
    STARTED = "started"
    ABANDONED = "abandoned"
