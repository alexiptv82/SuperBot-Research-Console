// Chunked bundle upload client (OLD36 usability wrapper).
//
// Same on-the-wire protocol as chunkedUpload.js but targets
// /api/bundles/* and produces a per-session import summary.

import { api } from "@/lib/api";

const DEFAULT_CHUNK = 8 * 1024 * 1024;

async function sha256File(file) {
  const buf = await file.arrayBuffer();
  const digest = await crypto.subtle.digest("SHA-256", buf);
  return Array.from(new Uint8Array(digest))
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
}

async function postChunk(uploadId, index, blob, signal) {
  const buf = await blob.arrayBuffer();
  const r = await api.post(`/bundles/${uploadId}/chunk/${index}`, buf, {
    headers: { "Content-Type": "application/octet-stream" },
    timeout: 300_000,
    signal,
  });
  return r.data;
}

export async function uploadBundleChunked({
  file,
  chunkSize = DEFAULT_CHUNK,
  maxRetries = 3,
  onProgress = () => {},
  signal,
}) {
  onProgress({ phase: "prepare", uploadedBytes: 0, totalBytes: file.size });

  const sha256Promise = sha256File(file);

  const initResp = await api.post("/bundles/init", {
    filename: file.name,
    total_size: file.size,
    chunk_size: chunkSize,
  });
  const uploadId = initResp.data.upload_id;
  const totalChunks = initResp.data.total_chunks;

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
          try { await api.delete(`/bundles/${uploadId}`); } catch (_) {}
          throw err;
        }
        attempt += 1;
        if (attempt > maxRetries) {
          try { await api.delete(`/bundles/${uploadId}`); } catch (_) {}
          const detail = err?.response?.data?.detail || err?.message || "chunk upload failed";
          throw new Error(`Chunk ${i} failed after ${maxRetries} retries: ${detail}`);
        }
        onProgress({ phase: "retry", chunkIndex: i, attempt, uploadedBytes, totalBytes: file.size });
        await new Promise((res) => setTimeout(res, 500 * Math.pow(2, attempt)));
      }
    }
    uploadedBytes = end;
    onProgress({
      phase: "uploading",
      chunkIndex: i,
      totalChunks,
      uploadedBytes,
      totalBytes: file.size,
    });
  }

  onProgress({ phase: "assembling", uploadedBytes, totalBytes: file.size });
  const sha256 = await sha256Promise;
  onProgress({ phase: "verifying", uploadedBytes, totalBytes: file.size });

  const completeResp = await api.post(
    `/bundles/${uploadId}/complete`,
    { sha256 },
    { timeout: 600_000 },
  );
  onProgress({ phase: "done", uploadedBytes: file.size, totalBytes: file.size, result: completeResp.data });
  return completeResp.data;
}
