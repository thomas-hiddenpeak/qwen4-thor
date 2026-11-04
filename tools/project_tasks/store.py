"""Private, bounded task ledgers. One exclusive writer per task; no replay."""
from contextlib import contextmanager, ExitStack
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import uuid

from tools.project_tasks.errors import ContractError

ROOT = Path(__file__).resolve().parents[2]
FORMAT = "q4t.project.tasks.v1"
MAX_EVENTS = 256
MAX_EVENT_BYTES = 1024 * 1024
MAX_WIRE_BYTES = 1024 * 1024
RESERVED_BYTES = 80 * 1024 * 1024


class ArchivedTask(ValueError):
    """A permanent key exists; callers must never recreate this task."""


def require(value, message, *, code="contract_violation"):
    if not value:
        raise ContractError(message, code)


def encode(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False,
                      sort_keys=True, separators=(",", ":")).encode()


def digest(value):
    return hashlib.sha256(encode(value)).hexdigest()


def private(path, directory=False):
    info = Path(path).lstat()
    require((stat.S_ISDIR(info.st_mode) if directory else
             stat.S_ISREG(info.st_mode) and info.st_nlink == 1) and
            info.st_uid == os.getuid() and
            stat.S_IMODE(info.st_mode) == (0o700 if directory else 0o600),
            "private local object required: " + str(path), code="unsafe_path")
    return info


def local(path):
    path = Path(os.path.abspath(path))
    require(path.is_relative_to(ROOT / ".q4t-work") or
            path.is_relative_to(ROOT / "build"), "use build or .q4t-work", code='unsafe_path')
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        require(not current.is_symlink(), "symlink path refused", code='unsafe_path')
    return path


def sync(directory):
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_new(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                 0o600)
    with os.fdopen(fd, "wb") as output:
        output.write(data)
        output.flush()
        os.fsync(output.fileno())
    sync(Path(path).parent)


@contextmanager
def lock(path, *, exclusive=True):
    before = private(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        require((before.st_dev, before.st_ino) ==
                (os.fstat(fd).st_dev, os.fstat(fd).st_ino), "lock changed")
        fcntl.flock(fd, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) |
                    fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


def init(path, max_tasks=16):
    require(type(max_tasks) is int and 1 <= max_tasks <= 64,
            "max_tasks must be 1..64", code='invalid_arguments')
    path = local(path)
    path.mkdir(parents=True, mode=0o700)
    write_new(path / ".lock", b"")
    (path / "tasks").mkdir(mode=0o700)
    info = path.stat()
    write_new(path / "store.json", encode(dict(format=FORMAT,
        store_id=uuid.uuid4().hex, uid=os.getuid(), root_device=info.st_dev,
        root_inode=info.st_ino, max_tasks=max_tasks,
        reserved_bytes_per_task=RESERVED_BYTES)))
    return dict(created=True, max_tasks=max_tasks)


def checked_root(path, *, allow_handoff=False, logical_root=None):
    path = local(path)
    info = private(path, True)
    private(path / "tasks", True)
    private(path / "store.json")
    meta = json.loads((path / "store.json").read_bytes())
    require(meta["format"] == FORMAT and meta["uid"] == os.getuid() and
            (meta["root_device"], meta["root_inode"]) ==
            (info.st_dev, info.st_ino) and
            1 <= meta["max_tasks"] <= 64 and
            meta["reserved_bytes_per_task"] == RESERVED_BYTES,
            "store identity/policy changed", code='integrity_error')
    entries = {p.name for p in path.iterdir()}
    allowed = {".lock", "store.json", "tasks", "retention", "restored.json"}
    require(allow_handoff or not entries.intersection({"handoff.json", ".handoff.next"}),
            "unknown store entry or retired store", code="store_retired")
    if allow_handoff:
        allowed.update(("handoff.json", ".handoff.next"))
    require({".lock", "store.json", "tasks"} <= entries <= allowed, "unknown store entry or retired store")
    if "restored.json" in entries:
        from tools.project_tasks import backup
        backup.check_placement(path, meta, logical_root=logical_root)
    if "retention" in entries:
        from tools.project_tasks import retention
        retention.extension(path, meta)
    return path, meta


@contextmanager
def root_lock(path, *, exclusive=True, allow_handoff=False, logical_root=None):
    options = dict(allow_handoff=allow_handoff, logical_root=logical_root)
    path, _ = checked_root(path, **options)
    with lock(path / ".lock", exclusive=exclusive):
        # A checked path can be retired while this process is between its
        # first lookup and lock acquisition. Never use that stale approval.
        yield checked_root(path, **options)


def task_name(client_id, key):
    require(isinstance(key, str) and 1 <= len(key.encode()) <= 256,
            "idempotency key must be 1..256 bytes", code='invalid_arguments')
    require(isinstance(client_id, str) and
            re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", client_id), "client ID", code='invalid_arguments')
    return digest([client_id, key])


def connection(path):
    private(path / "ledger.db")
    for child in path.iterdir():
        require(child.name in {"ledger.db", "ledger.db-journal", ".lock"} or
                re.fullmatch(r"(?:wire|check)-[0-9]{3}\.bin", child.name),
                "unknown task entry")
        private(child)
    db = sqlite3.connect(path / "ledger.db", timeout=2, isolation_level=None)
    try:
        require(db.execute("PRAGMA journal_mode=DELETE").fetchone()[0] ==
                "delete", "rollback journal unavailable")
        db.execute("PRAGMA synchronous=FULL")
        db.execute("PRAGMA max_page_count=8192")
        require(db.execute("PRAGMA page_size").fetchone()[0] == 4096 and
                db.execute("PRAGMA quick_check").fetchall() == [("ok",)],
                "ledger format/integrity", code='integrity_error')
        return db
    except BaseException:
        db.close()
        raise


def create(root, name, spec):
    root, meta = checked_root(root)
    require(re.fullmatch(r"[0-9a-f]{64}", name), "task identity")
    with root_lock(root):
        from tools.project_tasks import retention
        if retention.present(root, meta, name):
            raise ArchivedTask("task key was archived")
        path = root / "tasks" / name
        if os.path.lexists(path):
            private(path, True)
            return path, False
        entries = list((root / "tasks").iterdir())
        require(len(entries) < meta["max_tasks"], "task capacity exhausted", code='task_capacity')
        require(all(re.fullmatch(r"[0-9a-f]{64}", p.name) for p in entries),
                "interrupted task creation; preserve and inspect")
        retention.admit(root, meta)
        require(shutil.disk_usage(root).free >= RESERVED_BYTES,
                "insufficient task storage headroom", code='storage_full')
        temporary = root / "tasks" / (".creating-" + uuid.uuid4().hex)
        temporary.mkdir(mode=0o700)
        write_new(temporary / ".lock", b"")
        write_new(temporary / "ledger.db", b"")
        db = connection(temporary)
        try:
            db.execute("BEGIN IMMEDIATE")
            db.execute("CREATE TABLE meta (id INTEGER PRIMARY KEY CHECK(id=1), value TEXT NOT NULL)")
            db.execute("CREATE TABLE events (seq INTEGER PRIMARY KEY, kind TEXT NOT NULL, payload TEXT NOT NULL, previous TEXT NOT NULL, digest TEXT NOT NULL)")
            db.execute("INSERT INTO meta VALUES (1, ?)",
                       (encode(dict(format=FORMAT, task_id=name,
                                    store_id=meta["store_id"], spec=spec)).decode(),))
            db.execute("COMMIT")
        finally:
            db.close()
        sync(temporary)
        os.rename(temporary, path)
        sync(path.parent)
        return path, True


class Ledger:
    def __init__(self, path, db, store_id):
        self.path, self.db = path, db
        self.meta = json.loads(db.execute("SELECT value FROM meta WHERE id=1").fetchone()[0])
        require(self.meta["format"] == FORMAT and
                self.meta["task_id"] == path.name and
                self.meta["store_id"] == store_id, "task identity changed", code='integrity_error')
        self.spec = self.meta["spec"]
        self.initial = digest(self.meta)

    def events(self):
        rows = self.db.execute("SELECT seq,kind,payload,previous,digest FROM events ORDER BY seq").fetchmany(MAX_EVENTS + 1)
        require(len(rows) <= MAX_EVENTS, "event count exceeded")
        result, previous = [], self.initial
        for index, (seq, kind, text, parent, checksum) in enumerate(rows, 1):
            require(seq == index and parent == previous and
                    len(text.encode()) <= MAX_EVENT_BYTES, "event sequence/size", code='integrity_error')
            payload = json.loads(text)
            require(digest([seq, kind, payload, parent]) == checksum,
                    "event content changed", code='integrity_error')
            result.append(dict(seq=seq, kind=kind, payload=payload,
                               digest=checksum))
            previous = checksum
        return result

    def append(self, kind, payload):
        events = self.events()
        require(len(events) < MAX_EVENTS and
                len(encode(payload)) <= MAX_EVENT_BYTES, "event budget exhausted", code='task_budget')
        seq = len(events) + 1
        previous = events[-1]["digest"] if events else self.initial
        checksum = digest([seq, kind, payload, previous])
        self.db.execute("BEGIN IMMEDIATE")
        try:
            self.db.execute("INSERT INTO events VALUES (?,?,?,?,?)",
                (seq, kind, encode(payload).decode(), previous, checksum))
            self.db.execute("COMMIT")
        except BaseException:
            if self.db.in_transaction:
                self.db.execute("ROLLBACK")
            raise
        return seq

    def wire(self, number):
        require(type(number) is int and 1 <= number <= 8, "wire identity")
        return self.path / f"wire-{number:03d}.bin"


@contextmanager
def open_task(root, name, *, exclusive=True):
    root, meta = checked_root(root)
    require(re.fullmatch(r"[0-9a-f]{64}", name), "task identity")
    path = root / "tasks" / name
    with ExitStack() as stack:
        # Stabilize lookup and claim against archive rename; release the root
        # lock before any model work so other task keys can still proceed.
        with root_lock(root, exclusive=False):
            from tools.project_tasks import retention
            if retention.present(root, meta, name):
                raise ArchivedTask("task key was archived")
            private(path, True)
            stack.enter_context(lock(path / ".lock", exclusive=exclusive))
        db = connection(path)
        try:
            yield Ledger(path, db, meta["store_id"])
        finally:
            db.close()
