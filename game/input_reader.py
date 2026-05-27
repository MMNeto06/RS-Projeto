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
        while True:
            char = await self._queue.get()

            if char in ('\r', '\n'):          
                line = self.buffer
                self.buffer = ""
                return line

            elif char in ('\x7f', '\x08'):    
                self.buffer = self.buffer[:-1]

            elif char == '\x15':                
                self.buffer = ""

            elif char == '\x03':              
                raise KeyboardInterrupt

            elif char == '\x04':                
                raise EOFError

            elif ord(char) >= 32:               
                self.buffer += char

    @property
    def prompt_line(self) -> str:
        return f"{self.prompt}{self.buffer}"