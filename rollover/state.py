from __future__ import annotations

import hashlib
import json
import os
from copy import deepcopy
from pathlib import Path


def fingerprint(value) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


class Journal:
    """Atomic ledger and immutable raw archives; corrupted files fail closed."""

    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "rollover_jobs.json"
        self.jobs = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}
        if not isinstance(self.jobs, dict) or any(
            not isinstance(k, str) or not isinstance(v, dict)
            or not isinstance(v.get("session"), str) or not isinstance(v.get("day"), str)
            or v.get("phase") not in {"baseline", "waiting", "archived", "imported", "prepared", "done", "superseded"}
            or not isinstance(v.get("pending_ids", []), list)
            for k, v in self.jobs.items()
        ):
            raise ValueError("invalid_rollover_journal")

    @staticmethod
    def atomic_write(path: Path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(path.suffix + ".tmp")
        try:
            with temp.open("w", encoding="utf-8") as handle:
                if os.name != "nt":
                    os.fchmod(handle.fileno(), 0o600)
                json.dump(value, handle, ensure_ascii=False, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, path)
            if os.name != "nt":
                fd = os.open(path.parent, os.O_RDONLY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
        finally:
            if temp.exists():
                temp.unlink()

    def put(self, key, job):
        proposed = deepcopy(self.jobs)
        proposed[key] = deepcopy(job)
        self.atomic_write(self.path, proposed)
        self.jobs = proposed

    def archive(self, snapshot):
        digest = fingerprint(snapshot)
        name = "archives/" + digest + ".json"
        path = self.root / name
        if not path.exists():
            self.atomic_write(path, snapshot)
        elif fingerprint(json.loads(path.read_text(encoding="utf-8"))) != digest:
            raise ValueError("archive_corrupt")
        return name

    def read_archive(self, name):
        if not isinstance(name, str) or not name.startswith("archives/") or not name.endswith(".json") or len(name) != 78:
            raise ValueError("invalid_archive_path")
        stem = name[len("archives/"):-5]
        if any(c not in "0123456789abcdef" for c in stem):
            raise ValueError("invalid_archive_path")
        value = json.loads((self.root / name).read_text(encoding="utf-8"))
        if fingerprint(value) != stem:
            raise ValueError("archive_corrupt")
        return value
