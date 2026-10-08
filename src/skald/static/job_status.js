import { bindLiveStatus, createLiveSocket, websocketUrl } from "./live_socket.mjs";

(function () {
  const container = document.querySelector("[data-job-id]");
  if (!container || !window.WebSocket) return;

  const jobId = container.dataset.jobId;
  const currentStatus = container.dataset.jobStatus;
  const terminalStatuses = new Set(["organized", "needs_attention", "failed"]);
  let finished = terminalStatuses.has(currentStatus);
  let deleted = false;
  let liveSocket = null;

  const onState = bindLiveStatus(
    document.querySelector("[data-live-status]"),
    () => liveSocket && liveSocket.retry()
  );

  function markDeleted() {
    deleted = true;
    finished = true;
    const banner = document.querySelector("[data-job-deleted]");
    if (banner) banner.hidden = false;
    document
      .querySelectorAll("[data-job-actions] button, [data-job-recovery] button, [data-job-recovery] input")
      .forEach((el) => {
        el.disabled = true;
      });
    document.querySelectorAll("[data-job-actions] form, [data-job-recovery] form").forEach((form) => {
      form.removeAttribute("data-confirm");
      form.addEventListener("submit", (event) => event.preventDefault());
    });
    const status = document.querySelector("[data-live-status]");
    if (status) status.hidden = true;
  }

  liveSocket = createLiveSocket({
    url: websocketUrl(`/ws/jobs/${jobId}`),
    // A server-closed socket after a terminal state (or deletion) is expected.
    shouldReconnect: () => !finished,
    onState: (state) => {
      if (deleted) return;
      if (state === "closed" || (finished && state === "live")) {
        onState("closed");
        return;
      }
      onState(state);
    },
    onMessage(event) {
      let data;
      try {
        data = JSON.parse(event.data);
      } catch (err) {
        return;
      }
      if (!data || !data.status) return;
      if (data.status === "not_found") {
        markDeleted();
        return;
      }

      if (data.status !== currentStatus) {
        window.location.reload();
        return;
      }
      if (data.terminal === true || terminalStatuses.has(data.status)) finished = true;

      if (typeof data.progress === "number") {
        const fill = document.querySelector("[data-job-progress-fill]");
        const pct = document.querySelector("[data-job-progress-text]");
        const bar = document.querySelector("[data-job-progress]");
        const percent = Math.round(data.progress * 1000) / 10;
        const rounded = Math.round(data.progress * 100);
        if (fill) fill.style.width = `${percent}%`;
        if (pct) pct.textContent = `${rounded}%`;
        if (bar) {
          bar.setAttribute("aria-valuenow", String(rounded));
          bar.setAttribute("aria-valuetext", `${rounded}%`);
        }
      }
    },
  });
})();
