(() => {
  const POLL_MS = 900;
  let activeRun = null;
  let stopping = false;

  const style = document.createElement("style");
  style.textContent = `
    #research-stop-control {
      position: fixed;
      right: 18px;
      bottom: 18px;
      z-index: 99999;
      display: none;
      align-items: center;
      gap: 10px;
      max-width: min(520px, calc(100vw - 36px));
      padding: 10px 12px;
      border: 1px solid #f1b7b7;
      border-radius: 10px;
      background: #fff7f7;
      box-shadow: 0 8px 28px rgba(15, 23, 42, .18);
      color: #7f1d1d;
      font: 600 13px/1.25 system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    #research-stop-control .research-stop-copy { min-width: 0; }
    #research-stop-control .research-stop-title { font-weight: 750; }
    #research-stop-control .research-stop-status {
      margin-top: 2px;
      color: #9f3a3a;
      font-size: 11px;
      font-weight: 500;
    }
    #research-stop-control button {
      flex: 0 0 auto;
      border: 1px solid #b42318;
      border-radius: 7px;
      background: #b42318;
      color: #fff;
      padding: 8px 11px;
      font: inherit;
      cursor: pointer;
    }
    #research-stop-control button:hover:not(:disabled) { background: #912018; }
    #research-stop-control button:disabled { opacity: .65; cursor: wait; }
  `;
  document.head.appendChild(style);

  const control = document.createElement("div");
  control.id = "research-stop-control";
  control.innerHTML = `
    <div class="research-stop-copy">
      <div class="research-stop-title">Research is running</div>
      <div class="research-stop-status">You can stop after the current source request finishes.</div>
    </div>
    <button type="button">Stop research</button>
  `;

  const title = control.querySelector(".research-stop-title");
  const status = control.querySelector(".research-stop-status");
  const button = control.querySelector("button");

  function mount() {
    if (!document.body.contains(control)) document.body.appendChild(control);
  }

  function render() {
    mount();
    if (!activeRun) {
      control.style.display = "none";
      stopping = false;
      button.disabled = false;
      button.textContent = "Stop research";
      return;
    }

    control.style.display = "flex";
    title.textContent = `Research run #${activeRun.id} is ${activeRun.status === "cancel_requested" ? "stopping" : "running"}`;
    if (activeRun.status === "cancel_requested" || stopping) {
      status.textContent = "Stop requested. The current source request is allowed to finish, then the run will stop.";
      button.disabled = true;
      button.textContent = "Stopping…";
    } else {
      status.textContent = "Stop is safe: remaining bidder/source tasks will stay not checked, not be treated as clean results.";
      button.disabled = false;
      button.textContent = "Stop research";
    }
  }

  async function refresh() {
    try {
      const response = await fetch("/api/runs/active", { cache: "no-store" });
      if (!response.ok) return;
      const data = await response.json();
      activeRun = data.item || null;
      render();
    } catch {
      // The normal app error/diagnostic surfaces handle backend connectivity.
    }
  }

  button.addEventListener("click", async () => {
    if (!activeRun || stopping) return;
    stopping = true;
    render();
    try {
      const response = await fetch(`/api/runs/${activeRun.id}/cancel`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ actor: "local-user" }),
      });
      if (!response.ok) throw new Error(`Stop request failed (${response.status})`);
      const data = await response.json();
      activeRun = data.item || activeRun;
    } catch (error) {
      stopping = false;
      status.textContent = error instanceof Error ? error.message : "Unable to stop the research run.";
    }
    render();
  });

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", mount, { once: true });
  } else {
    mount();
  }
  void refresh();
  window.setInterval(refresh, POLL_MS);
})();
