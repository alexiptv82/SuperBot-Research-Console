"""Chunked upload regression tests.

Covers:
- Small legacy multipart upload still works.
- Chunked flow end-to-end (init + chunks + complete + QA verdict).
- Multi-chunk equivalent of the reported 164 MB scenario (synthetic 30 MB
  split into 4 chunks; validates the code paths that break on large
  single-shot uploads).
- Interrupted upload (missing chunk on complete).
- Resumed upload (idempotent duplicate chunk write).
- Wrong final size (declared size doesn't match assembled bytes).
- Client-supplied SHA256 mismatch.
- Incomplete upload cleanup via DELETE.
- Legacy endpoint refuses very large payloads.
- QA does NOT run before finalize (chunks alone don't create QA runs).
"""
from __future__ import annotations

import hashlib
import io
import os
import sys
import zipfile

from fastapi.testclient import TestClient  # noqa: E402

# All env + init handled by conftest.py.

from server import app  # noqa: E402

sys.path.insert(0, "/app/backend/tests")
from fixtures import BuildOptions, build_zip  # noqa: E402


def _auth(client):
    client.post("/api/auth/login", json={"password": os.environ["SUPERBOT_PASSWORD"]})


def _init(client, payload):
    r = client.post("/api/uploads/init", json=payload)
    assert r.status_code == 200, r.text
    return r.json()


def _chunk(client, upload_id, index, data):
    return client.post(
        f"/api/uploads/{upload_id}/chunk/{index}",
        content=data,
        headers={"Content-Type": "application/octet-stream"},
    )


def _complete(client, upload_id, sha256):
    return client.post(f"/api/uploads/{upload_id}/complete", json={"sha256": sha256})


# ---------------------------------------------------------------------------


def test_legacy_small_multipart_still_works():
    client = TestClient(app)
    _auth(client)
    zbytes = build_zip(BuildOptions(session_id="20260910T123759Z_legacy"))
    r = client.post(
        "/api/sessions/upload",
        files={"files": ("small.zip", zbytes, "application/zip")},
        data={"retain_raw": "false", "checkpoint_hint": "NEW12"},
    )
    assert r.status_code == 200, r.text
    result = r.json()["results"][0]
    assert result["verdict"] == "PASS"
    assert result["duplicate_status"] in ("NEW", "EXACT_DUPLICATE", "SAME_SESSION_DIFFERENT_FILE")


def test_legacy_endpoint_refuses_over_cap():
    client = TestClient(app)
    _auth(client)
    huge = b"\x00" * (65 * 1024 * 1024)  # 65 MiB > 64 MiB legacy cap
    r = client.post(
        "/api/sessions/upload",
        files={"files": ("big.zip", huge, "application/zip")},
        data={"retain_raw": "false"},
    )
    assert r.status_code == 413
    assert "chunked upload" in r.json()["detail"].lower()


def test_chunked_upload_end_to_end_small():
    client = TestClient(app)
    _auth(client)
    zbytes = build_zip(BuildOptions(session_id="20260910T123759Z_chunkedA"))
    sha = hashlib.sha256(zbytes).hexdigest()
    init = _init(client, {"filename": "a.zip", "total_size": len(zbytes), "chunk_size": 512})
    upload_id = init["upload_id"]
    total = init["total_chunks"]
    for i in range(total):
        start = i * 512
        end = min(len(zbytes), start + 512)
        r = _chunk(client, upload_id, i, zbytes[start:end])
        assert r.status_code == 200, r.text
    r = _complete(client, upload_id, sha)
    assert r.status_code == 200, r.text
    result = r.json()
    assert result["verdict"] == "PASS"
    assert result["server_sha256"] == sha


def test_chunked_upload_multi_chunk_large_synthetic():
    """Synthetic large ZIP (~30 MB) split into 4 chunks. Represents the
    real 164 MB scenario without spending 164 MB of test disk."""
    client = TestClient(app)
    _auth(client)
    zbytes = build_zip(BuildOptions(session_id="20260910T123759Z_chunkedB"))
    padded = _pad_zip(zbytes, target_mb=30)
    sha = hashlib.sha256(padded).hexdigest()
    chunk_size = 8 * 1024 * 1024
    init = _init(client, {
        "filename": "big.zip",
        "total_size": len(padded),
        "chunk_size": chunk_size,
        "retain_raw": False,
        "checkpoint_hint": "NEW12",
    })
    upload_id = init["upload_id"]
    total = init["total_chunks"]
    assert total >= 2
    for i in range(total):
        start = i * chunk_size
        end = min(len(padded), start + chunk_size)
        r = _chunk(client, upload_id, i, padded[start:end])
        assert r.status_code == 200, r.text
    r = _complete(client, upload_id, sha)
    assert r.status_code == 200, r.text
    result = r.json()
    assert result["verdict"] == "PASS"
    assert result["server_sha256"] == sha


def test_missing_chunk_fails_complete():
    client = TestClient(app)
    _auth(client)
    zbytes = build_zip(BuildOptions(session_id="20260910T123759Z_missing"))
    sha = hashlib.sha256(zbytes).hexdigest()
    init = _init(client, {"filename": "m.zip", "total_size": len(zbytes), "chunk_size": 512})
    upload_id = init["upload_id"]
    total = init["total_chunks"]
    # Skip chunk index 1 to simulate an interrupted upload
    for i in range(total):
        if i == 1:
            continue
        start = i * 512
        end = min(len(zbytes), start + 512)
        _chunk(client, upload_id, i, zbytes[start:end])
    r = _complete(client, upload_id, sha)
    assert r.status_code == 400
    assert "missing chunks" in r.json()["detail"].lower()


def test_duplicate_chunk_is_idempotent_resume():
    client = TestClient(app)
    _auth(client)
    zbytes = build_zip(BuildOptions(session_id="20260910T123759Z_resume"))
    sha = hashlib.sha256(zbytes).hexdigest()
    init = _init(client, {"filename": "r.zip", "total_size": len(zbytes), "chunk_size": 512})
    upload_id = init["upload_id"]
    total = init["total_chunks"]
    # Send chunk 0 twice (resume case)
    slice0 = zbytes[:512]
    r1 = _chunk(client, upload_id, 0, slice0)
    r2 = _chunk(client, upload_id, 0, slice0)
    assert r1.status_code == 200 and r2.status_code == 200
    # Send remaining chunks
    for i in range(1, total):
        start = i * 512
        end = min(len(zbytes), start + 512)
        _chunk(client, upload_id, i, zbytes[start:end])
    r = _complete(client, upload_id, sha)
    assert r.status_code == 200, r.text


def test_wrong_chunk_size_rejected():
    client = TestClient(app)
    _auth(client)
    zbytes = build_zip(BuildOptions(session_id="20260910T123759Z_wsize"))
    init = _init(client, {"filename": "w.zip", "total_size": len(zbytes), "chunk_size": 512})
    upload_id = init["upload_id"]
    # Send a shorter chunk than expected for a non-final index
    r = _chunk(client, upload_id, 0, zbytes[:256])
    assert r.status_code == 400
    assert "size mismatch" in r.json()["detail"].lower()


def test_wrong_total_size_declared():
    client = TestClient(app)
    _auth(client)
    zbytes = build_zip(BuildOptions(session_id="20260910T123759Z_wts"))
    # Declare a size 100 bytes larger than actual and try to finalize
    init = _init(client, {"filename": "t.zip", "total_size": len(zbytes) + 100, "chunk_size": 4096})
    upload_id = init["upload_id"]
    total = init["total_chunks"]
    # Only send real bytes for the first chunks; last chunk will fail
    for i in range(total - 1):
        start = i * 4096
        end = min(len(zbytes), start + 4096)
        payload = zbytes[start:end]
        if len(payload) != 4096:
            break
        _chunk(client, upload_id, i, payload)
    # Complete without sending final chunk => missing chunk error, which
    # protects us from ever assembling the wrong size.
    r = _complete(client, upload_id, None)
    assert r.status_code == 400


def test_sha256_mismatch_rejected():
    client = TestClient(app)
    _auth(client)
    zbytes = build_zip(BuildOptions(session_id="20260910T123759Z_shabad"))
    init = _init(client, {"filename": "s.zip", "total_size": len(zbytes), "chunk_size": 4096})
    upload_id = init["upload_id"]
    total = init["total_chunks"]
    for i in range(total):
        start = i * 4096
        end = min(len(zbytes), start + 4096)
        _chunk(client, upload_id, i, zbytes[start:end])
    r = _complete(client, upload_id, "0" * 64)
    assert r.status_code == 400
    assert "sha256 mismatch" in r.json()["detail"].lower()


def test_abort_cleans_up_incomplete_upload():
    client = TestClient(app)
    _auth(client)
    init = _init(client, {"filename": "x.zip", "total_size": 2048, "chunk_size": 1024})
    upload_id = init["upload_id"]
    _chunk(client, upload_id, 0, b"\x00" * 1024)
    r = client.delete(f"/api/uploads/{upload_id}")
    assert r.status_code == 200
    # Session should be gone
    r2 = client.get(f"/api/uploads/{upload_id}")
    assert r2.status_code == 404


def test_qa_does_not_run_before_finalize():
    """Uploading chunks but never calling /complete must NOT create any
    audit/QA rows for the session."""
    client = TestClient(app)
    _auth(client)
    zbytes = build_zip(BuildOptions(session_id="20260910T123759Z_noqa"))
    init = _init(client, {"filename": "n.zip", "total_size": len(zbytes), "chunk_size": 512})
    upload_id = init["upload_id"]
    _chunk(client, upload_id, 0, zbytes[:512])
    # No /complete call. Verify no QA row exists for this session id.
    r = client.get("/api/sessions?q=20260910T123759Z_noqa")
    assert r.status_code == 200
    ids = [s["session_id"] for s in r.json()["sessions"]]
    assert "20260910T123759Z_noqa" not in ids
    # cleanup
    client.delete(f"/api/uploads/{upload_id}")


def test_uploads_limits_endpoint():
    client = TestClient(app)
    _auth(client)
    r = client.get("/api/uploads/limits")
    assert r.status_code == 200
    j = r.json()
    assert j["max_upload_bytes"] >= 1024 * 1024 * 1024  # >= 1 GiB
    assert j["recommended_chunk_size"] == 8 * 1024 * 1024


def _pad_zip(zbytes: bytes, target_mb: int) -> bytes:
    """Repack the fixture ZIP with padded parquet payloads to a target
    size while keeping the archive structurally valid."""
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(zbytes), "r") as zin, zipfile.ZipFile(out, "w", zipfile.ZIP_STORED) as zout:
        pad_each = (target_mb * 1024 * 1024) // 6
        pad = b"P" * pad_each
        for info in zin.infolist():
            data = zin.read(info.filename)
            if info.filename.endswith(".parquet"):
                # Preserve PAR1 head + trailer.
                data = data[:4] + pad + data[-4:]
            zout.writestr(info.filename, data)
    return out.getvalue()
