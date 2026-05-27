def addr_key(node_id_hex: str) -> str:
    return f"peer:{node_id_hex}:addr"


def username_key(username: str) -> str:
    return f"username:{username}"


def peer_lobby_key(node_id_hex: str) -> str:
    return f"peer:{node_id_hex}:lobby"


def lobby_meta_key(code: str) -> str:
    return f"lobby:{code}:meta"


def lobby_member_key(code: str, node_id_hex: str) -> str:
    return f"lobby:{code}:member:{node_id_hex}"


def lobby_index_key(code: str) -> str:
    return f"lobby:index:{code}"
