"""
input_reader.py
Leitor de input caracter-a-caracter para Unix (tty.setcbreak).

Mantém um `.buffer` público com o texto que o utilizador está
a escrever.  O renderer lê este buffer em cada frame e redesenha
a linha de prompt — tornando o input sempre visível mesmo quando
o ecrã é atualizado.

Compatibilidade: Unix/macOS (requer `tty` e `termios`).
Windows: fallback para aioconsole (sem buffer visível durante render).
"""

import asyncio
import os
import sys

try:
    import tty
    import termios
    _UNIX = True
except ImportError:
    _UNIX = False


class InputReader:
    """
    Lê stdin em modo cbreak (Ctrl+C/Z continuam a funcionar como sinais).

    Uso típico:
        reader = InputReader()
        reader.start()
        try:
            line = await reader.readline()   # bloqueia até Enter
        finally:
            reader.stop()

    O renderer acede a `reader.prompt_line` para saber o que mostrar.
    """

    def __init__(self, prompt: str = "> "):
        self.prompt = prompt
        self.buffer: str = ""
        self._queue: asyncio.Queue = asyncio.Queue()
        self.input_event: asyncio.Event = None
        self._fd = sys.stdin.fileno() if _UNIX else None
        self._old_settings = None
        self._started = False

    def start(self):
        if not _UNIX or self._started:
            return
        try:
            if not os.isatty(self._fd):
                return
        except Exception:
            return
        self.input_event = asyncio.Event()
        self._old_settings = termios.tcgetattr(self._fd)
        tty.setcbreak(self._fd)
        asyncio.get_event_loop().add_reader(self._fd, self._on_readable)
        self._started = True

    def stop(self):
        """Restaura as definições do terminal e remove o leitor."""
        if not _UNIX or not self._started:
            return
        try:
            asyncio.get_event_loop().remove_reader(self._fd)
        except Exception:
            pass
        try:
            termios.tcsetattr(self._fd, termios.TCSADRAIN, self._old_settings)
        except Exception:
            pass
        self._started = False

    # -- LEITURA ----------------------------------------------------

    def _on_readable(self):
        try:
            raw = os.read(self._fd, 32)
        except OSError:
            return
        for byte in raw:
            self._queue.put_nowait(chr(byte))
        if self.input_event:
            self.input_event.set()

    async def readline(self) -> str:
        """
        Coroutine que aguarda o utilizador pressionar Enter.
        Gere: backspace, Ctrl+U (limpar linha), Ctrl+C, Ctrl+D.
        Devolve a linha digitada (sem newline).
        """
        while True:
            char = await self._queue.get()

            if char in ('\r', '\n'):            # Enter
                line = self.buffer
                self.buffer = ""
                return line

            elif char in ('\x7f', '\x08'):      # Backspace / DEL
                self.buffer = self.buffer[:-1]

            elif char == '\x15':                # Ctrl+U — apagar linha
                self.buffer = ""

            elif char == '\x03':                # Ctrl+C
                raise KeyboardInterrupt

            elif char == '\x04':                # Ctrl+D / EOF
                raise EOFError

            elif ord(char) >= 32:               # caracteres imprimíveis
                self.buffer += char

    # -- UTILITÁRIO PARA O RENDERER ---------------------------------

    @property
    def prompt_line(self) -> str:
        """Linha completa pronta para o renderer: prompt + o que foi digitado."""
        return f"{self.prompt}{self.buffer}"