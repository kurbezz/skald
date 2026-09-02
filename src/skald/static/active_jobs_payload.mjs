const ACTIVE_STATUSES = new Set(["queued", "downloading", "completed", "organizing", "deleting"]);
const MEDIA_TYPES = new Set(["movie", "tv"]);

export function normalizeActiveJobsSnapshot(payload) {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) return null;
  if (!Array.isArray(payload.jobs)) return null;
  if (!Number.isInteger(payload.completed_count) || payload.completed_count < 0) return null;

  const ids = new Set();
  const jobs = [];
  for (const job of payload.jobs) {
    if (!job || typeof job !== "object" || Array.isArray(job)) return null;
    if (!Number.isSafeInteger(job.id) || job.id <= 0 || ids.has(job.id)) return null;
    if (typeof job.title !== "string" || !MEDIA_TYPES.has(job.type)) return null;
    if (typeof job.status !== "string" || !ACTIVE_STATUSES.has(job.status)) return null;
    if (!Number.isFinite(job.progress)) return null;

    ids.add(job.id);
    jobs.push({
      id: job.id,
      type: job.type,
      title: job.title,
      status: job.status,
      progress: job.progress,
    });
  }

  return { jobs, completed_count: payload.completed_count };
}
