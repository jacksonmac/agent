"""Non-blocking single-key reads from a TTY (termios cbreak + select).

Used by ui.py for the [p]ause / [m]essage / [o]pen / [q]uit controls. cbreak
rather than raw mode: ISIG stays on so Ctrl-C still raises KeyboardInterrupt,
and output post-processing is untouched so rich rendering isn't mangled.
"""

from __future__ import annotations

import atexit
import os
import select
import sys
import time
from contextlib import contextmanager

try:
    import termios
    import tty
    HAVE_TERMIOS = True
except ImportError:  # windows / exotic environments
    HAVE_TERMIOS = False


class KeyReader:
    def __init__(self, fd: int | None = None):
        self.fd = sys.stdin.fileno() if fd is None else fd
        self._saved = None  # termios attrs to restore; also the "active" flag
        self._eof = False   # stdin hung up: stop selecting, emulate timeouts

    @property
    def active(self) -> bool:
        return self._saved is not None

    def start(self) -> None:
        if not HAVE_TERMIOS or self._saved is not None:
            return
        if not os.isatty(self.fd):
            return
        self._saved = termios.tcgetattr(self.fd)
        self._eof = False
        tty.setcbreak(self.fd)
        atexit.register(self.stop)

    def stop(self) -> None:
        """Idempotent — safe to call from atexit, ui.stop(), and tests."""
        if self._saved is None:
            return
        try:
            termios.tcsetattr(self.fd, termios.TCSADRAIN, self._saved)
        except (termios.error, OSError, ValueError):
            pass  # fd may already be closed at interpreter shutdown
        self._saved = None

    def poll(self) -> str | None:
        """One buffered key, or None — never blocks."""
        if self._saved is None:
            return None
        r, _, _ = select.select([self.fd], [], [], 0)
        if not r:
            return None
        data = os.read(self.fd, 1)
        return data.decode(errors="ignore") or None

    def _read_byte(self, timeout: float | None) -> bytes | None:
        """One byte; None on timeout; b'' on EOF/hangup (which select
        reports as readable — without tracking it the caller would spin)."""
        r, _, _ = select.select([self.fd], [], [], timeout)
        if not r:
            return None
        try:
            b = os.read(self.fd, 1)
        except OSError:  # macOS ptys raise EIO on hangup instead of b""
            b = b""
        if b == b"":
            self._eof = True
        return b

    def read_token(self, timeout: float | None = 0.1) -> str | None:
        """One decoded key, or None on timeout. Escape handling: after a lone
        \\x1b, wait briefly for a follow-up — '[' (CSI) or 'O' (SS3) starts a
        sequence (arrows, F-keys) which is swallowed entirely (returns None);
        no follow-up means a real Esc press (returns '\\x1b')."""
        if self._saved is None:
            return None
        if self._eof:
            # stdin is gone for good: emulate the timeout so the reader
            # thread doesn't busy-spin at 100% CPU
            time.sleep(timeout if timeout else 0.1)
            return None
        b = self._read_byte(timeout)
        if not b:
            return None
        if b != b"\x1b":
            return b.decode(errors="ignore") or None
        nxt = self._read_byte(0.03)
        if nxt in (b"[", b"O"):
            # swallow to the final byte of the sequence (0x40-0x7e for CSI;
            # SS3 is a single byte after 'O')
            while True:
                c = self._read_byte(0.03)
                if not c or (nxt == b"O") or 0x40 <= c[0] <= 0x7e:
                    return None
        elif nxt:
            # Alt+key style: ignore the pair
            return None
        return "\x1b"

    @contextmanager
    def suspend(self):
        """Cooked mode for the duration — wraps every input() call so line
        editing and echo work normally."""
        if self._saved is None:
            yield
            return
        saved = self._saved
        termios.tcsetattr(self.fd, termios.TCSADRAIN, saved)
        try:
            yield
        finally:
            tty.setcbreak(self.fd)
