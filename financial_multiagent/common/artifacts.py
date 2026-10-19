"""Portable JSON snapshots and publication of complete, immutable stage runs."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile
from uuid import uuid4


def canonical_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def sha256_bytes(raw):
    return hashlib.sha256(raw).hexdigest()


def content_hash(value):
    return sha256_bytes(canonical_json(value).encode("utf-8"))


def read_scoped_json(path, access_scope):
    raw = Path(path).read_bytes()
    value = json.loads(raw)
    if not access_scope or not isinstance(value, dict) or value.get("access_scope") != access_scope:
        raise PermissionError("Artifact access scope differs from caller")
    return value, sha256_bytes(raw)


def read_jsonl(path):
    raw = Path(path).read_bytes()
    values = [json.loads(s) for s in raw.decode("utf-8").splitlines() if s.strip()]
    return values, sha256_bytes(raw)


def new_run_id():
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid4().hex[:12]


def publish_run(base, run_id, files):
    """Publish a directory only after all files are written; never overwrite a run."""
    base = Path(base)
    if not run_id or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for c in run_id):
        raise ValueError("Invalid run ID")
    base.mkdir(parents=True, exist_ok=True)
    destination = base / run_id
    if destination.exists():
        raise FileExistsError("Run already published")
    with tempfile.TemporaryDirectory(prefix=".stage-", dir=base) as temporary:
        stage = Path(temporary) / "run"
        stage.mkdir()
        for name, text in files.items():
            if Path(name).name != name or name in {".", ".."}:
                raise ValueError("Stage filenames must be simple names")
            (stage / name).write_text(text, encoding="utf-8")
        os.rename(stage, destination)
    return destination
