"""Locked, crash-recoverable writes for generated brand files.

A transaction first persists every old/new byte and a ``prepared`` manifest.  It
then records ``committing`` progress around target replacements.  Recovery
always rolls a prepared/committing transaction back to the complete old batch;
a durable ``committed`` manifest instead proves and retains the complete new
batch before its journal is removed.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import time
import uuid
from collections.abc import Callable
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any

try:
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None


class TransactionError(RuntimeError):
    pass


FaultInjector = Callable[[str, int | None], None]


class TransactionSession(AbstractContextManager):
    def __init__(self, repo_root: Path, timeout: float = 30.0):
        self.repo_root = Path(repo_root)
        self.root = self.repo_root / ".scraper-transactions"
        self.timeout = timeout
        self._handle = None

    def __enter__(self):
        if fcntl is None:  # pragma: no cover
            raise TransactionError("fcntl.flock is required for transaction locking")
        self.root.mkdir(parents=True, exist_ok=True)
        _fsync_dir(self.root.parent)
        _fsync_dir(self.root)
        lock_path = self.root / ".lock"
        self._handle = lock_path.open("a+b")
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    self._handle.close()
                    raise TransactionError(f"lock timeout after {self.timeout}s: {lock_path}")
                time.sleep(0.1)
            except OSError as exc:
                self._handle.close()
                raise TransactionError(f"cannot lock {lock_path}: {exc}") from exc
        recover_pending_transaction(self)
        return self

    def __exit__(self, *unused):
        if self._handle is not None:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
            self._handle.close()
            self._handle = None
        return False


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_file(path: Path, content: bytes) -> None:
    """Write and fsync a regular file without replacing a destination."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())


def _atomic_write(path: Path, content: bytes) -> None:
    """Atomically replace one file and durably record its directory entry."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
        _fsync_dir(path.parent)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _manifest_path(journal: Path) -> Path:
    return journal / "manifest.json"


def _write_manifest(journal: Path, manifest: dict[str, Any]) -> None:
    content = (json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    _atomic_write(_manifest_path(journal), content)
    _fsync_dir(journal)


def _journal_id(journal: Path) -> str:
    if not journal.name.startswith("txn-"):
        raise TransactionError(f"invalid transaction journal name: {journal}")
    transaction_id = journal.name.removeprefix("txn-")
    if not transaction_id or any(character not in "0123456789abcdef" for character in transaction_id):
        raise TransactionError(f"invalid transaction ID: {journal}")
    return transaction_id


def _relative_target(session: TransactionSession, value: object) -> Path:
    if not isinstance(value, str):
        raise TransactionError("transaction target path is invalid")
    path = Path(value)
    if path.is_absolute() or ".." in path.parts or path == Path("."):
        raise TransactionError(f"transaction target path is unsafe: {value!r}")
    target = session.repo_root / path
    if target.parent != session.repo_root / "data":
        raise TransactionError(f"transaction target is outside data/: {value!r}")
    return target


def _load_manifest(session: TransactionSession, journal: Path) -> dict[str, Any]:
    try:
        raw = _manifest_path(journal).read_bytes()
        manifest = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        raise TransactionError(f"cannot read transaction manifest {journal}: {exc}") from exc
    if not isinstance(manifest, dict) or set(manifest) != {"transaction_id", "state", "targets", "replaced"}:
        raise TransactionError(f"invalid transaction manifest: {journal}")
    if manifest["transaction_id"] != _journal_id(journal):
        raise TransactionError(f"transaction ID mismatch: {journal}")
    if manifest["state"] not in {"prepared", "committing", "committed"}:
        raise TransactionError(f"invalid transaction state: {journal}")
    if not isinstance(manifest["targets"], list) or not manifest["targets"]:
        raise TransactionError(f"invalid transaction targets: {journal}")
    if not isinstance(manifest["replaced"], list) or any(type(index) is not int for index in manifest["replaced"]):
        raise TransactionError(f"invalid transaction replacement progress: {journal}")
    expected_replaced = list(range(len(manifest["replaced"])))
    if manifest["replaced"] != expected_replaced or len(manifest["replaced"]) > len(manifest["targets"]):
        raise TransactionError(f"invalid transaction replacement progress: {journal}")
    required = {"path", "old_exists", "old_file", "new_file", "old_sha256", "new_sha256"}
    paths = set()
    for ordinal, item in enumerate(manifest["targets"]):
        if not isinstance(item, dict) or set(item) != required:
            raise TransactionError(f"invalid transaction target {ordinal}: {journal}")
        target = _relative_target(session, item["path"])
        if target in paths:
            raise TransactionError(f"duplicate transaction target: {journal}")
        paths.add(target)
        if type(item["old_exists"]) is not bool:
            raise TransactionError(f"invalid old_exists value: {journal}")
        if item["old_file"] != f"{ordinal}.bin" or item["new_file"] != f"{ordinal}.bin":
            raise TransactionError(f"invalid transaction backup name: {journal}")
        if not all(isinstance(item[key], str) and len(item[key]) == 64 and all(c in "0123456789abcdef" for c in item[key]) for key in ("old_sha256", "new_sha256")):
            raise TransactionError(f"invalid transaction hash: {journal}")
    if manifest["state"] == "prepared" and manifest["replaced"]:
        raise TransactionError(f"prepared transaction has replacement progress: {journal}")
    if manifest["state"] == "committed" and manifest["replaced"] != list(range(len(manifest["targets"]))):
        raise TransactionError(f"committed transaction is incomplete: {journal}")
    return manifest


def _read_staged(journal: Path, directory: str, name: str, digest: str) -> bytes:
    path = journal / directory / name
    try:
        content = path.read_bytes()
    except OSError as exc:
        raise TransactionError(f"missing {directory} backup {path}: {exc}") from exc
    if _sha(content) != digest:
        raise TransactionError(f"{directory} backup hash mismatch: {path}")
    return content


def _replace_target(target: Path, content: bytes, digest: str) -> None:
    _atomic_write(target, content)
    try:
        actual = target.read_bytes()
    except OSError as exc:
        raise TransactionError(f"cannot verify written target {target}: {exc}") from exc
    if _sha(actual) != digest:
        raise TransactionError(f"written hash mismatch: {target}")


def _restore(session: TransactionSession, journal: Path, manifest: dict[str, Any]) -> None:
    """Restore every target, including those never replaced, then verify all."""
    directories = set()
    for item in manifest["targets"]:
        target = _relative_target(session, item["path"])
        directories.add(target.parent)
        if item["old_exists"]:
            old = _read_staged(journal, "old", item["old_file"], item["old_sha256"])
            _replace_target(target, old, item["old_sha256"])
        elif target.exists():
            target.unlink()
            _fsync_dir(target.parent)
    for item in manifest["targets"]:
        target = _relative_target(session, item["path"])
        if item["old_exists"]:
            if not target.exists() or _sha(target.read_bytes()) != item["old_sha256"]:
                raise TransactionError(f"rollback hash mismatch: {target}")
        elif target.exists():
            raise TransactionError(f"rollback did not remove new target: {target}")
    for directory in directories:
        _fsync_dir(directory)


def _verify_new(session: TransactionSession, manifest: dict[str, Any]) -> None:
    for item in manifest["targets"]:
        target = _relative_target(session, item["path"])
        try:
            content = target.read_bytes()
        except OSError as exc:
            raise TransactionError(f"missing committed target {target}: {exc}") from exc
        if _sha(content) != item["new_sha256"]:
            raise TransactionError(f"disk state unknown after committed transaction: {target}")


def _remove_tree_durably(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path)
        _fsync_dir(path.parent)


def _cleanup_committed(session: TransactionSession, journal: Path) -> None:
    """Rename durable committed work before deleting it, so cleanup is recoverable."""
    cleanup = session.root / f"cleanup-{_journal_id(journal)}"
    if cleanup.exists():
        raise TransactionError(f"cleanup journal already exists: {cleanup}")
    os.replace(journal, cleanup)
    _fsync_dir(session.root)
    _remove_tree_durably(cleanup)


def _recover_cleanup_journals(session: TransactionSession) -> None:
    for cleanup in sorted(session.root.glob("cleanup-*")):
        suffix = cleanup.name.removeprefix("cleanup-")
        if not suffix or any(character not in "0123456789abcdef" for character in suffix):
            raise TransactionError(f"invalid cleanup journal name: {cleanup}")
        # Reaching cleanup-* is only possible after a durable committed manifest
        # was verified and atomically renamed.  It is safe to finish deletion.
        _remove_tree_durably(cleanup)


def recover_pending_transaction(session: TransactionSession) -> None:
    """Recover under the session lock before any reader or writer touches data."""
    _recover_cleanup_journals(session)
    for journal in sorted(session.root.glob("txn-*")):
        _journal_id(journal)
        manifest_path = _manifest_path(journal)
        if not manifest_path.exists():
            # No prepared manifest was durably published, so no target replacement
            # was permitted.  Drop incomplete staging and leave data untouched.
            _remove_tree_durably(journal)
            continue
        manifest = _load_manifest(session, journal)
        if manifest["state"] == "committed":
            _verify_new(session, manifest)
            _cleanup_committed(session, journal)
        else:
            _restore(session, journal, manifest)
            _remove_tree_durably(journal)


def _fault(injector: FaultInjector | None, point: str, ordinal: int | None = None) -> None:
    if injector is not None:
        injector(point, ordinal)


def write_bytes_many(payloads: dict[Path, bytes], session: TransactionSession, *, fault_injector: FaultInjector | None = None) -> None:
    """Persist a complete batch or make its complete prior batch recoverable.

    ``fault_injector`` is a test seam.  A ``BaseException`` it raises models a
    process death and intentionally bypasses the normal in-process rollback.
    """
    if not payloads:
        raise TransactionError("no write payloads")
    transaction_id = uuid.uuid4().hex
    journal = session.root / f"txn-{transaction_id}"
    journal.mkdir()
    _fsync_dir(session.root)
    prepared = False
    committed = False
    try:
        targets = []
        ordered = sorted(payloads.items(), key=lambda row: str(row[0]))
        for ordinal, (raw_target, content) in enumerate(ordered):
            target = Path(raw_target)
            try:
                relative = target.relative_to(session.repo_root)
            except ValueError as exc:
                raise TransactionError(f"{target}: target is outside repository") from exc
            if target.parent != session.repo_root / "data":
                raise TransactionError(f"{target}: target is outside data/")
            if target.parent.stat().st_dev != session.root.stat().st_dev:
                raise TransactionError(f"{target}: transaction journal is on another filesystem")
            old_exists = target.exists()
            old = target.read_bytes() if old_exists else b""
            old_file = f"{ordinal}.bin"
            new_file = f"{ordinal}.bin"
            _write_file(journal / "old" / old_file, old)
            _write_file(journal / "new" / new_file, content)
            _fault(fault_injector, "after_backup", ordinal)
            targets.append(
                {
                    "path": str(relative),
                    "old_exists": old_exists,
                    "old_file": old_file,
                    "new_file": new_file,
                    "old_sha256": _sha(old),
                    "new_sha256": _sha(content),
                }
            )
        _fsync_dir(journal / "old")
        _fsync_dir(journal / "new")
        _fsync_dir(journal)
        _fault(fault_injector, "before_prepared")
        manifest: dict[str, Any] = {
            "transaction_id": transaction_id,
            "state": "prepared",
            "targets": targets,
            "replaced": [],
        }
        _write_manifest(journal, manifest)
        _fsync_dir(session.root)
        prepared = True
        _fault(fault_injector, "after_prepared")

        manifest["state"] = "committing"
        _write_manifest(journal, manifest)
        for ordinal, item in enumerate(targets):
            target = session.repo_root / item["path"]
            new = _read_staged(journal, "new", item["new_file"], item["new_sha256"])
            _replace_target(target, new, item["new_sha256"])
            manifest["replaced"].append(ordinal)
            _write_manifest(journal, manifest)
            _fault(fault_injector, "after_replace", ordinal)

        _verify_new(session, manifest)
        _fault(fault_injector, "before_committed")
        manifest["state"] = "committed"
        _write_manifest(journal, manifest)
        _fsync_dir(session.root)
        committed = True
        _fault(fault_injector, "before_cleanup")
        _cleanup_committed(session, journal)
    except Exception as exc:
        if prepared and not committed:
            try:
                manifest = _load_manifest(session, journal)
                _restore(session, journal, manifest)
                _remove_tree_durably(journal)
            except Exception as rollback:
                raise TransactionError(f"disk state unknown after {exc}; rollback failed: {rollback}") from exc
            raise TransactionError(f"write failed and rolled back: {exc}") from exc
        if not prepared:
            try:
                _remove_tree_durably(journal)
            except Exception as cleanup:
                raise TransactionError(f"write failed before preparation: {exc}; staging cleanup failed: {cleanup}") from exc
            raise TransactionError(f"write failed before preparation: {exc}") from exc
        # The committed marker is durable and every new target was verified.  Do
        # not roll it back; the next locked entry will safely finish cleanup.
        raise TransactionError(f"write committed but journal cleanup is pending: {exc}") from exc
