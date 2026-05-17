"""
utils.py
Funções utilitárias partilhadas entre módulos do jogo.
"""
# a ser expandido? neste moemnto este ó o único usado por muita gente.

def chebyshev(x1: int, y1: int, x2: int, y2: int) -> int:
    """
    Distância de Chebyshev entre dois pontos num grid (max(|dx|, |dy|)).
    Usada para calcular alcance de ataques: range 1 = melee (8 células adjacentes).
    """
    return max(abs(x1 - x2), abs(y1 - y2))