"""Atomic JSON writes use a unique temp file and fsync before rename."""

import json
import os
from pathlib import Path

import json_store


def test_atomic_write_uses_unique_temp_and_fsyncs(tmp_path, monkeypatch):
    fsyncs = []
    real_fsync = os.fsync

    def spy_fsync(fd):
        fsyncs.append(fd)
        return real_fsync(fd)

    names = []
    real_replace = os.replace

    def spy_replace(src, dst):
        names.append(Path(src).name)
        assert Path(src).is_file()
        assert Path(src).stat().st_size > 0
        return real_replace(src, dst)

    monkeypatch.setattr(json_store.os, "fsync", spy_fsync)
    monkeypatch.setattr(json_store.os, "replace", spy_replace)
    target = tmp_path / "state.json"
    json_store.atomic_write_json(target, {"n": 1})
    json_store.atomic_write_json(target, {"n": 2})
    assert json.loads(target.read_text()) == {"n": 2}
    assert len(fsyncs) == 2
    assert len(set(names)) == 2
    assert all(not name.endswith("state.json.tmp") for name in names)
    assert list(tmp_path.glob("*.tmp")) == []
