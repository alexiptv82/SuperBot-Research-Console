"""Regression: cross-filesystem retention (EXDEV) must fall back to
streaming copy, not silently drop the raw artefact.

Reproduces the runtime bug that caused the first 2.14 GiB OLD36
bundle finalize to land 11 QA rows in the DB while retaining zero
raw ZIP files on disk (extraction workdir on /tmp overlay vs
retention directory on the mounted data volume).
"""
from __future__ import annotations

import errno
import hashlib
import os
from pathlib import Path

import pytest

import raw_storage
from raw_storage import persist_from_path


def test_cross_filesystem_fallback_copies_and_unlinks(tmp_path, monkeypatch):
    src = tmp_path / "src.zip"
    payload = b"cross-fs payload " * 4096
    src.write_bytes(payload)
    sha = hashlib.sha256(payload).hexdigest()

    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()

    # Monkeypatch os.replace inside raw_storage to always raise EXDEV.
    def fake_replace(_src, _dst):  # noqa: ARG001
        raise OSError(errno.EXDEV, "Invalid cross-device link")

    monkeypatch.setattr(raw_storage.os, "replace", fake_replace)

    dest = persist_from_path(raw_dir, src, sha)
    assert dest.exists(), "canonical destination must be created via copy fallback"
    assert dest.read_bytes() == payload
    assert not src.exists(), "src must be unlinked after successful copy fallback"


def test_replace_error_other_than_exdev_reraises(tmp_path, monkeypatch):
    src = tmp_path / "src.zip"
    src.write_bytes(b"x")
    sha = hashlib.sha256(b"x").hexdigest()

    def fake_replace(_src, _dst):  # noqa: ARG001
        raise OSError(errno.EACCES, "permission denied")

    monkeypatch.setattr(raw_storage.os, "replace", fake_replace)

    with pytest.raises(OSError) as ei:
        persist_from_path(tmp_path / "raw", src, sha)
    assert ei.value.errno == errno.EACCES
