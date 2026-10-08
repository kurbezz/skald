import { normalizeActiveJobsSnapshot } from "./active_jobs_payload.mjs";
import { bindLiveStatus, createLiveSocket, websocketUrl } from "./live_socket.mjs";
import { deleteConfirmText } from "./job_confirm.mjs";

(function () {
  const container = document.querySelector("[data-active-jobs]");
  if (!container || !window.WebSocket) return;

  const list = container.querySelector("[data-active-job-list]");
  const template = container.querySelector("[data-active-job-template]");
  const table = container.querySelector("[data-active-table]");
  const empty = container.querySelector("[data-active-empty]");
  const live = container.querySelector("[data-active-jobs-live]");
  const queueCount = document.querySelector("[data-queue-count]");
  const attentionCount = document.querySelector("[data-attention-count]");
  const historyCount = document.querySelector("[data-history-count]");
  const queueTab = document.querySelector('a[href="/jobs?tab=queue"]');
  if (!list || !template || !table || !empty) return;

  function labelFor(status) {
    return status === "completed" ? "downloaded" : String(status).replace(/_/g, " ");
  }

  function paddedNumber(value) {
    return Number.isSafeInteger(value) && value > 0 ? String(value).padStart(2, "0") : "?";
  }

  function episodeLabel(episodeSet, episode) {
    if (episodeSet) {
      try {
        const episodes = JSON.parse(episodeSet);
        if (Array.isArray(episodes) && episodes.every((value) => Number.isSafeInteger(value) && value > 0)) {
          const unique = [...new Set(episodes)].sort((a, b) => a - b);
          const ranges = [];
          let start = unique[0];
          let end = start;
          unique.slice(1).forEach((value) => {
            if (value === end + 1) {
              end = value;
              return;
            }
            ranges.push([start, end]);
            start = end = value;
          });
          ranges.push([start, end]);
          return ranges
            .map(([first, last]) => `E${paddedNumber(first)}${first === last ? "" : `-E${paddedNumber(last)}`}`)
            .join(",");
        }
      } catch (error) {
        // Fall back to the single-episode value for malformed legacy data.
      }
    }
    return `E${paddedNumber(episode)}`;
  }

  function tvEpisodeLabel(job) {
    return `S${paddedNumber(job.season)}${episodeLabel(job.episode_set, job.episode)}`;
  }

  function updateCount(element, count, label) {
    if (!element) return;
    element.textContent = String(count);
    element.setAttribute("aria-label", `${count} ${label}`);
  }

  function setProgress(row, title, progress) {
    const cell = row.querySelector("[data-job-progress]");
    const fill = row.querySelector("[data-job-progress-fill]");
    const text = row.querySelector("[data-job-progress-text]");
    const value = Math.min(1, Math.max(0, Number(progress) || 0));
    const percent = value * 100;
    const rounded = Math.round(percent);

    if (fill) fill.style.width = `${percent.toFixed(1)}%`;
    if (text) text.textContent = `${rounded}%`;
    if (cell) {
      cell.setAttribute("aria-label", `${title} download progress`);
      cell.setAttribute("aria-valuemin", "0");
      cell.setAttribute("aria-valuemax", "100");
      cell.setAttribute("aria-valuenow", String(rounded));
      cell.setAttribute("aria-valuetext", `${rounded}%`);
    }
  }

  function patchRow(row, job) {
    const id = String(job.id);
    const title = String(job.title || "");
    const type = String(job.type || "");
    const status = String(job.status || "");
    const badge = row.querySelector("[data-job-status-badge]");

    row.dataset.jobId = id;
    row.dataset.jobStatus = status;
    const idLabel = row.querySelector("[data-job-id-label]");
    const typeLabel = row.querySelector("[data-job-type]");
    const titleLink = row.querySelector("[data-job-title]");
    const episode = row.querySelector("[data-job-episode]");
    const statusLabel = row.querySelector("[data-job-status-label]");
    const deleteForm = row.querySelector("[data-job-delete-form]");
    if (idLabel) idLabel.textContent = `#${id}`;
    if (typeLabel) typeLabel.textContent = type;
    if (titleLink) {
      titleLink.textContent = title;
      titleLink.href = `/jobs/${id}`;
    }
    if (episode) {
      episode.hidden = type !== "tv";
      episode.textContent = type === "tv" ? tvEpisodeLabel(job) : "";
    }
    if (badge) {
      Array.from(badge.classList)
        .filter((className) => className.startsWith("badge-"))
        .forEach((className) => badge.classList.remove(className));
      badge.classList.add(`badge-${status}`);
    }
    if (statusLabel) statusLabel.textContent = labelFor(status);
    if (deleteForm) {
      deleteForm.action = `/jobs/${id}/delete`;
      deleteForm.dataset.confirm = deleteConfirmText(title, status);
    }
    setProgress(row, title, job.progress);
  }

  function createRow() {
    return template.content.firstElementChild.cloneNode(true);
  }

  function announce(added, removed, removedReasons) {
    if (!live || (!added.length && !removed.length)) return;
    const parts = [];
    if (added.length) parts.push(`${added.length} job${added.length === 1 ? "" : "s"} added to the queue.`);
    removedReasons.forEach((reason) => parts.push(reason));
    live.textContent = parts.join(" ");
  }

  function removalReason(title, historyBefore, historyNow, attentionBefore, attentionNow) {
    // A rising history count means the job was organized; a rising attention count means it needs you.
    if (attentionNow > attentionBefore) return `${title} needs attention.`;
    return historyNow > historyBefore ? `${title} organized.` : `${title} removed.`;
  }

  let lastHistory = Number(historyCount && historyCount.textContent) || 0;
  let lastAttention = Number(attentionCount && attentionCount.textContent) || 0;

  function reconcile(payload) {
    if (!payload || typeof payload !== "object" || !Array.isArray(payload.jobs)) return;
    const jobs = payload.jobs.filter((job) => job && typeof job === "object" && job.id != null);
    const rows = new Map(
      Array.from(list.querySelectorAll("[data-job-row]")).map((row) => [row.dataset.jobId, row])
    );
    const seen = new Set();
    const added = [];
    const ordered = [];

    jobs.forEach((job) => {
      const id = String(job.id);
      let row = rows.get(id);
      if (!row) {
        row = createRow();
        added.push(id);
      }
      patchRow(row, job);
      ordered.push(row);
      seen.add(id);
    });

    const removed = [];
    const removedReasons = [];
    const historyNow = Number.isFinite(payload.history_count) ? payload.history_count : lastHistory;
    const attentionNow = Number.isFinite(payload.attention_count) ? payload.attention_count : lastAttention;
    rows.forEach((row, id) => {
      if (!seen.has(id)) {
        const hadFocus = row.contains(document.activeElement);
        const link = row.querySelector("[data-job-title]");
        const title = (link && link.textContent) || `Job #${id}`;
        row.remove();
        removed.push(id);
        removedReasons.push(removalReason(title, lastHistory, historyNow, lastAttention, attentionNow));
        if (hadFocus && queueTab) queueTab.focus();
      }
    });

    // Only touch the DOM order where it actually differs, so focus and
    // in-progress interactions on untouched rows are preserved.
    ordered.forEach((row, index) => {
      if (list.children[index] !== row) list.insertBefore(row, list.children[index] || null);
    });

    updateCount(queueCount, jobs.length, "in queue");
    updateCount(attentionCount, attentionNow, "need attention");
    updateCount(historyCount, historyNow, "organized");
    lastAttention = attentionNow;
    lastHistory = historyNow;
    table.hidden = jobs.length === 0;
    empty.hidden = jobs.length !== 0;
    announce(added, removed, removedReasons);
  }

  let live_socket = null;
  const onState = bindLiveStatus(
    container.querySelector("[data-live-status]"),
    () => live_socket && live_socket.retry()
  );
  live_socket = createLiveSocket({
    url: websocketUrl(container.dataset.wsUrl),
    onState,
    onMessage(event) {
      let payload;
      try {
        payload = normalizeActiveJobsSnapshot(JSON.parse(event.data));
      } catch (error) {
        return;
      }
      if (!payload) return;
      reconcile(payload);
    },
  });
})();
