(function () {
  const container = document.querySelector("[data-job-id]");
  if (!container || !window.WebSocket) return;

  const jobId = container.dataset.jobId;
  let currentStatus = container.dataset.jobStatus;
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  const socket = new WebSocket(`${protocol}//${window.location.host}/ws/jobs/${jobId}`);

  socket.addEventListener("message", (event) => {
    let data;
    try {
      data = JSON.parse(event.data);
    } catch (err) {
      return;
    }
    if (!data.status || data.status === "not_found") return;

    if (data.status !== currentStatus) {
      window.location.reload();
      return;
    }

    if (typeof data.progress === "number") {
      const fill = document.querySelector(".progress-fill");
      const pct = document.querySelector(".progress-pct");
      const percent = Math.round(data.progress * 1000) / 10;
      if (fill) fill.style.width = `${percent}%`;
      if (pct) pct.textContent = `${Math.round(data.progress * 100)}%`;
    }
  });
})();
