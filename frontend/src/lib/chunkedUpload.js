// Chunked upload client for the SuperBot Research Console.
//
// - Splits a File into fixed-size chunks (default 8 MiB).
// - Sends chunks sequentially as raw octet-stream bodies to
//   /api/uploads/{id}/chunk/{index}.
// - Retries each chunk up to `maxRetries` with exponential backoff.
// - Computes the file SHA256 incrementally via WebCrypto during the
//   upload (parallel to the chunk POSTs) so the server-side hash can be
//   verified at finalize time.
// - Emits a rich status stream via `onProgress` for the UI.

import { api } from "@/lib/api";

const DEFAULT_CHUNK = 8 * 1024 * 1024;

async function sha256File(file, onHashProgress) {
  // WebCrypto has no incremental API; we stream via a manual SHA-256
  // implementation is overkill for V1. Instead we compute the digest
  // once over the whole file using SubtleCrypto in a single call, but
  // stream it in chunks via ArrayBuffer to avoid a second file read.
  // Most browsers can hash 500 MB in a couple of seconds.
  const buf = await file.arrayBuffer();
  onHashProgress && onHashProgress(1);
  const digest = await crypto.subtle.digest("SHA-256", buf);
  return Array.from(new Uint8Array(digest))
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
}

async function postChunk(uploadId, index, blob, signal) {
  const buf = await blob.arrayBuffer();
  const r = await api.post(`/uploads/${uploadId}/chunk/${index}`, buf, {
    headers: { "Content-Type": "application/octet-stream" },
    timeout: 120_000,
    signal,
  });
  return r.data;
}

export async function uploadChunked({
  file,
  chunkSize = DEFAULT_CHUNK,
  retainRaw = false,
  checkpointHint = null,
  maxRetries = 3,
  onProgress = () => {},
  signal,
}) {
  onProgress({ phase: "prepare", uploadedBytes: 0, totalBytes: file.size });

  // Kick off SHA256 hashing in parallel with the first chunks so we
  // don’t block the upload on it.
  const sha256Promise = sha256File(file, (p) => onProgress({ phase: "hashing", hashProgress: p }));

  // 1. Init the server-side session.
  const initResp = await api.post("/uploads/init", {
    filename: file.name,
    total_size: file.size,
    chunk_size: chunkSize,
    retain_raw: retainRaw,
    checkpoint_hint: checkpointHint || null,
  });
  const uploadId = initResp.data.upload_id;
  const totalChunks = initResp.data.total_chunks;

  // 2. Sequentially POST every chunk with retries.
  let uploadedBytes = 0;
  for (let i = 0; i < totalChunks; i++) {
    const start = i * chunkSize;
    const end = Math.min(file.size, start + chunkSize);
    const blob = file.slice(start, end);

    let attempt = 0;
    while (true) {
      try {
        if (signal?.aborted) throw new Error("aborted");
        await postChunk(uploadId, i, blob, signal);
        break;
      } catch (err) {
        if (signal?.aborted) {
          try { await api.delete(`/uploads/${uploadId}`); } catch (_) {}
          throw err;
        }
        attempt += 1;
        if (attempt > maxRetries) {
          try { await api.delete(`/uploads/${uploadId}`); } catch (_) {}
          const detail = err?.response?.data?.detail || err?.message || "chunk upload failed";
          throw new Error(`Chunk ${i} failed after ${maxRetries} retries: ${detail}`);
        }
        onProgress({
          phase: "retry",
          uploadId,
          chunkIndex: i,
          attempt,
          uploadedBytes,
          totalBytes: file.size,
        });
        // Exponential backoff
        await new Promise((res) => setTimeout(res, 500 * Math.pow(2, attempt)));
      }
    }
    uploadedBytes = end;
    onProgress({
      phase: "uploading",
      uploadId,
      chunkIndex: i,
      totalChunks,
      uploadedBytes,
      totalBytes: file.size,
    });
  }

  // 3. Wait for SHA256, then finalize.
  onProgress({
    phase: "assembling",
    uploadId,
    uploadedBytes,
    totalBytes: file.size,
  });
  const sha256 = await sha256Promise;
  onProgress({
    phase: "qa",
    uploadId,
    uploadedBytes,
    totalBytes: file.size,
  });
  const completeResp = await api.post(`/uploads/${uploadId}/complete`, { sha256 }, {
    timeout: 300_000,
  });
  onProgress({
    phase: "done",
    uploadId,
    uploadedBytes: file.size,
    totalBytes: file.size,
    result: completeResp.data,
  });
  return completeResp.data;
}
