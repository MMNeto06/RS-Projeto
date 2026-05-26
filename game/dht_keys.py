#functions for getting the kademlia keys


def addr_key(node_id_hex: str) -> str:
    #get grpc address from node id
    return f"peer:{node_id_hex}:addr"


def username_key(username: str) -> str:
    # get node id from username (used for pinging users to check if the username is unavailable)
    return f"username:{username}"


def peer_lobby_key(node_id_hex: str) -> str:
    #lobby key from node id
    return f"peer:{node_id_hex}:lobby"


def lobby_meta_key(code: str) -> str:
    #lobby metadata key
    return f"lobby:{code}:meta"


def lobby_member_key(code: str, node_id_hex: str) -> str:
    #lobby member registry key from lobby code and node id
    return f"lobby:{code}:member:{node_id_hex}"


def lobby_index_key(code: str) -> str:
    #lobby member counter from lobby code
    return f"lobby:index:{code}"
