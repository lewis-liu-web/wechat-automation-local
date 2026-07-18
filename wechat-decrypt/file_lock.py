#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tiny cross-process file lock using only stdlib."""
from __future__ import annotations
import os
import sys
import time
from pathlib import Path
from typing import Optional

class LockTimeout(Exception): pass

class _InterProcessLock:
    def __init__(self, path, timeout=30.0, poll=0.05):
        self.path = Path(path)
        self.timeout = timeout
        self.poll = poll
        self._fd = None
    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self.path), os.O_CREAT | os.O_RDWR)
        self._fd = fd
        try:
            size = os.lseek(fd, 0, os.SEEK_END)
            if size == 0:
                os.write(fd, b'\x00')
            os.lseek(fd, 0, os.SEEK_SET)
            self._acquire(fd)
        except Exception:
            self._close()
            raise
        return self
    def __exit__(self, *args):
        self._release()
        self._close()
        return False
    def _close(self):
        if self._fd is not None:
            try: os.close(self._fd)
            except OSError: pass
            self._fd = None
    def _acquire(self, fd): raise NotImplementedError
    def _release(self): raise NotImplementedError

if sys.platform == 'win32':
    import msvcrt
    class InterProcessLock(_InterProcessLock):
        def _acquire(self, fd):
            deadline = time.time() + self.timeout
            while True:
                try:
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                    return
                except OSError:
                    if time.time() >= deadline:
                        raise LockTimeout('could not acquire lock %s within %.1fs' % (self.path, self.timeout))
                    time.sleep(self.poll)
        def _release(self):
            if self._fd is not None:
                try:
                    os.lseek(self._fd, 0, os.SEEK_SET)
                    msvcrt.locking(self._fd, msvcrt.LK_UNLCK, 1)
                except OSError: pass
else:
    import fcntl
    class InterProcessLock(_InterProcessLock):
        def _acquire(self, fd):
            deadline = time.time() + self.timeout
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    return
                except (OSError, BlockingIOError):
                    if time.time() >= deadline:
                        raise LockTimeout('could not acquire lock %s within %.1fs' % (self.path, self.timeout))
                    time.sleep(self.poll)
        def _release(self):
            if self._fd is not None:
                try: fcntl.flock(self._fd, fcntl.LOCK_UN)
                except OSError: pass

def lock_path_for_db(db_path):
    db_path = Path(db_path)
    return db_path.parent / ('.' + db_path.name + '.refresh_lock')


def refresh_lock_path(root='.'):
    """Return the global refresh lock path for a project root."""
    return Path(root) / 'temp' / 'fast_refresh.lock'
