// Shared reconnecting WebSocket helper with bounded exponential backoff.
export const MIN_DELAY_MS = 1000;
export const MAX_DELAY_MS = 30000;
export const MAX_ATTEMPTS = 10;

export function backoffDelay(attempt) {
  return Math.min(MAX_DELAY_MS, MIN_DELAY_MS * 2 ** attempt);
}

export const STATE_TEXT = {
  connecting: "Connecting…",
  live: "Live",
  reconnecting: "Reconnecting…",
  disconnected: "Disconnected —",
  closed: "Up to date",
};

/**
 * Reconnecting socket. States: connecting, live, reconnecting, disconnected, closed.
 * `shouldReconnect()` is consulted when the server closes the socket; returning
 * false (e.g. a terminal job state) yields the quiet "closed" state.
 */
export function createLiveSocket({
  url,
  onMessage,
  onState = () => {},
  shouldReconnect = () => true,
  WebSocketImpl = globalThis.WebSocket,
  setTimer = (fn, ms) => setTimeout(fn, ms),
  clearTimer = (id) => clearTimeout(id),
  hooks = typeof document !== "undefined",
}) {
  let socket = null;
  let attempt = 0;
  let timer = null;
  let stopped = false;

  function setState(state) {
    onState(state);
  }

  function connect() {
    if (timer !== null) {
      clearTimer(timer);
      timer = null;
    }
    if (stopped) return;
    let ws;
    try {
      ws = new WebSocketImpl(url);
    } catch (error) {
      scheduleRetry();
      return;
    }
    socket = ws;
    ws.addEventListener("open", () => {
      if (socket !== ws) return;
      attempt = 0;
      setState("live");
    });
    ws.addEventListener("message", (event) => {
      if (socket !== ws) return;
      onMessage(event);
    });
    ws.addEventListener("close", () => {
      if (socket !== ws) return;
      socket = null;
      if (!shouldReconnect()) {
        setState("closed");
        return;
      }
      scheduleRetry();
    });
    ws.addEventListener("error", () => {
      // A close event always follows; reconnect handling lives there.
    });
  }

  function scheduleRetry() {
    if (stopped || timer !== null) return;
    if (attempt >= MAX_ATTEMPTS) {
      setState("disconnected");
      return;
    }
    setState("reconnecting");
    const delay = backoffDelay(attempt);
    attempt += 1;
    timer = setTimer(() => {
      timer = null;
      connect();
    }, delay);
  }

  function isOpenOrConnecting() {
    return socket !== null && socket.readyState <= 1;
  }

  function retry() {
    if (stopped || isOpenOrConnecting()) return;
    attempt = 0;
    setState("reconnecting");
    connect();
  }

  function stop() {
    stopped = true;
    if (timer !== null) clearTimer(timer);
    timer = null;
    if (socket) socket.close();
  }

  if (hooks) {
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "visible") retry();
    });
    window.addEventListener("online", retry);
  }

  setState("connecting");
  connect();
  return { retry, stop };
}

/** Wire a `[data-live-status]` element; returns an `onState` callback. */
export function bindLiveStatus(element, retry) {
  if (!element) return () => {};
  const text = element.querySelector("[data-live-status-text]");
  const button = element.querySelector("[data-live-retry]");
  if (button) button.addEventListener("click", () => retry());
  return (state) => {
    element.dataset.liveState = state;
    if (text) text.textContent = STATE_TEXT[state] || "";
    if (button) button.hidden = state !== "disconnected";
  };
}

export function websocketUrl(path) {
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${protocol}//${window.location.host}${path}`;
}
