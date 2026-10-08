const ACTIVE_STATUSES = new Set(["queued", "downloading", "completed", "organizing", "deleting"]);
const MEDIA_TYPES = new Set(["movie", "tv"]);

function isValidJob(job) {
  if (!job || typeof job !== "object" || Array.isArray(job)) return false;
  if (!Number.isSafeInteger(job.id) || job.id <= 0) return false;
  if (typeof job.title !== "string" || !MEDIA_TYPES.has(job.type)) return false;
  if (typeof job.status !== "string" || !ACTIVE_STATUSES.has(job.status)) return false;
  if (!Number.isFinite(job.progress)) return false;
  if (job.season !== null && !Number.isSafeInteger(job.season)) return false;
  if (job.episode !== null && !Number.isSafeInteger(job.episode)) return false;
  if (job.episode_set !== null && typeof job.episode_set !== "string") return false;
  return true;
}

export function normalizeActiveJobsSnapshot(payload) {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) return null;
  if (!Array.isArray(payload.jobs)) return null;
  if (!Number.isInteger(payload.attention_count) || payload.attention_count < 0) return null;
  if (!Number.isInteger(payload.history_count) || payload.history_count < 0) return null;

  const ids = new Set();
  const jobs = [];
  for (const job of payload.jobs) {
    if (!isValidJob(job) || ids.has(job.id)) {
      console.warn("Skipping invalid job in active-jobs snapshot", job);
      continue;
    }

    ids.add(job.id);
    jobs.push({
      id: job.id,
      type: job.type,
      title: job.title,
      season: job.season,
      episode: job.episode,
      episode_set: job.episode_set,
      status: job.status,
      progress: job.progress,
    });
  }

  return { jobs, attention_count: payload.attention_count, history_count: payload.history_count };
}
