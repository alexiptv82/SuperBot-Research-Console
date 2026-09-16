// Multipart OLD36 bundle upload (5 raw binary parts).
//
// Client reconstructs nothing locally \u2014 it just uploads the 5 parts to
// their server-side .part slots via the existing /api/bundles/{id}/chunk
// endpoints, then triggers assemble which:
//   1) concatenates parts (streaming, server-side)
//   2) verifies SHA256
//   3) hands the reassembled ZIP to the existing single-bundle import
//      pipeline (inner ZIP validation + per-session QA + retention).

import { api } from "@/lib/api";

const DEFAULT_CHUNK = 8 * 1024 * 1024;

export async function fetchMultipartManifest() {
  const r = await api.get("/bundles/multipart/manifest");
  return r.data;
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

async function uploadOnePart(slot, file, { onProgress, signal, maxRetries = 3 }) {
  const chunkSize = slot.chunk_size || DEFAULT_CHUNK;
  const totalChunks = slot.total_chunks || Math.ceil(file.size / chunkSize);
  let uploaded = 0;
  for (let i = 0; i < totalChunks; i++) {
    const start = i * chunkSize;
    const end = Math.min(file.size, start + chunkSize);
    const blob = file.slice(start, end);
    let attempt = 0;
    while (true) {
      try {
        if (signal?.aborted) throw new Error("aborted");
        await postChunk(slot.upload_id, i, blob, signal);
        break;
      } catch (err) {
        attempt += 1;
        if (attempt > maxRetries) {
          const detail = err?.response?.data?.detail || err?.message || "chunk upload failed";
          throw new Error(`Part ${slot.part_name} chunk ${i}: ${detail}`);
        }
        await new Promise((res) => setTimeout(res, 500 * Math.pow(2, attempt)));
      }
    }
    uploaded = end;
    onProgress?.({ partIndex: slot.part_index, uploadedBytes: uploaded, totalBytes: file.size });
  }
}

export async function uploadMultipartBundle({
  files,        // File[] length 5, in order part-00..part-04
  onProgress = () => {},
  signal,
}) {
  onProgress({ phase: "manifest" });
  const manifest = await fetchMultipartManifest();

  // 1. Local validation.
  if (files.length !== manifest.parts.length) {
    throw new Error(
      `Expected ${manifest.parts.length} parts, got ${files.length}`,
    );
  }
  // Sort by filename to enforce lexical order regardless of drop order.
  const orderedFiles = [...files].sort((a, b) => a.name.localeCompare(b.name));
  manifest.parts.forEach((p, i) => {
    if (orderedFiles[i].name !== p.name) {
      throw new Error(
        `Slot ${i}: expected file name ${p.name}, got ${orderedFiles[i].name}`,
      );
    }
    if (orderedFiles[i].size !== p.size) {
      throw new Error(
        `Slot ${i} (${p.name}): expected size ${p.size} bytes, got ${orderedFiles[i].size}`,
      );
    }
  });

  onProgress({ phase: "init" });
  const init = await api.post("/bundles/multipart/init", {
    parts: manifest.parts.map((p) => ({ name: p.name, size: p.size })),
  });
  const multipartId = init.data.multipart_id;
  const slots = init.data.slots;

  const partUploaded = new Array(slots.length).fill(0);
  const totalBytes = manifest.bundle_total_size;

  const emit = () => {
    const uploaded = partUploaded.reduce((a, b) => a + b, 0);
    onProgress({
      phase: "uploading",
      uploaded,
      total: totalBytes,
      partProgress: partUploaded.map((b, i) => ({
        partIndex: i,
        uploadedBytes: b,
        totalBytes: manifest.parts[i].size,
      })),
    });
  };
  emit();

  try {
    // 2. Upload parts sequentially. Parallel would race disk IO for
    //    2 GB across the same volume; sequential keeps memory + IO
    //    predictable.
    for (let i = 0; i < slots.length; i++) {
      await uploadOnePart(slots[i], orderedFiles[i], {
        signal,
        onProgress: (p) => {
          partUploaded[i] = p.uploadedBytes;
          emit();
        },
      });
    }

    // 3. Enqueue async finalize job. Server returns 202 with job_id
    //    almost immediately; the heavy reassembly + import happens in
    //    a background worker that survives Cloudflare / browser
    //    disconnects.
    onProgress({ phase: "assembling", uploaded: totalBytes, total: totalBytes });
    const assembleResp = await api.post(
      `/bundles/multipart/${multipartId}/assemble`,
      {},
      { timeout: 60_000 },
    );
    const jobId = assembleResp.data?.job_id;
    if (!jobId) {
      throw new Error("Server did not return a job id for the finalize job");
    }
    onProgress({
      phase: "job_enqueued",
      uploaded: totalBytes,
      total: totalBytes,
      jobId,
      job: assembleResp.data?.job,
    });

    // 4. Poll job status until COMPLETE or FAILED. Each poll is a
    //    lightweight GET that Cloudflare can serve in <100 ms, so
    //    no long-lived request stays open.
    const finalJob = await pollBundleJob({
      jobId,
      onUpdate: (job) =>
        onProgress({
          phase: "job_progress",
          uploaded: totalBytes,
          total: totalBytes,
          jobId,
          job,
        }),
      signal,
    });
    if (finalJob.status !== "COMPLETE") {
      const detail =
        finalJob.error_message || finalJob.stage_detail || "job failed";
      const err = new Error(detail);
      err.job = finalJob;
      throw err;
    }
    onProgress({
      phase: "done",
      uploaded: totalBytes,
      total: totalBytes,
      jobId,
      job: finalJob,
      result: finalJob.result_summary,
    });
    return finalJob.result_summary || {};
  } catch (err) {
    // NOTE: on client-side errors (network, abort) we DO NOT delete
    // the multipart session anymore. The parts survive Cloudflare
    // timeouts + browser reloads and the async job on the server
    // continues on its own. Local abort is user-explicit only.
    throw err;
  }
}

const DEFAULT_POLL_INTERVAL_MS = 2500;
const DEFAULT_POLL_TIMEOUT_MS = 60 * 60 * 1000; // 1h upper bound.

export async function fetchBundleJob(jobId) {
  const r = await api.get(`/bundles/jobs/${jobId}`);
  return r.data?.job;
}

export async function fetchActiveBundleJob() {
  const r = await api.get(`/bundles/jobs/active`);
  return r.data?.job;
}

export async function pollBundleJob({
  jobId,
  onUpdate = () => {},
  intervalMs = DEFAULT_POLL_INTERVAL_MS,
  timeoutMs = DEFAULT_POLL_TIMEOUT_MS,
  signal,
} = {}) {
  const start = Date.now();
  while (true) {
    if (signal?.aborted) throw new Error("aborted");
    let job;
    try {
      job = await fetchBundleJob(jobId);
    } catch (err) {
      // Transient network / Cloudflare hiccup while polling: sleep
      // and retry rather than surfacing as job failure.
      await new Promise((res) => setTimeout(res, intervalMs));
      if (Date.now() - start > timeoutMs) {
        throw new Error(`Polling job ${jobId} timed out`);
      }
      continue;
    }
    onUpdate(job);
    if (job.status === "COMPLETE" || job.status === "FAILED") {
      return job;
    }
    if (Date.now() - start > timeoutMs) {
      throw new Error(`Polling job ${jobId} timed out`);
    }
    await new Promise((res) => setTimeout(res, intervalMs));
  }
}
