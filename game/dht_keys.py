"""
dht_keys.py
Funções puras que geram as chaves usadas na DHT (Kademlia).
Centralizar aqui evita strings mágicas espalhadas pelo código.
"""


def addr_key(node_id_hex: str) -> str:
    """Chave para obter o endereço gRPC de um node pelo seu ID."""
    return f"peer:{node_id_hex}:addr"


def username_key(username: str) -> str:
    """Chave para obter o node_id de um utilizador pelo seu username."""
    return f"username:{username}"


def peer_lobby_key(node_id_hex: str) -> str:
    """Chave do lobby anunciado por um peer (para listagem rápida)."""
    return f"peer:{node_id_hex}:lobby"


def lobby_meta_key(code: str) -> str:
    """Metadados de um lobby (nome, owner, max_players, status…)."""
    return f"lobby:{code}:meta"


def lobby_member_key(code: str, node_id_hex: str) -> str:
    """Registo de um membro específico num lobby."""
    return f"lobby:{code}:member:{node_id_hex}"


def lobby_index_key(code: str) -> str:
    """Contador de membros num lobby (pode ser usado como sentinel de existência)."""
    return f"lobby:index:{code}"
