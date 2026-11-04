"""Test tripwires: metadata commands must not connect or execute project tools."""
from contextlib import contextmanager
from pathlib import Path
import runpy
import socket
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT))
from tools.project_tasks import engine, store


@contextmanager
def forbid(marker):
    def stopped(*args, **kwargs):
        store.write_new(marker, store.encode(dict(forbidden_execution=True)))
        raise RuntimeError("metadata-only validation attempted execution")
    with patch.object(socket, "create_connection", stopped), \
            patch.object(socket.socket, "connect", stopped), \
            patch.object(socket.socket, "connect_ex", stopped), \
            patch.object(engine.ProjectRecords, "execute", stopped):
        yield


if __name__ == "__main__":
    marker = Path(sys.argv[1])
    sys.argv = [str(ROOT / "tools/project_tasks/runner.py"), *sys.argv[2:]]
    with forbid(marker):
        runpy.run_path(sys.argv[0], run_name="__main__")
