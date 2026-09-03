import { normalizeActiveJobsSnapshot } from "./active_jobs_payload.mjs";

(function () {
  const container = document.querySelector("[data-active-jobs]");
  if (!container || !window.WebSocket) return;

  const list = container.querySelector("[data-active-job-list]");
  const template = container.querySelector("[data-active-job-template]");
  const table = container.querySelector("[data-active-table]");
  const empty = container.querySelector("[data-active-empty]");
  const live = container.querySelector("[data-active-jobs-live]");
  const activeCount = document.querySelector("[data-active-count]");
  const completedCount = document.querySelector("[data-completed-count]");
  const activeTab = document.querySelector('a[href="/jobs?tab=active"]');
  if (!list || !template || !table || !empty) return;

  function labelFor(status) {
    return String(status).replace(/_/g, " ");
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
    if (deleteForm) deleteForm.action = `/jobs/${id}/delete`;
    setProgress(row, title, job.progress);
  }

  function createRow() {
    return template.content.firstElementChild.cloneNode(true);
  }

  function announce(added, removed) {
    if (!live || (!added.length && !removed.length)) return;
    const parts = [];
    if (added.length) parts.push(`${added.length} active job${added.length === 1 ? "" : "s"} added.`);
    if (removed.length) parts.push(`${removed.length} active job${removed.length === 1 ? "" : "s"} removed.`);
    live.textContent = parts.join(" ");
  }

  function reconcile(payload) {
    if (!payload || typeof payload !== "object" || !Array.isArray(payload.jobs)) return;
    const jobs = payload.jobs.filter((job) => job && typeof job === "object" && job.id != null);
    const rows = new Map(
      Array.from(list.querySelectorAll("[data-job-row]")).map((row) => [row.dataset.jobId, row])
    );
    const seen = new Set();
    const added = [];

    jobs.forEach((job) => {
      const id = String(job.id);
      let row = rows.get(id);
      if (!row) {
        row = createRow();
        added.push(id);
      }
      patchRow(row, job);
      list.append(row);
      seen.add(id);
    });

    const removed = [];
    rows.forEach((row, id) => {
      if (!seen.has(id)) {
        const hadFocus = row.contains(document.activeElement);
        row.remove();
        removed.push(id);
        if (hadFocus && activeTab) activeTab.focus();
      }
    });

    updateCount(activeCount, jobs.length, "active jobs");
    if (Number.isFinite(payload.completed_count)) {
      updateCount(completedCount, payload.completed_count, "completed jobs");
    }
    table.hidden = jobs.length === 0;
    empty.hidden = jobs.length !== 0;
    announce(added, removed);
  }

  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  const socket = new WebSocket(`${protocol}//${window.location.host}${container.dataset.wsUrl}`);
  socket.addEventListener("message", (event) => {
    let payload;
    try {
      payload = normalizeActiveJobsSnapshot(JSON.parse(event.data));
    } catch (error) {
      return;
    }
    if (!payload) return;
    reconcile(payload);
  });
})();
