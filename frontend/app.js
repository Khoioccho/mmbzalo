/**
 * MMBZalo - Dashboard Client Logic
 * Handles dashboard auth, workspace switching, Zalo session control,
 * settings, contacts, campaigns, and async job polling.
 */

(() => {
  "use strict";

  const $ = (sel) => document.querySelector(sel);
  const $$ = (sel) => document.querySelectorAll(sel);
  const TERMINAL_JOB_STATUSES = new Set(["succeeded", "failed", "cancelled"]);

  const state = {
    auth: {
      ready: false,
      authenticated: false,
      user: null,
      workspaces: [],
      activeWorkspaceId: null,
    },
    authMode: "login",
    loginPollInterval: null,
    campaignProgressPollInterval: null,
    campaignProgressSeen: new Set(),
    jobPollers: new Map(),
    lastRenderedLoginState: null,
    campaignHistoryCache: [],
    manualPickerState: {
      contacts: [],
      filtered: [],
      loaded: false,
      loading: false,
      error: "",
      mode: "manual",
      /** Pending picks, only committed to the form on Confirm. */
      draft: new Map(),
    },
    campaignSelectionState: {
      selectedContacts: [],
    },
    contactsMeta: {
      stored_contact_count: 0,
      last_sync_status: null,
      last_sync_at: null,
    },
    zaloState: "idle",
    campaignRunning: false,
    campaignRun: null,
  };

  function esc(str) {
    const d = document.createElement("div");
    d.textContent = str ?? "";
    return d.innerHTML;
  }

  function appendLogEntry(containerId, message, type) {
    const logEl = $(`#${containerId}`);
    if (!logEl) return;
    const empty = logEl.querySelector(".log-empty");
    if (empty) empty.remove();
    const now = new Date().toLocaleTimeString("en-GB", { hour12: false });
    const entry = document.createElement("div");
    entry.className = "log-entry";
    entry.innerHTML = `<span class="log-time">${now}</span><span class="log-msg ${type ? `log-msg--${type}` : ""}">${esc(message)}</span>`;
    logEl.prepend(entry);
    while (logEl.children.length > 50) {
      logEl.lastChild.remove();
    }
  }

  /** Raw/technical output. Lives inside the collapsed "Technical details" block. */
  function log(message, type = "") {
    appendLogEntry("activity-log", message, type);
  }

  /** User-facing milestone ("Zalo connected", "212 contacts synced"). */
  function logEvent(message, type = "") {
    appendLogEntry("event-log", message, type);
  }

  /* ─── Toast notifications ──────────────────────────────────── */

  const TOAST_DEFAULT_TIMEOUT = {
    success: 4500,
    info: 4500,
    warning: 8000,
    error: 0,
    progress: 0,
  };

  const toastRegistry = new Map();

  function dismissToast(key) {
    const entry = toastRegistry.get(key);
    if (!entry) return;
    toastRegistry.delete(key);
    if (entry.timer) clearTimeout(entry.timer);
    entry.el.classList.add("toast--closing");
    entry.el.addEventListener("animationend", () => entry.el.remove(), { once: true });
    setTimeout(() => entry.el.remove(), 400);
  }

  function scheduleToastDismiss(key, timeout) {
    const entry = toastRegistry.get(key);
    if (!entry) return;
    if (entry.timer) clearTimeout(entry.timer);
    entry.timer = timeout > 0 ? setTimeout(() => dismissToast(key), timeout) : null;
  }

  function paintToast(el, type, title, message) {
    el.className = `toast toast--${type}`;
    el.innerHTML = `
      <span class="toast__accent"></span>
      <div class="toast__body">
        <div class="toast__title">${type === "progress" ? '<span class="toast__spinner"></span>' : ""}${esc(title)}</div>
        ${message ? `<div class="toast__message">${esc(message)}</div>` : ""}
      </div>
      <button class="toast__close" type="button" aria-label="Dismiss notification">
        <svg viewBox="0 0 20 20" fill="currentColor" width="14" height="14"><path fill-rule="evenodd" d="M4.293 4.293a1 1 0 0 1 1.414 0L10 8.586l4.293-4.293a1 1 0 1 1 1.414 1.414L11.414 10l4.293 4.293a1 1 0 0 1-1.414 1.414L10 11.414l-4.293 4.293a1 1 0 0 1-1.414-1.414L8.586 10 4.293 5.707a1 1 0 0 1 0-1.414z" clip-rule="evenodd"/></svg>
      </button>`;
  }

  /**
   * Show (or update, when `key` matches a live toast) an in-app notification.
   * Returns a handle so long-running jobs can mutate one toast instead of stacking duplicates.
   */
  function notify(type, title, message = "", options = {}) {
    const root = $("#toast-root");
    if (!root) return { update() {}, dismiss() {} };
    const key = options.key || `${type}:${title}:${message}`;
    const timeout = options.timeout !== undefined ? options.timeout : TOAST_DEFAULT_TIMEOUT[type] ?? 4500;

    let entry = toastRegistry.get(key);
    if (!entry) {
      const el = document.createElement("div");
      el.addEventListener("click", (event) => {
        if (event.target.closest(".toast__close")) dismissToast(key);
      });
      root.prepend(el);
      entry = { el, timer: null };
      toastRegistry.set(key, entry);
      while (root.children.length > 4) {
        root.lastChild.remove();
      }
    }
    paintToast(entry.el, type, title, message);
    scheduleToastDismiss(key, timeout);

    return {
      key,
      update(nextType, nextTitle, nextMessage = "", nextOptions = {}) {
        const live = toastRegistry.get(key);
        if (!live) {
          notify(nextType, nextTitle, nextMessage, { ...nextOptions, key });
          return;
        }
        paintToast(live.el, nextType, nextTitle, nextMessage);
        const nextTimeout = nextOptions.timeout !== undefined
          ? nextOptions.timeout
          : TOAST_DEFAULT_TIMEOUT[nextType] ?? 4500;
        scheduleToastDismiss(key, nextTimeout);
      },
      dismiss: () => dismissToast(key),
    };
  }

  /**
   * Turn a raw backend/transport error into a short user-facing sentence.
   * The untouched original is still written to the activity log.
   */
  /** Stack traces, driver dumps and DB errors — never show these verbatim. */
  const TECHNICAL_ERROR_PATTERN =
    /traceback|\.py[:\b]|\bat Object\.|\bat \w+ \(|psycopg2|sqlalchemy|playwright|selenium|webdriver|ECONNREFUSED|NoneType|Exception:|TimeoutError|<[a-z]+ object at 0x/i;

  function friendlyError(err, fallback = "Something went wrong. Please try again.") {
    const raw = (err && err.message ? err.message : String(err || "")).trim();
    if (!raw) return fallback;
    if (err && err.payload && err.payload.error_code && raw) return raw;
    if (err && err.status === 401) return "Your session expired. Sign in again.";
    if (err && err.status === 403) return "You do not have permission to do that in this workspace.";
    if (err && err.status === 409) return "That action conflicts with something already running.";
    if (err && err.status === 429) return "Too many requests. Wait a moment and try again.";
    if (err && err.status >= 500) return "The server hit an error. Please try again.";
    if (/failed to fetch|networkerror|load failed/i.test(raw)) {
      return "Cannot reach the server. Check that the backend is running.";
    }
    // Anything that looks like raw machine output stays in Technical details only.
    if (TECHNICAL_ERROR_PATTERN.test(raw) || raw.length > 160) return fallback;
    return raw;
  }

  /**
   * Run an async action with the button locked, so a double click cannot
   * queue the same job twice. Restores the original label afterwards.
   */
  async function withBusy(selector, busyLabel, action) {
    const btn = typeof selector === "string" ? $(selector) : selector;
    if (!btn) return action();
    if (btn.disabled) return undefined;
    const originalHtml = btn.innerHTML;
    btn.disabled = true;
    btn.dataset.busy = "true";
    btn.innerHTML = `<span class="spinner-inline"></span> ${esc(busyLabel)}`;
    try {
      return await action();
    } finally {
      delete btn.dataset.busy;
      btn.innerHTML = originalHtml;
      btn.disabled = false;
      if (typeof refreshCampaignActionState === "function") refreshCampaignActionState();
    }
  }

  function formatTimestamp(value) {
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return value;
    return date.toLocaleString("en-GB", {
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hour12: false,
    });
  }

  function normalizeSearchText(value) {
    return (value || "").trim().toLocaleLowerCase();
  }

  function formatLegacyProxyValue(settings) {
    if (settings.proxy_raw) return settings.proxy_raw;
    if (settings.proxy_address && settings.proxy_port) {
      return `${settings.proxy_address}:${settings.proxy_port}`;
    }
    return "";
  }

  function setAuthMessage(message, stateName = "neutral") {
    const el = $("#auth-message");
    el.textContent = message;
    if (stateName === "neutral") {
      el.removeAttribute("data-state");
    } else {
      el.dataset.state = stateName;
    }
  }

  function setAuthMode(mode) {
    state.authMode = mode;
    const isRegister = mode === "register";
    $("#auth-register-fields").style.display = isRegister ? "flex" : "none";
    $("#btn-auth-login").style.display = isRegister ? "none" : "inline-flex";
    $("#btn-auth-register").style.display = isRegister ? "inline-flex" : "none";
    $("#auth-password").autocomplete = isRegister ? "new-password" : "current-password";
    $$(".auth-mode__btn").forEach((btn) => {
      btn.classList.toggle("auth-mode__btn--active", btn.dataset.authMode === mode);
    });
    setAuthMessage(
      isRegister
        ? "Create an account and a private workspace for your Zalo session."
        : "Sign in to unlock workspace data and automation actions.",
      "neutral"
    );
  }

  function getActiveWorkspace() {
    return state.auth.workspaces.find((workspace) => workspace.workspace_id === state.auth.activeWorkspaceId) || null;
  }

  function stopAllJobWatchers() {
    for (const timer of state.jobPollers.values()) {
      clearInterval(timer);
    }
    state.jobPollers.clear();
  }

  function stopCampaignProgressPolling() {
    if (state.campaignProgressPollInterval) {
      clearInterval(state.campaignProgressPollInterval);
      state.campaignProgressPollInterval = null;
    }
  }

  function stopLoginPolling() {
    if (state.loginPollInterval) {
      clearInterval(state.loginPollInterval);
      state.loginPollInterval = null;
    }
  }

  function clearRuntimePollers() {
    stopAllJobWatchers();
    stopCampaignProgressPolling();
    stopLoginPolling();
  }

  function getToggle(groupId) {
    const active = $(`#${groupId} .toggle-btn--active`);
    return active ? active.dataset.value : "";
  }

  function setToggle(groupId, value) {
    const group = $(`#${groupId}`);
    if (!group) return;
    group.querySelectorAll(".toggle-btn").forEach((btn) => {
      btn.classList.toggle("toggle-btn--active", btn.dataset.value === value);
    });
  }

  function showTaskResult(elId, data, kind) {
    const el = $(`#${elId}`);
    el.style.display = "block";
    const isSuccess = (data.failed || 0) === 0;
    el.className = `task-result ${isSuccess ? "task-result--success" : "task-result--info"}`;
    const icon = isSuccess ? "OK" : "WARN";
    const list = data.results || [];
    const items = list.length > 0
      ? `<ul class="task-result__items">${
          list.map((row) => {
            const label = kind === "friend" ? row.phone : row.target;
            return row.success
              ? `<li class="success">OK ${esc(label)}</li>`
              : `<li class="fail">ERR ${esc(label)} - ${esc(row.error || "Unknown error")}</li>`;
          }).join("")
        }</ul>`
      : "";
    el.innerHTML = `
      <div class="task-result__title">${icon} ${esc(data.message || "Completed.")}</div>
      <div class="task-result__detail">Total: ${data.total || 0} | Sent: ${data.sent || 0} | Failed: ${data.failed || 0}</div>
      ${items}`;
  }

  function showTaskResultError(elId, message) {
    const el = $(`#${elId}`);
    el.style.display = "block";
    el.className = "task-result task-result--fail";
    el.innerHTML = `<div class="task-result__title">ERR Error</div><div class="task-result__detail">${esc(message)}</div>`;
  }

  function showTaskResultInfo(elId, title, message, items = []) {
    const el = $(`#${elId}`);
    el.style.display = "block";
    el.className = "task-result task-result--info";
    const list = items.length
      ? `<ul class="task-result__items">${items.map((item) => `<li>${esc(item)}</li>`).join("")}</ul>`
      : "";
    el.innerHTML = `<div class="task-result__title">${esc(title)}</div><div class="task-result__detail">${esc(message)}</div>${list}`;
  }

  function setContactsStatus(stateName, message) {
    const el = $("#contacts-status");
    el.className = `contacts-status contacts-status--${stateName}`;
    el.textContent = message;
  }

  function updateContactsMeta(data) {
    state.contactsMeta = {
      stored_contact_count: data.stored_contact_count || 0,
      last_sync_status: data.last_sync_status || null,
      last_sync_at: data.last_sync_at || null,
    };
    $("#contacts-meta-count").textContent = String(data.stored_contact_count || 0);
    $("#contacts-meta-status").textContent = data.last_sync_status || "Never synced";
    $("#contacts-meta-time").textContent = data.last_sync_at ? formatTimestamp(data.last_sync_at) : "No stored sync yet";
    renderAccountSyncCard();
    renderOnboarding();
  }

  function renderContacts(data) {
    $("#contacts-block").style.display = "block";
    $("#contacts-count-badge").textContent = `${data.stored_contact_count || data.contact_count || 0} stored`;
    $("#contacts-tbody").innerHTML = (data.contacts || []).map((contact, index) => `
      <tr>
        <td>${index + 1}</td>
        <td>${contact.avatar_url ? `<img src="${esc(contact.avatar_url)}" class="contact-avatar" alt="" />` : "-"}</td>
        <td class="contact-name">${esc(contact.name)}</td>
        <td>${contact.last_message ? esc(contact.last_message) : "-"}</td>
        <td>${contact.unread ? '<span class="unread-dot"></span>' : "-"}</td>
      </tr>
    `).join("");
  }

  function clearContactsDisplay(message) {
    $("#contacts-block").style.display = "none";
    updateContactsMeta({ stored_contact_count: 0, last_sync_status: "Never synced", last_sync_at: null });
    setContactsStatus("empty", message);
    $("#contacts-result").style.display = "none";
  }

  function formatDuration(seconds) {
    const total = Math.max(0, Math.round(seconds));
    if (total < 60) return `${total}s`;
    const minutes = Math.floor(total / 60);
    return `${minutes}m ${String(total % 60).padStart(2, "0")}s`;
  }

  function resetCampaignProgressUI(total = 0) {
    state.campaignProgressSeen = new Set();
    state.campaignRun = { total, startedAt: Date.now(), finishedAt: null };
    state.campaignRunning = true;
    $("#campaign-progress").style.display = "block";
    $("#campaign-results").style.display = "none";
    $("#campaign-results-list").innerHTML = "";
    $("#campaign-progress-list").innerHTML = '<div class="campaign-progress__item">Preparing campaign...</div>';
    $("#campaign-progress-list").classList.remove("campaign-progress__list--expanded");
    $("#btn-campaign-toggle-events").textContent = "View all activity";
    renderCampaignProgressSummary({ status: "running", total, sent: 0, failed: 0 });
    refreshCampaignActionState();
  }

  /** Summary-first header: status badge, progress bar, and the four key counters. */
  function renderCampaignProgressSummary(data) {
    const total = data.total || state.campaignRun?.total || 0;
    const sent = data.sent || 0;
    const failed = data.failed || 0;
    const done = sent + failed;
    const status = (data.status || "running").toLowerCase();
    const terminal = ["succeeded", "failed", "cancelled"].includes(status);

    if (terminal && state.campaignRun && !state.campaignRun.finishedAt) {
      state.campaignRun.finishedAt = Date.now();
    }

    const badge = $("#campaign-progress-status");
    const labels = {
      succeeded: ["Completed", "ok"],
      failed: ["Failed", "error"],
      cancelled: ["Cancelled", "error"],
      running: ["Running", "running"],
      queued: ["Queued", "running"],
    };
    const [label, variant] = labels[status] || ["Running", "running"];
    badge.textContent = label;
    badge.className = `status-badge status-badge--${variant}`;

    const percent = total > 0 ? Math.min(100, Math.round((done / total) * 100)) : terminal ? 100 : 0;
    const bar = $("#campaign-progress-bar");
    bar.style.width = `${percent}%`;
    bar.parentElement.className = `progress-bar${
      terminal ? (failed > 0 || status !== "succeeded" ? " progress-bar--failed" : " progress-bar--done") : ""
    }`;

    $("#campaign-stat-progress").textContent = `${done}/${total}`;
    $("#campaign-stat-sent").textContent = String(sent);
    const failedEl = $("#campaign-stat-failed");
    failedEl.textContent = String(failed);
    failedEl.className = `progress-stat__value${failed > 0 ? " progress-stat__value--fail" : ""}`;

    const started = state.campaignRun?.startedAt;
    const ended = state.campaignRun?.finishedAt || Date.now();
    $("#campaign-stat-duration").textContent = started ? formatDuration((ended - started) / 1000) : "0s";

    if (terminal) {
      state.campaignRunning = false;
      refreshCampaignActionState();
    }
  }

  function appendCampaignProgressEvent(event) {
    if (!event || state.campaignProgressSeen.has(event.sequence)) return;
    state.campaignProgressSeen.add(event.sequence);
    const list = $("#campaign-progress-list");
    const placeholder = list.querySelector(".campaign-progress__item");
    if (placeholder && placeholder.textContent === "Preparing campaign...") {
      placeholder.remove();
    }
    const item = document.createElement("div");
    item.className = `campaign-progress__item ${event.success === true ? "campaign-progress__item--success" : ""} ${event.success === false ? "campaign-progress__item--error" : ""}`;
    const route = event.route ? `<span class="campaign-progress__route">${esc(event.route.replaceAll("_", " "))}</span>` : "";
    item.innerHTML = `<span>${esc(event.message || "")}</span>${route}`;
    list.prepend(item);
    while (list.children.length > 60) {
      list.lastChild.remove();
    }
  }

  /** Compact per-recipient outcome; route metadata only shows when a row is expanded. */
  function renderCampaignResults(results) {
    const panel = $("#campaign-results");
    const list = $("#campaign-results-list");
    if (!results || !results.length) {
      panel.style.display = "none";
      return;
    }
    panel.style.display = "block";
    $("#campaign-results-count").textContent = `${results.length} recipient(s)`;
    list.innerHTML = results.map((item) => {
      const ok = item.success;
      const details = [
        item.route ? `Route: ${item.route.replaceAll("_", " ")}` : null,
        item.target ? `Target: ${item.target}` : null,
        item.duration ? `Duration: ${item.duration}` : null,
        item.error ? `Error: ${item.error}` : null,
      ].filter(Boolean);
      return `
        <details class="campaign-result-item campaign-result-item--${ok ? "ok" : "fail"}">
          <summary>
            <span>${esc(item.name || item.target || "Unknown")}</span>
            <span class="campaign-result-item__status">${ok ? "Sent" : "Failed"}</span>
          </summary>
          <div class="campaign-result-item__detail">${details.length ? esc(details.join(" · ")) : "No extra detail."}</div>
        </details>`;
    }).join("");
  }

  function buildContactsQuery(filters) {
    const params = new URLSearchParams();
    if (filters.search) params.set("search", filters.search);
    if (filters.unread_only) params.set("unread_only", "true");
    if (filters.identity_source && filters.identity_source !== "all") params.set("identity_source", filters.identity_source);
    if (filters.sort_by) params.set("sort_by", filters.sort_by);
    if (filters.sort_order) params.set("sort_order", filters.sort_order);
    if (filters.selected_ids && filters.selected_ids.length) params.set("selected_ids", filters.selected_ids.join(","));
    return params.toString();
  }

  async function parseJsonResponse(res) {
    const contentType = res.headers.get("content-type") || "";
    if (!contentType.includes("application/json")) {
      const text = await res.text();
      return text ? { detail: text } : null;
    }
    return res.json();
  }

  function handleSignedOut(message = "Sign in to unlock workspace data and automation actions.", silent = false) {
    clearRuntimePollers();
    state.auth.ready = true;
    state.auth.authenticated = false;
    state.auth.user = null;
    state.auth.workspaces = [];
    state.auth.activeWorkspaceId = null;
    state.lastRenderedLoginState = null;
    $("#workspace-select").innerHTML = "";
    $("#dashboard-session").style.display = "none";
    $("#header-workspace").style.display = "none";
    $("#btn-auth-logout").style.display = "none";
    $("#dashboard-session-user").textContent = "";
    $("#dashboard-session-meta").textContent = "";
    $("#auth-form-panel").style.display = "block";
    $("#auth-summary-panel").style.display = "none";
    setAuthMode(state.authMode || "login");
    setAuthMessage(message, silent ? "neutral" : "error");
    $("#login-info").style.display = "none";
    $("#login-name").textContent = "";
    $("#btn-login-start").disabled = true;
    $("#btn-login-stop").disabled = true;
    $("#account-sync-card").style.display = "none";
    $("#login-qr").style.display = "none";
    $("#login-preparing").style.display = "none";
    $("#btn-login-refresh-qr").style.display = "none";
    $("#login-qr-image").removeAttribute("src");
    state.zaloState = "signed_out";
    $$(".nav-item").forEach((btn) => {
      const unlocked = btn.dataset.module === "login";
      btn.disabled = !unlocked;
      btn.classList.toggle("nav-item--active", unlocked);
    });
    $$(".module").forEach((module) => {
      module.style.display = module.id === "mod-login" ? "block" : "none";
    });
    clearContactsDisplay("Sign in to load contacts for the active workspace.");
    $("#campaign-history-list").innerHTML = '<p class="field-hint">Sign in to load campaign history.</p>';
    $("#campaign-progress").style.display = "none";
    renderOnboarding();
    if (!silent) {
      log(message, "error");
      logEvent(message, "error");
    }
  }

  function applyAuthSession(data, messageState = "success") {
    state.auth.ready = true;
    state.auth.authenticated = true;
    state.auth.user = data.user;
    state.auth.workspaces = data.workspaces || [];
    state.auth.activeWorkspaceId = data.active_workspace_id || (state.auth.workspaces[0] ? state.auth.workspaces[0].workspace_id : null);

    $("#auth-form-panel").style.display = "none";
    $("#auth-summary-panel").style.display = "block";
    $("#new-workspace-name").value = "";
    $("#btn-auth-logout").style.display = "inline-flex";
    $("#dashboard-session").style.display = "flex";
    $("#header-workspace").style.display = "flex";
    $("#dashboard-session-user").textContent = data.user.display_name || data.user.email;

    const workspaceSelect = $("#workspace-select");
    workspaceSelect.innerHTML = "";
    (state.auth.workspaces || []).forEach((workspace) => {
      const option = document.createElement("option");
      option.value = workspace.workspace_id;
      option.textContent = `${workspace.name} (${workspace.role})`;
      option.selected = workspace.workspace_id === state.auth.activeWorkspaceId;
      workspaceSelect.appendChild(option);
    });

    const activeWorkspace = getActiveWorkspace();
    $("#dashboard-session-meta").textContent = activeWorkspace ? activeWorkspace.name : "No workspace selected";
    $("#auth-summary-user").textContent = `${data.user.display_name} (${data.user.email})`;
    $("#auth-summary-workspace").textContent = activeWorkspace ? activeWorkspace.name : "-";
    $("#auth-summary-role").textContent = activeWorkspace ? activeWorkspace.role : "-";
    setAuthMessage(`Signed in as ${data.user.email}.`, messageState);

    $$(".nav-item").forEach((btn) => {
      btn.disabled = false;
    });
    renderOnboarding();
  }

  async function apiRequest(path, options = {}) {
    const { allowUnauthorized = false, body, headers = {}, ...rest } = options;
    const init = {
      credentials: "same-origin",
      ...rest,
      headers: { ...headers },
    };
    if (body !== undefined) {
      init.headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(body);
    }
    const res = await fetch(path, init);
    const data = await parseJsonResponse(res);
    if (res.status === 401 && allowUnauthorized) {
      return null;
    }
    if (!res.ok) {
      const message = (data && typeof data.message === "string" && data.message)
        || (data && typeof data.detail === "string" && data.detail)
        || "Request failed.";
      if (res.status === 401) {
        handleSignedOut("Your session expired. Sign in again.");
      }
      const error = new Error(message);
      error.status = res.status;
      error.payload = data;
      error.errorCode = data && data.error_code;
      error.retryable = Boolean(data && data.retryable);
      throw error;
    }
    return data;
  }

  async function checkHealth() {
    try {
      const health = await apiRequest("/api/health", { allowUnauthorized: true });
      let label = "API Online";
      let dotClass = "status-dot status-dot--ok";
      try {
        const readiness = await apiRequest("/api/readiness", { allowUnauthorized: true });
        if (readiness && readiness.worker && readiness.worker !== "ok") {
          label = `API Ready / Worker ${readiness.worker}`;
          dotClass = "status-dot";
        } else {
          label = "API Ready";
        }
      } catch {
        label = health?.status === "ok" ? "API Online" : "Checking...";
      }
      $("#health-badge .status-dot").className = dotClass;
      $("#health-badge span:last-child").textContent = label;
    } catch {
      $("#health-badge .status-dot").className = "status-dot status-dot--err";
      $("#health-badge span:last-child").textContent = "API Offline";
    }
  }

  async function bootstrapSession() {
    const session = await apiRequest("/api/auth/me", { allowUnauthorized: true });
    if (!session) {
      handleSignedOut("Sign in to unlock workspace data and automation actions.", true);
      return;
    }
    applyAuthSession(session, "success");
    await hydrateWorkspace();
  }

  async function hydrateWorkspace() {
    if (!state.auth.authenticated) return;
    const activeWorkspace = getActiveWorkspace();
    $("#dashboard-session-meta").textContent = activeWorkspace ? activeWorkspace.name : "No workspace selected";
    $("#auth-summary-workspace").textContent = activeWorkspace ? activeWorkspace.name : "-";
    $("#auth-summary-role").textContent = activeWorkspace ? activeWorkspace.role : "-";
    clearRuntimePollers();
    renderCampaignSelection();
    setMessageMode("manual");
    await Promise.allSettled([
      loadSettings(),
      loadStoredContacts(),
      loadCampaignHistory(),
      refreshLoginStatus(),
    ]);
  }

  async function refreshLoginStatus() {
    if (!state.auth.authenticated) {
      $("#btn-login-start").disabled = true;
      $("#btn-login-stop").disabled = true;
      return;
    }
    try {
      const data = await apiRequest("/api/login/status");
      renderZaloLoginState(data);
    } catch (err) {
      renderZaloLoginState({
        state: "error",
        message: err.message,
      });
    }
  }

  function setOnboardStep(step, status, subtitle) {
    const el = $(`.onboard-step[data-step="${step}"]`);
    if (!el) return;
    el.classList.toggle("onboard-step--done", status === "done");
    el.classList.toggle("onboard-step--active", status === "active");
    const sub = el.querySelector(".onboard-step__sub");
    if (sub && subtitle) sub.textContent = subtitle;
  }

  /**
   * Paint the 5-step onboarding strip from state we already hold.
   * The first step that is not complete becomes the active one.
   */
  function renderOnboarding() {
    const signedIn = state.auth.authenticated;
    const workspace = getActiveWorkspace();
    const zaloReady = state.zaloState === "authenticated";
    const contactCount = state.contactsMeta.stored_contact_count || 0;
    const contactsReady = contactCount > 0;

    const steps = [
      {
        key: "account",
        done: signedIn,
        subtitle: signedIn
          ? (state.auth.user?.display_name || state.auth.user?.email || "Signed in")
          : "Sign in or sign up",
      },
      {
        key: "workspace",
        done: signedIn && Boolean(workspace),
        subtitle: workspace ? workspace.name : "Pick a workspace",
      },
      {
        key: "zalo",
        done: zaloReady,
        subtitle: zaloReady
          ? "Connected"
          : state.zaloState === "waiting_qr"
            ? "Waiting for scan"
            : "Scan the QR code",
      },
      {
        key: "contacts",
        done: contactsReady,
        subtitle: contactsReady ? `${contactCount} contacts` : "Sync your contact list",
      },
      {
        key: "ready",
        done: zaloReady && contactsReady,
        subtitle: zaloReady && contactsReady ? "You can send messages" : "Start messaging",
      },
    ];

    const firstPending = steps.findIndex((step) => !step.done);
    steps.forEach((step, index) => {
      const status = step.done ? "done" : index === firstPending ? "active" : "pending";
      setOnboardStep(step.key, status, step.subtitle);
    });
  }

  function setLoginBadge(label, variant) {
    const badge = $("#login-status-badge");
    badge.textContent = label;
    badge.className = `status-badge${variant ? ` status-badge--${variant}` : ""}`;
  }

  function renderZaloLoginState(data) {
    const icon = $("#login-state-icon");
    const text = $("#login-state-text");
    const detail = $("#login-state-detail");
    const card = $("#login-status-card");
    const btnStart = $("#btn-login-start");
    const btnStop = $("#btn-login-stop");
    const btnRefresh = $("#btn-login-refresh-qr");
    const syncCard = $("#account-sync-card");
    const qrPanel = $("#login-qr");
    const qrImage = $("#login-qr-image");
    const preparing = $("#login-preparing");
    const hasQrImage = typeof data.qr_image_base64 === "string"
      && data.qr_image_base64.startsWith("data:image/png;base64,");

    qrPanel.style.display = hasQrImage ? "block" : "none";
    if (hasQrImage) {
      qrImage.src = data.qr_image_base64;
    } else {
      qrImage.removeAttribute("src");
    }
    preparing.style.display = "none";
    btnRefresh.style.display = "none";
    card.dataset.state = data.state || "idle";

    if (!state.auth.authenticated) {
      text.textContent = "Dashboard sign-in required";
      detail.textContent = "Sign in first, then manage the workspace browser session.";
      icon.className = "login-state";
      setLoginBadge("Signed out", "");
      btnStart.disabled = true;
      btnStop.disabled = true;
      syncCard.style.display = "none";
      $("#login-info").style.display = "none";
      state.zaloState = "signed_out";
      renderOnboarding();
      return;
    }

    const loginInfo = $("#login-info");
    const loginName = $("#login-name");
    if (data.profile_name) {
      loginInfo.style.display = "flex";
      loginName.textContent = data.profile_name;
    } else {
      loginInfo.style.display = "none";
      loginName.textContent = "";
    }

    state.zaloState = data.state || "idle";

    if (data.state === "authenticated") {
      text.textContent = "Zalo connected";
      detail.textContent = data.profile_name
        ? `Signed in as ${data.profile_name}. Keep one Zalo account per workspace to avoid session conflicts.`
        : `${data.message || "Workspace session is ready."} Keep one Zalo account per workspace.`;
      icon.className = "login-state login-state--ok";
      setLoginBadge("Connected", "ok");
      btnStart.disabled = true;
      btnStop.disabled = false;
      syncCard.style.display = "block";
      if (state.lastRenderedLoginState !== "authenticated") {
        logEvent(`Zalo connected${data.profile_name ? ` as ${data.profile_name}` : ""}.`, "success");
        notify("success", "Zalo connected", data.profile_name ? `Signed in as ${data.profile_name}.` : "", { key: "zalo-connected" });
        log(`Workspace Zalo session ready${data.profile_name ? ` for ${data.profile_name}` : ""}.`, "success");
      }
      state.lastRenderedLoginState = "authenticated";
      renderOnboarding();
      return;
    }

    syncCard.style.display = "none";

    if (data.state === "waiting_qr") {
      text.textContent = hasQrImage ? "Scan to connect" : "Preparing QR code";
      detail.textContent = hasQrImage
        ? (data.message || "Open Zalo on your phone and scan the code below.")
        : "Starting the browser session. This page updates automatically.";
      icon.className = "login-state login-state--waiting";
      setLoginBadge(hasQrImage ? "Waiting for scan" : "Preparing", "waiting");
      preparing.style.display = hasQrImage ? "none" : "flex";
      btnStart.disabled = true;
      btnStop.disabled = false;
      btnRefresh.style.display = hasQrImage ? "inline-flex" : "none";
      state.lastRenderedLoginState = "waiting_qr";
      renderOnboarding();
      return;
    }

    if (data.state === "expired" || data.state === "error") {
      const expired = data.state === "expired";
      text.textContent = expired ? "Session expired" : "Connection failed";
      detail.textContent = expired
        ? "The Zalo session is no longer valid. Connect again to continue."
        : friendlyError({ message: data.message }, "The Zalo session could not be started.");
      icon.className = "login-state login-state--err";
      setLoginBadge(expired ? "Expired" : "Error", "error");
      btnStart.disabled = false;
      btnStop.disabled = false;
      if (state.lastRenderedLoginState !== data.state) {
        logEvent(expired ? "Zalo session expired." : "Zalo connection failed.", "error");
        notify(
          expired ? "warning" : "error",
          expired ? "Zalo session expired" : "Zalo connection failed",
          expired ? "Connect again to keep sending messages." : friendlyError({ message: data.message }),
          { key: "zalo-problem" }
        );
        if (data.message) log(data.message, "error");
      }
      state.lastRenderedLoginState = data.state;
      renderOnboarding();
      return;
    }

    text.textContent = "Not connected";
    detail.textContent = data.message || 'Click "Connect Zalo" to link your Zalo account.';
    icon.className = "login-state";
    setLoginBadge("Not connected", "");
    btnStart.disabled = false;
    btnStop.disabled = true;
    state.lastRenderedLoginState = "idle";
    renderOnboarding();
  }

  function renderAccountSyncCard() {
    const meta = state.contactsMeta || {};
    const count = meta.stored_contact_count || 0;
    const badge = $("#account-sync-badge");
    const status = (meta.last_sync_status || "").toLowerCase();

    $("#account-sync-count").textContent = String(count);
    $("#account-sync-time").textContent = meta.last_sync_at ? formatTimestamp(meta.last_sync_at) : "Never";

    const variants = {
      succeeded: ["Synced", "ok"],
      running: ["Syncing", "running"],
      queued: ["Queued", "running"],
      failed: ["Failed", "error"],
      cancelled: ["Cancelled", "error"],
    };
    const [label, variant] = variants[status] || (count > 0 ? ["Synced", "ok"] : ["Never synced", ""]);
    badge.textContent = label;
    badge.className = `status-badge${variant ? ` status-badge--${variant}` : ""}`;

    const btn = $("#btn-login-sync-contacts");
    btn.textContent = status === "failed" ? "Retry Sync" : count > 0 ? "Sync Again" : "Sync Contacts";
    $("#btn-account-view-contacts").style.display = count > 0 ? "inline-flex" : "none";
  }

  function startLoginPolling() {
    stopLoginPolling();
    state.loginPollInterval = setInterval(async () => {
      try {
        const data = await apiRequest("/api/login/status");
        renderZaloLoginState(data);
        if (data.state === "authenticated" || data.state === "idle" || data.state === "error" || data.state === "expired") {
          stopLoginPolling();
        }
      } catch (err) {
        stopLoginPolling();
        log(`Login status error: ${err.message}`, "error");
      }
    }, 1500);
  }

  function latestJobMessage(job) {
    const events = job.events || [];
    const lastEvent = events.length ? events[events.length - 1] : null;
    return (lastEvent && lastEvent.message) || (job.result && job.result.message) || `${job.type} ${job.status}`;
  }

  function extractJobError(job) {
    const message = latestJobMessage(job);
    if (job.result && typeof job.result.message === "string" && job.result.message) {
      return job.result.message;
    }
    return message;
  }

  async function pollJob(jobId, handlers = {}) {
    const key = String(jobId);
    const runTick = async () => {
      const job = await apiRequest(`/api/jobs/${jobId}`);
      if (handlers.onUpdate) {
        handlers.onUpdate(job);
      }
      if (TERMINAL_JOB_STATUSES.has(job.status)) {
        const timer = state.jobPollers.get(key);
        if (timer) {
          clearInterval(timer);
          state.jobPollers.delete(key);
        }
        if (handlers.onComplete) {
          handlers.onComplete(job);
        }
      }
      return job;
    };

    const wrappedTick = () => {
      runTick().catch((err) => {
        const timer = state.jobPollers.get(key);
        if (timer) {
          clearInterval(timer);
          state.jobPollers.delete(key);
        }
        if (handlers.onError) {
          handlers.onError(err);
        } else {
          log(`Job poll error: ${err.message}`, "error");
        }
      });
    };

    if (state.jobPollers.has(key)) {
      clearInterval(state.jobPollers.get(key));
      state.jobPollers.delete(key);
    }
    const firstJob = await runTick();
    if (firstJob && TERMINAL_JOB_STATUSES.has(firstJob.status)) {
      return;
    }
    const timer = setInterval(wrappedTick, handlers.intervalMs || 1500);
    state.jobPollers.set(key, timer);
  }

  function showPendingJob(elId, job, label) {
    const eventMessages = (job.events || []).slice(-3).map((event) => `${event.event_type}: ${event.message}`);
    showTaskResultInfo(
      elId,
      `${label} ${job.status.toUpperCase()}`,
      latestJobMessage(job),
      eventMessages
    );
  }

  async function queueBackgroundJob(config) {
    const submission = await config.submit();
    if (config.onQueued) {
      config.onQueued(submission);
    }
    log(`${config.label} job queued.`, "success");
    await pollJob(submission.job_id, {
      onUpdate: (job) => {
        if (config.onUpdate) config.onUpdate(job, submission);
      },
      onComplete: async (job) => {
        if (config.onComplete) await config.onComplete(job, submission);
      },
      onError: (err) => {
        if (config.onError) {
          config.onError(err, submission);
        } else {
          log(`${config.label} job error: ${err.message}`, "error");
        }
      },
    });
    return submission;
  }

  function renderMessageJobResult(job) {
    if (job.status === "succeeded") {
      const result = job.result || {};
      showTaskResult("msg-result", result, "message");
      const sent = result.sent || 0;
      const failed = result.failed || 0;
      logEvent(`Messages sent: ${sent} sent, ${failed} failed.`, failed > 0 ? "error" : "success");
      notify(
        failed > 0 ? "warning" : "success",
        failed > 0 ? "Sent with failures" : "Messages sent",
        `${sent} sent, ${failed} failed.`,
        { key: "manual-send" }
      );
      log(result.message || "Message job completed.", "success");
      return;
    }
    if (job.status === "failed" || job.status === "cancelled") {
      const raw = extractJobError(job);
      showTaskResultError("msg-result", friendlyError({ message: raw }, "Messages could not be sent."));
      logEvent("Message sending failed.", "error");
      notify("error", "Sending failed", friendlyError({ message: raw }), { key: "manual-send" });
      log(`Messaging error: ${raw}`, "error");
      return;
    }
    showPendingJob("msg-result", job, "Messaging");
  }

  function renderFriendJobResult(job) {
    if (job.status === "succeeded") {
      const result = job.result || {};
      showTaskResult("friend-result", result, "friend");
      const failed = result.failed || 0;
      logEvent(`Friend requests: ${result.sent || 0} sent, ${failed} failed.`, failed > 0 ? "error" : "success");
      notify(
        failed > 0 ? "warning" : "success",
        "Friend requests finished",
        `${result.sent || 0} sent, ${failed} failed.`,
        { key: "friend-send" }
      );
      log(result.message || "Friend request job completed.", failed > 0 ? "error" : "success");
      return;
    }
    if (job.status === "failed" || job.status === "cancelled") {
      const raw = extractJobError(job);
      showTaskResultError("friend-result", friendlyError({ message: raw }, "Friend requests failed."));
      notify("error", "Friend requests failed", friendlyError({ message: raw }), { key: "friend-send" });
      log(`Friend request error: ${raw}`, "error");
      return;
    }
    showPendingJob("friend-result", job, "Friend request");
  }

  function renderGroupJobResult(job) {
    const el = $("#group-result");
    el.style.display = "block";
    if (job.status === "succeeded") {
      const success = Boolean(job.result && job.result.success);
      el.className = `task-result ${success ? "task-result--success" : "task-result--fail"}`;
      el.innerHTML = `<div class="task-result__title">${success ? "OK Message Sent" : "ERR Failed"}</div><div class="task-result__detail">${esc(job.result?.message || "Group job completed.")}</div>`;
      logEvent(success ? "Group message sent." : "Group message failed.", success ? "success" : "error");
      notify(
        success ? "success" : "error",
        success ? "Group message sent" : "Group message failed",
        job.result?.message || "",
        { key: "group-send" }
      );
      log(job.result?.message || "Group job completed.", success ? "success" : "error");
      return;
    }
    if (job.status === "failed" || job.status === "cancelled") {
      const raw = extractJobError(job);
      showTaskResultError("group-result", friendlyError({ message: raw }, "Group message failed."));
      notify("error", "Group message failed", friendlyError({ message: raw }), { key: "group-send" });
      log(`Group error: ${raw}`, "error");
      return;
    }
    showPendingJob("group-result", job, "Group message");
  }

  function applyContactSyncResult(result) {
    updateContactsMeta(result);
    $("#contacts-result").style.display = "none";
    if (result.contacts && result.contacts.length > 0) {
      renderContacts(result);
      setContactsStatus("success", result.message || `Loaded ${result.contact_count || result.contacts.length} stored contact(s).`);
    } else {
      $("#contacts-block").style.display = "none";
      setContactsStatus("empty", result.message || "No stored contacts are available for this workspace.");
    }
  }

  function renderContactSyncJob(job) {
    if (job.status === "succeeded") {
      const result = job.result || {};
      applyContactSyncResult(result);
      showTaskResultInfo("contacts-result", "Contact Sync Completed", result.message || "Contact sync completed.");
      const count = result.stored_contact_count || result.contact_count || (result.contacts || []).length;
      logEvent(`${count} contacts synced.`, "success");
      notify("success", "Contact sync completed", `${count} contact${count === 1 ? "" : "s"} available.`, { key: "contact-sync" });
      log(result.message || "Contact sync completed.", "success");
      return;
    }
    if (job.status === "failed" || job.status === "cancelled") {
      const raw = extractJobError(job);
      const readable = friendlyError({ message: raw }, "Check the Zalo connection, then try again.");
      $("#contacts-block").style.display = "none";
      setContactsStatus("error", readable);
      showTaskResultError("contacts-result", readable);
      state.contactsMeta.last_sync_status = "failed";
      renderAccountSyncCard();
      logEvent("Contact sync failed.", "error");
      notify("error", "Contact sync failed", readable, { key: "contact-sync" });
      log(`Contact sync error: ${raw}`, "error");
      return;
    }
    setContactsStatus("progress", latestJobMessage(job));
    showPendingJob("contacts-result", job, "Contact sync");
  }

  /** Shared by the Account onboarding card and the Contacts tab button. */
  async function runContactSync(buttonSelector) {
    await withBusy(buttonSelector, "Syncing...", async () => {
      state.contactsMeta.last_sync_status = "running";
      renderAccountSyncCard();
      notify("progress", "Syncing contacts", "Importing your Zalo friend list...", { key: "contact-sync" });
      await queueBackgroundJob({
        label: "Contact sync",
        submit: () => apiRequest("/api/contacts/sync", { method: "POST" }),
        onQueued: (submission) => {
          $("#contacts-block").style.display = "none";
          setContactsStatus("progress", `Sync job ${submission.job_id} queued. Waiting for the worker.`);
          showTaskResultInfo("contacts-result", "Contact Sync Queued", `Job ${submission.job_id} is waiting for the worker.`);
          logEvent("Contact sync started.");
        },
        onUpdate: (job) => renderContactSyncJob(job),
        onComplete: (job) => renderContactSyncJob(job),
        onError: (err) => {
          $("#contacts-block").style.display = "none";
          state.contactsMeta.last_sync_status = "failed";
          renderAccountSyncCard();
          const readable = friendlyError(err, "Check the Zalo connection, then try again.");
          setContactsStatus("error", readable);
          showTaskResultError("contacts-result", readable);
          notify("error", "Contact sync failed", readable, { key: "contact-sync" });
          log(`Contact sync error: ${err.message}`, "error");
        },
      });
    });
    // withBusy restores the original label on exit, so repaint the card afterwards
    // to keep the "Retry Sync" state visible.
    renderAccountSyncCard();
  }

  /**
   * Campaign outcome renders into the campaign panel only — the progress list
   * already carries the per-event narrative, so nothing is repeated in #msg-result.
   */
  function renderCampaignExecutionJob(job) {
    if (job.status === "succeeded") {
      const result = job.result || {};
      const campaign = result.campaign || {};
      // Fall back to the last polled progress so a sparse job result cannot zero the summary.
      const progress = state.campaignRun?.lastProgress || {};
      const sent = result.sent ?? campaign.sent_count ?? progress.sent ?? 0;
      const failed = result.failed ?? campaign.failed_count ?? progress.failed ?? 0;
      const total = result.total ?? campaign.matched_count ?? progress.total ?? sent + failed;
      renderCampaignProgressSummary({ status: "succeeded", total, sent, failed });
      renderCampaignResults(result.results?.length ? result.results : campaign.results || []);
      logEvent(`Campaign completed: ${sent} sent, ${failed} failed.`, failed > 0 ? "error" : "success");
      notify(
        failed > 0 ? "warning" : "success",
        "Campaign completed",
        `${sent} sent, ${failed} failed.`,
        { key: "campaign-run" }
      );
      log(result.message || "Campaign completed.", failed > 0 ? "error" : "success");
      return;
    }
    if (job.status === "failed" || job.status === "cancelled") {
      const raw = extractJobError(job);
      renderCampaignProgressSummary({ status: job.status, total: state.campaignRun?.total || 0, sent: 0, failed: 0 });
      logEvent("Campaign failed.", "error");
      notify("error", "Campaign failed", friendlyError({ message: raw }), { key: "campaign-run" });
      log(`Campaign execution error: ${raw}`, "error");
    }
  }

  async function pollCampaignProgress(campaignId) {
    const data = await apiRequest(`/api/campaigns/${campaignId}/progress`);
    $("#campaign-progress").style.display = "block";
    if (state.campaignRun) state.campaignRun.lastProgress = data;
    renderCampaignProgressSummary(data);
    (data.events || []).forEach((event) => {
      appendCampaignProgressEvent(event);
      if (event.message) {
        // Raw event stream stays in Technical details; the summary above is the headline.
        log(event.message, event.level === "error" || event.success === false ? "error" : "");
      }
    });
    if (data.status === "succeeded" || data.status === "failed" || data.status === "cancelled") {
      stopCampaignProgressPolling();
    }
    return data;
  }

  function startCampaignProgressPolling(campaignId, total = 0) {
    stopCampaignProgressPolling();
    resetCampaignProgressUI(total);
    pollCampaignProgress(campaignId).catch(() => {});
    state.campaignProgressPollInterval = setInterval(() => {
      pollCampaignProgress(campaignId).catch((err) => {
        log(`Campaign progress error: ${err.message}`, "error");
        stopCampaignProgressPolling();
      });
    }, 1500);
  }

  function getManualTargetLines() {
    return $("#msg-targets").value
      .split("\n")
      .map((line) => line.trim())
      .filter(Boolean);
  }

  function pickerContactKey(contact) {
    return state.manualPickerState.mode === "campaign"
      ? contact.identity_key || ""
      : normalizeSearchText(contact.name);
  }

  function contactIsSelectedForPicker(contact) {
    return state.manualPickerState.draft.has(pickerContactKey(contact));
  }

  function updatePickerSelectedCount() {
    const size = state.manualPickerState.draft.size;
    $("#contact-picker-selected").textContent = `${size} selected`;
    $("#btn-picker-confirm").disabled = size === 0;
  }

  function togglePickerContact(contact, forceOn) {
    const key = pickerContactKey(contact);
    if (!key) return;
    const draft = state.manualPickerState.draft;
    const shouldSelect = forceOn !== undefined ? forceOn : !draft.has(key);
    if (shouldSelect) {
      draft.set(key, contact);
    } else {
      draft.delete(key);
    }
    updatePickerSelectedCount();
  }

  function setContactPickerStatus(message, type = "neutral") {
    const el = $("#contact-picker-status");
    el.textContent = message;
    el.dataset.state = type;
  }

  function filterManualPickerContacts() {
    const query = normalizeSearchText($("#contact-picker-search").value);
    state.manualPickerState.filtered = state.manualPickerState.contacts.filter((contact) => !query || normalizeSearchText(contact.name).includes(query));
  }

  function renderManualPickerList() {
    const list = $("#contact-picker-list");
    if (state.manualPickerState.loading) {
      list.innerHTML = '<div class="picker-empty">Loading stored contacts...</div>';
      return;
    }
    if (state.manualPickerState.error) {
      list.innerHTML = `<div class="picker-empty">${esc(state.manualPickerState.error)}</div>`;
      return;
    }
    if (!state.manualPickerState.contacts.length) {
      list.innerHTML = '<div class="picker-empty">No stored contacts yet. Sync contacts first in the Contacts tab.</div>';
      return;
    }
    if (!state.manualPickerState.filtered.length) {
      list.innerHTML = '<div class="picker-empty">No stored contacts matched the current search.</div>';
      return;
    }

    list.innerHTML = state.manualPickerState.filtered.map((contact, index) => {
      const isSelected = contactIsSelectedForPicker(contact);
      const subtitleParts = [
        contact.identity_source || "unknown",
        contact.last_seen_at ? formatTimestamp(contact.last_seen_at) : "No last seen timestamp",
      ];
      return `
        <label class="picker-contact ${isSelected ? "picker-contact--selected" : ""}">
          <div class="picker-contact__main">
            <input type="checkbox" class="picker-contact__check" data-picker-index="${index}" ${isSelected ? "checked" : ""} />
            ${
              contact.avatar_url
                ? `<img src="${esc(contact.avatar_url)}" class="picker-contact__avatar" alt="" />`
                : `<span class="picker-contact__avatar picker-contact__avatar--placeholder">${esc((contact.name || "?").slice(0, 1))}</span>`
            }
            <div class="picker-contact__meta">
              <span class="picker-contact__name">${esc(contact.name)}</span>
              <span class="picker-contact__sub">${esc(subtitleParts.join(" | "))}</span>
            </div>
          </div>
          <div class="picker-contact__actions">
            <span class="campaign-pill ${contact.unread ? "campaign-pill--warn" : "campaign-pill--muted"}">${contact.unread ? "Unread" : "Seen"}</span>
          </div>
        </label>
      `;
    }).join("");
  }

  async function loadManualPickerContacts() {
    state.manualPickerState.loading = true;
    state.manualPickerState.error = "";
    state.manualPickerState.contacts = [];
    state.manualPickerState.filtered = [];
    setContactPickerStatus("Loading stored contacts...", "loading");
    renderManualPickerList();
    try {
      const query = buildContactsQuery(getCampaignDiscoveryFilters());
      const data = await apiRequest(`/api/contacts${query ? `?${query}` : ""}`);
      state.manualPickerState.contacts = data.contacts || [];
      state.manualPickerState.loaded = true;
      filterManualPickerContacts();
      if (state.manualPickerState.contacts.length) {
        setContactPickerStatus(
          `${state.manualPickerState.contacts.length} contact(s) available. Tick the ones you want, then confirm.`,
          "success"
        );
      } else {
        setContactPickerStatus("No contacts matched. Adjust the filters, or sync contacts first.", "empty");
      }
    } catch (err) {
      state.manualPickerState.error = friendlyError(err, "Could not load contacts.");
      setContactPickerStatus(friendlyError(err, "Could not load contacts."), "error");
      notify("error", "Could not load contacts", friendlyError(err));
      log(`Contact picker load error: ${err.message}`, "error");
    } finally {
      state.manualPickerState.loading = false;
      renderManualPickerList();
      updatePickerSelectedCount();
    }
  }

  async function openManualPicker(mode = "manual") {
    if (!state.auth.authenticated) {
      setAuthMessage("Sign in first to load workspace contacts.", "error");
      notify("warning", "Sign in required", "Sign in to load your stored contacts.");
      return;
    }
    state.manualPickerState.mode = mode;
    state.manualPickerState.draft = new Map();

    // Pre-tick whatever is already on the form so the picker edits, not replaces.
    if (mode === "campaign") {
      state.campaignSelectionState.selectedContacts.forEach((contact) => {
        if (contact.identity_key) state.manualPickerState.draft.set(contact.identity_key, contact);
      });
    } else {
      getManualTargetLines().forEach((name) => {
        state.manualPickerState.draft.set(normalizeSearchText(name), { name });
      });
    }

    $("#contact-picker-title").textContent = mode === "campaign" ? "Choose Campaign Recipients" : "Choose Contacts";
    $(".picker-modal__sub").textContent = mode === "campaign"
      ? "Search and filter your contacts, then confirm the campaign recipients."
      : "Search your contacts, then confirm to add them to the manual target list.";
    $("#contact-picker-modal").style.display = "flex";
    $("#contact-picker-modal").setAttribute("aria-hidden", "false");
    document.body.classList.add("modal-open");
    $("#contact-picker-search").value = "";
    updatePickerSelectedCount();
    $("#contact-picker-search").focus();
    await loadManualPickerContacts();
  }

  /** Commit the picker draft back into the campaign form or manual target list. */
  function confirmManualPicker() {
    const picked = Array.from(state.manualPickerState.draft.values());
    if (state.manualPickerState.mode === "campaign") {
      state.campaignSelectionState.selectedContacts = picked.filter((contact) => contact.identity_key);
      renderCampaignSelection();
      logEvent(`${picked.length} campaign recipient(s) selected.`);
      notify("success", "Recipients updated", `${picked.length} recipient${picked.length === 1 ? "" : "s"} selected.`, { key: "recipients" });
    } else {
      const names = picked.map((contact) => contact.name).filter(Boolean);
      $("#msg-targets").value = names.length ? `${names.join("\n")}\n` : "";
      logEvent(`${names.length} target(s) selected.`);
      notify("success", "Targets updated", `${names.length} target${names.length === 1 ? "" : "s"} in the list.`, { key: "targets" });
    }
    closeManualPicker();
  }

  function closeManualPicker() {
    $("#contact-picker-modal").style.display = "none";
    $("#contact-picker-modal").setAttribute("aria-hidden", "true");
    document.body.classList.remove("modal-open");
  }

  async function loadStoredContacts() {
    if (!state.auth.authenticated) {
      clearContactsDisplay("Sign in to load contacts for the active workspace.");
      return;
    }
    try {
      const data = await apiRequest("/api/contacts");
      updateContactsMeta(data);
      if (data.contacts && data.contacts.length > 0) {
        renderContacts(data);
        setContactsStatus("success", data.message || `Loaded ${data.contact_count} stored contact(s).`);
      } else {
        $("#contacts-block").style.display = "none";
        setContactsStatus("empty", data.message || "No stored contacts yet. Run a sync job to populate the contact store.");
      }
    } catch (err) {
      $("#contacts-block").style.display = "none";
      setContactsStatus("error", err.message);
      log(`Stored contacts load error: ${err.message}`, "error");
    }
  }

  function getCampaignDiscoveryFilters() {
    return {
      search: null,
      unread_only: $("#campaign-unread-only").checked,
      identity_source: $("#campaign-identity-source").value,
      sort_by: $("#campaign-sort-by").value,
      sort_order: $("#campaign-sort-order").value,
      selected_ids: [],
    };
  }

  function getCampaignFilters() {
    return {
      ...getCampaignDiscoveryFilters(),
      selected_ids: state.campaignSelectionState.selectedContacts.map((contact) => contact.identity_key).filter(Boolean),
    };
  }

  function setCampaignFilters(filters = {}) {
    $("#campaign-unread-only").checked = Boolean(filters.unread_only);
    $("#campaign-identity-source").value = filters.identity_source || "all";
    $("#campaign-sort-by").value = filters.sort_by || "name";
    $("#campaign-sort-order").value = filters.sort_order || "asc";
  }

  function clearCampaignSelection() {
    state.campaignSelectionState.selectedContacts = [];
    renderCampaignSelection();
  }

  function removeCampaignContact(identityKey) {
    state.campaignSelectionState.selectedContacts = state.campaignSelectionState.selectedContacts.filter((contact) => contact.identity_key !== identityKey);
    renderCampaignSelection();
  }

  const RECIPIENT_CHIP_LIMIT = 3;

  /**
   * Compact recipient summary: a count plus the first few names.
   * Stays collapsed to nothing while no recipient is selected.
   */
  function renderCampaignSelection() {
    const wrapper = $("#campaign-selection");
    const summary = $("#campaign-selection-summary");
    const count = $("#campaign-selected-count");
    const clearBtn = $("#btn-campaign-clear-selection");
    const pickerLabel = $("#btn-campaign-picker-label");
    const selected = state.campaignSelectionState.selectedContacts;

    wrapper.classList.toggle("campaign-selection--filled", selected.length > 0);
    clearBtn.style.display = selected.length ? "inline-flex" : "none";
    pickerLabel.textContent = selected.length ? "Edit selection" : "Choose From Contacts";

    if (!selected.length) {
      count.textContent = "None selected";
      summary.style.display = "none";
      summary.innerHTML = "";
      refreshCampaignActionState();
      return;
    }

    count.textContent = `${selected.length} recipient${selected.length === 1 ? "" : "s"} selected`;
    summary.style.display = "flex";
    const shown = selected.slice(0, RECIPIENT_CHIP_LIMIT);
    const remaining = selected.length - shown.length;
    summary.innerHTML = `${shown.map((contact) => `
      <span class="recipient-chip">
        ${esc(contact.name)}
        <button class="recipient-chip__remove" type="button" title="Remove ${esc(contact.name)}" data-campaign-remove="${esc(contact.identity_key)}">&times;</button>
      </span>
    `).join("")}${remaining > 0 ? `<span class="recipient-chip recipient-chip--more">+${remaining} more</span>` : ""}`;
    refreshCampaignActionState();
  }

  function setFieldError(elId, message) {
    const el = $(`#${elId}`);
    if (!el) return;
    el.textContent = message || "";
    el.style.display = message ? "block" : "none";
  }

  function clearCampaignErrors() {
    setFieldError("campaign-name-error", "");
    setFieldError("campaign-recipients-error", "");
    setFieldError("campaign-message-error", "");
  }

  /** Validate the campaign form and surface errors next to the affected field. */
  function validateCampaignForm(showErrors = true) {
    const errors = [];
    if (!$("#campaign-name").value.trim()) {
      errors.push(["campaign-name-error", "Give the campaign a name."]);
    }
    if (!state.campaignSelectionState.selectedContacts.length) {
      errors.push(["campaign-recipients-error", "Choose at least one recipient."]);
    }
    if (!$("#campaign-message").value.trim()) {
      errors.push(["campaign-message-error", "Write the message you want to send."]);
    }
    if (showErrors) {
      clearCampaignErrors();
      errors.forEach(([id, message]) => setFieldError(id, message));
    }
    return errors.length === 0;
  }

  /** Keep Save/Execute disabled while the form is incomplete or a run is in flight. */
  function refreshCampaignActionState() {
    const saveBtn = $("#btn-campaign-save");
    const executeBtn = $("#btn-campaign-execute");
    if (!saveBtn || !executeBtn) return;
    const hasRecipients = state.campaignSelectionState.selectedContacts.length > 0;
    const hasMessage = Boolean($("#campaign-message")?.value.trim());
    const hasName = Boolean($("#campaign-name")?.value.trim());
    const complete = hasRecipients && hasMessage && hasName;
    if (saveBtn.dataset.busy !== "true") {
      saveBtn.disabled = !complete || state.campaignRunning;
    }
    if (executeBtn.dataset.busy !== "true") {
      executeBtn.disabled = !complete || state.campaignRunning;
    }
  }

  async function loadCampaignHistory() {
    if (!state.auth.authenticated) {
      $("#campaign-history-list").innerHTML = '<p class="field-hint">Sign in to load campaign history.</p>';
      return;
    }
    try {
      const data = await apiRequest("/api/campaigns");
      state.campaignHistoryCache = data.campaigns || [];
      const list = $("#campaign-history-list");
      if (!state.campaignHistoryCache.length) {
        list.innerHTML = '<p class="field-hint">No campaigns yet.</p>';
        return;
      }
      const visibleCampaigns = state.campaignHistoryCache.slice(0, 6);
      list.innerHTML = visibleCampaigns.map((campaign) => {
        const status = (campaign.status || "draft").toLowerCase();
        const badgeVariant = {
          succeeded: "ok",
          completed: "ok",
          running: "running",
          failed: "error",
          cancelled: "error",
        }[status] || "";
        const recipients = campaign.selected_contact_ids?.length || campaign.matched_count || 0;
        const rowModifier = status === "running" ? " campaign-history__item--running"
          : status === "failed" || status === "cancelled" ? " campaign-history__item--failed" : "";
        return `
        <div class="campaign-history__item${rowModifier}">
          <div class="campaign-history__row">
            <div class="campaign-history__meta">
              <span class="campaign-history__name">${esc(campaign.name)}</span>
              <span class="campaign-history__sub">#${campaign.campaign_id} | ${recipients} recipient(s) | ${esc(formatTimestamp(campaign.created_at))}</span>
            </div>
            <div class="campaign-history__actions">
              <div class="campaign-history__badges">
                <span class="status-badge${badgeVariant ? ` status-badge--${badgeVariant}` : ""}">${esc(campaign.status)}</span>
                <span class="campaign-pill ${campaign.failed_count > 0 ? "campaign-pill--warn" : "campaign-pill--ok"}">${campaign.sent_count} sent / ${campaign.failed_count} failed</span>
              </div>
              <button class="btn btn--secondary btn--sm" type="button" data-campaign-load="${campaign.campaign_id}">Load</button>
              <button class="btn btn--secondary btn--sm" type="button" data-campaign-duplicate="${campaign.campaign_id}">Duplicate</button>
              ${campaign.results?.length ? `<button class="btn btn--secondary btn--sm" type="button" data-campaign-result="${campaign.campaign_id}">View result</button>` : ""}
            </div>
          </div>
        </div>
      `;
      }).join("");
      if (state.campaignHistoryCache.length > visibleCampaigns.length) {
        list.insertAdjacentHTML("beforeend", `<p class="field-hint">Showing latest ${visibleCampaigns.length} of ${state.campaignHistoryCache.length} campaigns.</p>`);
      }
    } catch (err) {
      log(`Campaign history load error: ${err.message}`, "error");
    }
  }

  function loadCampaignIntoBuilder(campaignId, { duplicate = false } = {}) {
    const campaign = state.campaignHistoryCache.find((item) => Number(item.campaign_id) === Number(campaignId));
    if (!campaign) {
      notify("error", "Campaign not found", "Refresh the campaign list and try again.");
      log("Campaign not found in history.", "error");
      return;
    }
    $("#campaign-name").value = duplicate ? `${campaign.name} (copy)` : campaign.name || "";
    $("#campaign-message").value = campaign.message || "";
    setCampaignFilters(campaign.filters || {});
    state.campaignSelectionState.selectedContacts = campaign.matched_contacts || [];
    clearCampaignErrors();
    renderCampaignSelection();
    if (!duplicate && campaign.results?.length) {
      $("#campaign-progress").style.display = "block";
      state.campaignRun = { total: campaign.matched_count || 0, startedAt: null, finishedAt: null };
      renderCampaignProgressSummary({
        status: campaign.status,
        total: campaign.matched_count || 0,
        sent: campaign.sent_count || 0,
        failed: campaign.failed_count || 0,
      });
      renderCampaignResults(campaign.results);
    }
    setMessageMode("campaign");
    $("#toggle-message-mode").querySelectorAll(".toggle-btn").forEach((btn) => {
      btn.classList.toggle("toggle-btn--active", btn.dataset.value === "campaign");
    });
    notify("info", duplicate ? "Campaign duplicated" : "Campaign loaded", campaign.name);
    log(`Loaded campaign '${campaign.name}'.`, "success");
  }

  async function createCampaignDraft() {
    const name = $("#campaign-name").value.trim();
    const message = $("#campaign-message").value.trim();
    if (!validateCampaignForm()) {
      throw new Error("Complete the campaign form before saving.");
    }
    const data = await apiRequest("/api/campaigns", {
      method: "POST",
      body: {
        name,
        message,
        filters: getCampaignFilters(),
      },
    });
    await loadCampaignHistory();
    return data;
  }

  async function executeCampaignDraft() {
    const draft = await createCampaignDraft();
    const delayMin = parseFloat($("#campaign-delay-min").value);
    const delayMax = parseFloat($("#campaign-delay-max").value);
    const campaignId = draft.campaign.campaign_id;
    startCampaignProgressPolling(campaignId, draft.campaign.matched_count || 0);
    const submission = await apiRequest(`/api/campaigns/${campaignId}/execute`, {
      method: "POST",
      body: {
        delay_min: Number.isFinite(delayMin) ? delayMin : 1,
        delay_max: Number.isFinite(delayMax) ? delayMax : 3,
      },
    });
    await pollJob(submission.job_id, {
      onComplete: async (job) => {
        stopCampaignProgressPolling();
        await pollCampaignProgress(campaignId).catch(() => {});
        renderCampaignExecutionJob(job);
        await loadCampaignHistory();
      },
      onError: (err) => {
        stopCampaignProgressPolling();
        state.campaignRunning = false;
        refreshCampaignActionState();
        notify("error", "Campaign failed", friendlyError(err), { key: "campaign-run" });
        log(`Campaign execution error: ${err.message}`, "error");
      },
    });
    return submission;
  }

  function setMessageMode(mode) {
    const manual = mode === "manual";
    $("#message-manual-mode").style.display = manual ? "block" : "none";
    $("#message-campaign-mode").style.display = manual ? "none" : "block";
    $("#manual-message-actions").style.display = manual ? "flex" : "none";
  }

  function bindNavigation() {
    $$(".nav-item").forEach((btn) => {
      btn.addEventListener("click", () => {
        if (btn.disabled) return;
        $$(".nav-item").forEach((item) => item.classList.remove("nav-item--active"));
        btn.classList.add("nav-item--active");
        const target = btn.dataset.module;
        $$(".module").forEach((module) => {
          module.style.display = module.id === `mod-${target}` ? "block" : "none";
        });
        if (target === "contacts" && state.auth.authenticated) {
          loadStoredContacts();
        }
        if (target === "messaging" && state.auth.authenticated) {
          loadCampaignHistory();
        }
      });
    });
  }

  function bindAuth() {
    $$(".auth-mode__btn").forEach((btn) => {
      btn.addEventListener("click", () => {
        setAuthMode(btn.dataset.authMode || "login");
      });
    });

    $("#btn-auth-login").addEventListener("click", async () => {
      const email = $("#auth-email").value.trim();
      const password = $("#auth-password").value;
      if (!email) {
        setAuthMessage("Email is required.", "error");
        return;
      }
      if (!password) {
        setAuthMessage("Password is required.", "error");
        return;
      }
      await withBusy("#btn-auth-login", "Signing In...", async () => {
        try {
          const session = await apiRequest("/api/auth/login", {
            method: "POST",
            body: { email, password },
          });
          applyAuthSession(session, "success");
          logEvent(`Signed in as ${session.user.email}.`, "success");
          notify("success", "Signed in", session.user.email);
          log(`Signed in as ${session.user.email}.`, "success");
          await hydrateWorkspace();
        } catch (err) {
          setAuthMessage(friendlyError(err, "Sign in failed."), "error");
          notify("error", "Sign in failed", friendlyError(err));
          log(`Login error: ${err.message}`, "error");
        }
      });
    });

    $("#btn-auth-register").addEventListener("click", async () => {
      const email = $("#auth-email").value.trim();
      const password = $("#auth-password").value;
      const displayName = $("#auth-display-name").value.trim();
      const workspaceName = $("#auth-workspace-name").value.trim();
      if (!email) {
        setAuthMessage("Email is required.", "error");
        return;
      }
      if (!password) {
        setAuthMessage("Password is required.", "error");
        return;
      }
      if (!displayName) {
        setAuthMessage("Display name is required.", "error");
        return;
      }
      if (!workspaceName) {
        setAuthMessage("Workspace name is required.", "error");
        return;
      }
      await withBusy("#btn-auth-register", "Creating...", async () => {
        try {
          const session = await apiRequest("/api/auth/register", {
            method: "POST",
            body: {
              email,
              password,
              display_name: displayName,
              workspace_name: workspaceName,
            },
          });
          applyAuthSession(session, "success");
          const workspace = getActiveWorkspace();
          logEvent(`Account created for ${session.user.email}.`, "success");
          notify("success", "Account created", workspace ? `Workspace '${workspace.name}' is ready.` : "");
          log(`Registered ${session.user.email}${workspace ? ` with workspace '${workspace.name}'` : ""}.`, "success");
          await hydrateWorkspace();
        } catch (err) {
          setAuthMessage(friendlyError(err, "Sign up failed."), "error");
          notify("error", "Sign up failed", friendlyError(err));
          log(`Registration error: ${err.message}`, "error");
        }
      });
    });

    $("#auth-password").addEventListener("keydown", (event) => {
      if (event.key === "Enter") {
        event.preventDefault();
        if (state.authMode === "register") {
          $("#btn-auth-register").click();
        } else {
          $("#btn-auth-login").click();
        }
      }
    });

    ["auth-display-name", "auth-workspace-name"].forEach((id) => {
      $(`#${id}`).addEventListener("keydown", (event) => {
        if (event.key === "Enter") {
          event.preventDefault();
          $("#btn-auth-register").click();
        }
      });
    });

    $("#btn-auth-logout").addEventListener("click", async () => {
      try {
        await apiRequest("/api/auth/logout", { method: "POST", allowUnauthorized: true });
      } catch {
        // Ignore logout transport failures; local session state still resets.
      }
      handleSignedOut("Signed out. Sign in to continue.", true);
      notify("info", "Signed out");
      log("Signed out.", "success");
    });

    $("#btn-create-workspace").addEventListener("click", async () => {
      const name = $("#new-workspace-name").value.trim();
      if (!name) {
        setAuthMessage("Workspace name is required.", "error");
        return;
      }
      await withBusy("#btn-create-workspace", "Creating...", async () => {
        try {
          const result = await apiRequest("/api/workspaces", {
            method: "POST",
            body: { name },
          });
          state.auth.activeWorkspaceId = result.active_workspace_id;
          const existingIndex = state.auth.workspaces.findIndex((workspace) => workspace.workspace_id === result.workspace.workspace_id);
          if (existingIndex >= 0) {
            state.auth.workspaces[existingIndex] = result.workspace;
          } else {
            state.auth.workspaces.push(result.workspace);
          }
          applyAuthSession({
            user: state.auth.user,
            active_workspace_id: result.active_workspace_id,
            workspaces: state.auth.workspaces,
          }, "success");
          $("#new-workspace-name").value = "";
          setAuthMessage(`Created workspace '${result.workspace.name}'.`, "success");
          logEvent(`Workspace '${result.workspace.name}' created.`, "success");
          notify("success", "Workspace created", result.workspace.name);
          log(`Created workspace '${result.workspace.name}'.`, "success");
          await hydrateWorkspace();
        } catch (err) {
          setAuthMessage(friendlyError(err, "Could not create the workspace."), "error");
          notify("error", "Could not create workspace", friendlyError(err));
          log(`Workspace create error: ${err.message}`, "error");
        }
      });
    });

    $("#new-workspace-name").addEventListener("keydown", (event) => {
      if (event.key === "Enter") {
        event.preventDefault();
        $("#btn-create-workspace").click();
      }
    });

    $("#workspace-select").addEventListener("change", async (event) => {
      const workspaceId = event.target.value;
      if (!workspaceId || workspaceId === state.auth.activeWorkspaceId) return;
      const previousValue = state.auth.activeWorkspaceId;
      event.target.disabled = true;
      try {
        const result = await apiRequest(`/api/workspaces/${workspaceId}/switch`, { method: "POST" });
        state.auth.activeWorkspaceId = result.active_workspace_id;
        const summary = state.auth.workspaces.find((workspace) => workspace.workspace_id === workspaceId);
        if (summary) {
          summary.login_state = result.workspace.login_state;
        }
        $("#dashboard-session-meta").textContent = result.workspace.name;
        $("#auth-summary-workspace").textContent = result.workspace.name;
        $("#auth-summary-role").textContent = result.workspace.role;
        setAuthMessage(`Switched to workspace '${result.workspace.name}'.`, "success");
        logEvent(`Switched to workspace '${result.workspace.name}'.`);
        notify("info", "Workspace switched", result.workspace.name);
        log(`Switched to workspace '${result.workspace.name}'.`, "success");
        await hydrateWorkspace();
      } catch (err) {
        event.target.value = previousValue || "";
        setAuthMessage(friendlyError(err, "Could not switch workspace."), "error");
        notify("error", "Could not switch workspace", friendlyError(err));
        log(`Workspace switch error: ${err.message}`, "error");
      } finally {
        event.target.disabled = false;
      }
    });
  }

  async function startZaloConnection(buttonSelector, busyLabel, replaceExisting = false) {
    if (!state.auth.authenticated) {
      setAuthMessage("Sign in first to start the workspace login browser.", "error");
      notify("warning", "Sign in required", "Sign in before connecting a Zalo session.");
      return;
    }
    const started = await withBusy(buttonSelector, busyLabel, async () => {
      $("#btn-login-stop").disabled = false;
      $("#login-state-text").textContent = "Starting browser...";
      $("#login-state-detail").textContent = "Preparing the Zalo session, this can take a few seconds.";
      $("#login-state-icon").className = "login-state login-state--waiting";
      $("#login-preparing").style.display = "flex";
      log("Starting workspace login browser...");
      try {
        if (replaceExisting) {
          await apiRequest("/api/login/stop", { method: "POST" });
        }
        const data = await apiRequest("/api/login/start", { method: "POST" });
        renderZaloLoginState(data);
        log("Zalo login started. Scan the QR code shown on this page.");
        startLoginPolling();
        return true;
      } catch (err) {
        renderZaloLoginState({ state: "error", message: err.message });
        notify("error", "Could not start Zalo session", friendlyError(err), { key: "zalo-problem" });
        log(err.message, "error");
        return false;
      }
    });
    if (started && buttonSelector === "#btn-login-start") {
      $(buttonSelector).disabled = true;
    }
  }

  function bindLogin() {
    $("#btn-login-start").addEventListener("click", () => {
      startZaloConnection("#btn-login-start", "Connecting...");
    });

    $("#btn-login-refresh-qr").addEventListener("click", () => {
      stopLoginPolling();
      startZaloConnection("#btn-login-refresh-qr", "Refreshing...", true);
    });

    $("#btn-login-stop").addEventListener("click", async () => {
      stopLoginPolling();
      await withBusy("#btn-login-stop", "Stopping...", async () => {
        try {
          const data = await apiRequest("/api/login/stop", { method: "POST" });
          renderZaloLoginState(data);
          logEvent("Zalo session stopped.");
        } catch (err) {
          renderZaloLoginState({ state: "error", message: err.message });
          notify("error", "Could not stop the session", friendlyError(err), { key: "zalo-problem" });
        }
      });
      $("#btn-login-stop").disabled = state.zaloState === "idle" || state.zaloState === "signed_out";
    });

    $("#btn-login-sync-contacts").addEventListener("click", () => {
      runContactSync("#btn-login-sync-contacts");
    });

    $("#btn-account-view-contacts").addEventListener("click", () => {
      const contactsNav = $('.nav-item[data-module="contacts"]');
      if (contactsNav && !contactsNav.disabled) contactsNav.click();
    });
  }

  function bindMessaging() {
    $("#btn-open-contact-picker").addEventListener("click", () => openManualPicker("manual"));
    $("#btn-campaign-open-contact-picker").addEventListener("click", () => openManualPicker("campaign"));
    $("#btn-clear-msg-targets").addEventListener("click", () => {
      if (!$("#msg-targets").value.trim()) return;
      $("#msg-targets").value = "";
      renderManualPickerList();
      log("Cleared manual target list.");
    });
    $("#btn-close-contact-picker").addEventListener("click", closeManualPicker);
    $("#btn-picker-cancel").addEventListener("click", closeManualPicker);
    $("#contact-picker-backdrop").addEventListener("click", closeManualPicker);
    $("#btn-picker-confirm").addEventListener("click", confirmManualPicker);
    $("#contact-picker-search").addEventListener("input", () => {
      filterManualPickerContacts();
      renderManualPickerList();
    });

    // Filters live server-side, so changing one reloads the picker list.
    ["#campaign-identity-source", "#campaign-sort-by", "#campaign-sort-order", "#campaign-unread-only"].forEach((selector) => {
      $(selector).addEventListener("change", () => {
        if ($("#contact-picker-modal").style.display !== "none") loadManualPickerContacts();
      });
    });

    $("#contact-picker-list").addEventListener("change", (event) => {
      const checkbox = event.target.closest(".picker-contact__check");
      if (!checkbox) return;
      const contact = state.manualPickerState.filtered[Number(checkbox.dataset.pickerIndex)];
      if (!contact) return;
      togglePickerContact(contact, checkbox.checked);
      checkbox.closest(".picker-contact")?.classList.toggle("picker-contact--selected", checkbox.checked);
    });

    $("#btn-picker-select-all").addEventListener("click", () => {
      state.manualPickerState.filtered.forEach((contact) => togglePickerContact(contact, true));
      renderManualPickerList();
      updatePickerSelectedCount();
    });

    $("#btn-picker-clear").addEventListener("click", () => {
      state.manualPickerState.draft = new Map();
      renderManualPickerList();
      updatePickerSelectedCount();
    });

    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && $("#contact-picker-modal").style.display !== "none") {
        closeManualPicker();
      }
    });

    $("#campaign-selection-summary").addEventListener("click", (event) => {
      const btn = event.target.closest("button[data-campaign-remove]");
      if (!btn) return;
      const identityKey = btn.dataset.campaignRemove;
      const contact = state.campaignSelectionState.selectedContacts.find((item) => item.identity_key === identityKey);
      removeCampaignContact(identityKey);
      if (contact) log(`Removed '${contact.name}' from campaign recipients.`);
    });

    $("#campaign-history-list").addEventListener("click", (event) => {
      const loadBtn = event.target.closest("button[data-campaign-load]");
      if (loadBtn) {
        loadCampaignIntoBuilder(loadBtn.dataset.campaignLoad);
        return;
      }
      const duplicateBtn = event.target.closest("button[data-campaign-duplicate]");
      if (duplicateBtn) {
        loadCampaignIntoBuilder(duplicateBtn.dataset.campaignDuplicate, { duplicate: true });
        return;
      }
      const resultBtn = event.target.closest("button[data-campaign-result]");
      if (resultBtn) {
        const campaign = state.campaignHistoryCache.find(
          (item) => Number(item.campaign_id) === Number(resultBtn.dataset.campaignResult)
        );
        if (!campaign) return;
        $("#campaign-progress").style.display = "block";
        state.campaignRun = { total: campaign.matched_count || 0, startedAt: null, finishedAt: null };
        renderCampaignProgressSummary({
          status: campaign.status,
          total: campaign.matched_count || 0,
          sent: campaign.sent_count || 0,
          failed: campaign.failed_count || 0,
        });
        renderCampaignResults(campaign.results || []);
        $("#campaign-results").scrollIntoView?.({ behavior: "smooth", block: "nearest" });
      }
    });

    $("#btn-campaign-clear-selection").addEventListener("click", () => {
      if (!state.campaignSelectionState.selectedContacts.length) return;
      clearCampaignSelection();
      log("Cleared campaign recipient selection.");
    });

    $("#btn-campaign-toggle-events").addEventListener("click", () => {
      const list = $("#campaign-progress-list");
      const expanded = list.classList.toggle("campaign-progress__list--expanded");
      $("#btn-campaign-toggle-events").textContent = expanded ? "Show less" : "View all activity";
    });

    // Live validation so the action buttons reflect the form as it is typed.
    ["#campaign-name", "#campaign-message"].forEach((selector) => {
      $(selector).addEventListener("input", refreshCampaignActionState);
    });

    $("#btn-msg-send").addEventListener("click", async () => {
      const raw = $("#msg-targets").value.trim();
      const message = $("#msg-content").value.trim();
      if (!raw) {
        notify("warning", "No targets", "Add at least one phone number or contact name.");
        return;
      }
      if (!message) {
        notify("warning", "Message is empty", "Write the message you want to send.");
        return;
      }
      const targets = raw.split("\n").map((line) => line.trim()).filter(Boolean);
      const delayMin = parseFloat($("#msg-delay-min").value) || 15;
      const delayMax = parseFloat($("#msg-delay-max").value) || 30;
      await withBusy("#btn-msg-send", "Queueing...", async () => {
        await queueBackgroundJob({
          label: "Messaging",
          submit: () => apiRequest("/api/message/send", {
            method: "POST",
            body: { targets, message, delay_min: delayMin, delay_max: delayMax },
          }),
          onQueued: (submission) => {
            showTaskResultInfo("msg-result", "Messaging queued", `Job ${submission.job_id} is waiting for the worker.`);
            logEvent(`Sending ${targets.length} message(s).`);
            notify("progress", "Sending messages", `${targets.length} target(s) queued.`, { key: "manual-send" });
          },
          onUpdate: (job) => renderMessageJobResult(job),
          onComplete: (job) => renderMessageJobResult(job),
          onError: (err) => {
            showTaskResultError("msg-result", friendlyError(err, "Messages could not be sent."));
            notify("error", "Sending failed", friendlyError(err), { key: "manual-send" });
            log(`Messaging error: ${err.message}`, "error");
          },
        });
      });
    });

    $("#btn-campaign-save").addEventListener("click", async () => {
      if (!validateCampaignForm()) {
        notify("warning", "Campaign incomplete", "Fix the highlighted fields first.");
        return;
      }
      await withBusy("#btn-campaign-save", "Saving...", async () => {
        try {
          const data = await createCampaignDraft();
          logEvent(`Campaign '${data.campaign.name}' saved.`, "success");
          notify("success", "Campaign saved", data.campaign.name);
          log(data.message || `Campaign '${data.campaign.name}' saved.`, "success");
        } catch (err) {
          notify("error", "Could not save campaign", friendlyError(err));
          log(`Campaign save error: ${err.message}`, "error");
        }
      });
    });

    $("#btn-campaign-execute").addEventListener("click", async () => {
      if (!validateCampaignForm()) {
        notify("warning", "Campaign incomplete", "Fix the highlighted fields first.");
        return;
      }
      await withBusy("#btn-campaign-execute", "Queueing...", async () => {
        try {
          const recipients = state.campaignSelectionState.selectedContacts.length;
          await executeCampaignDraft();
          logEvent(`Campaign started for ${recipients} recipient(s).`);
          notify("progress", "Campaign started", `Sending to ${recipients} recipient(s)...`, { key: "campaign-run" });
        } catch (err) {
          stopCampaignProgressPolling();
          state.campaignRunning = false;
          notify("error", "Campaign could not start", friendlyError(err), { key: "campaign-run" });
          log(`Campaign execution error: ${err.message}`, "error");
        }
      });
    });

    $("#btn-campaign-refresh").addEventListener("click", () => {
      loadCampaignHistory();
    });

    $("#toggle-message-mode").querySelectorAll(".toggle-btn").forEach((btn) => {
      btn.addEventListener("click", () => {
        $("#toggle-message-mode").querySelectorAll(".toggle-btn").forEach((item) => item.classList.remove("toggle-btn--active"));
        btn.classList.add("toggle-btn--active");
        setMessageMode(btn.dataset.value);
      });
    });
  }

  function bindFriends() {
    $("#btn-friend-send").addEventListener("click", async () => {
      const raw = $("#friend-phones").value.trim();
      if (!raw) {
        notify("warning", "No phone numbers", "Add at least one phone number, one per line.");
        return;
      }
      const phoneNumbers = raw.split("\n").map((line) => line.trim()).filter(Boolean);
      const greeting = $("#friend-greeting").value.trim() || null;
      await withBusy("#btn-friend-send", "Queueing...", async () => {
        await queueBackgroundJob({
          label: "Friend request",
          submit: () => apiRequest("/api/friends/add", {
            method: "POST",
            body: { phone_numbers: phoneNumbers, greeting_message: greeting },
          }),
          onQueued: (submission) => {
            showTaskResultInfo("friend-result", "Friend requests queued", `Job ${submission.job_id} is waiting for the worker.`);
            notify("progress", "Sending friend requests", `${phoneNumbers.length} number(s) queued.`, { key: "friend-send" });
          },
          onUpdate: (job) => renderFriendJobResult(job),
          onComplete: (job) => renderFriendJobResult(job),
          onError: (err) => {
            showTaskResultError("friend-result", friendlyError(err, "Friend requests failed."));
            notify("error", "Friend requests failed", friendlyError(err), { key: "friend-send" });
            log(`Friend request error: ${err.message}`, "error");
          },
        });
      });
    });
  }

  function bindGroups() {
    $("#btn-group-send").addEventListener("click", async () => {
      const groupName = $("#group-name").value.trim();
      const message = $("#group-message").value.trim();
      if (!groupName) {
        notify("warning", "Group name required", "Enter the group you want to post in.");
        return;
      }
      if (!message) {
        notify("warning", "Message is empty", "Write the message you want to send.");
        return;
      }
      await withBusy("#btn-group-send", "Queueing...", async () => {
        await queueBackgroundJob({
          label: "Group message",
          submit: () => apiRequest("/api/groups/message", {
            method: "POST",
            body: { group_name: groupName, message },
          }),
          onQueued: (submission) => {
            showTaskResultInfo("group-result", "Group message queued", `Job ${submission.job_id} is waiting for the worker.`);
            notify("progress", "Sending to group", groupName, { key: "group-send" });
          },
          onUpdate: (job) => renderGroupJobResult(job),
          onComplete: (job) => renderGroupJobResult(job),
          onError: (err) => {
            showTaskResultError("group-result", friendlyError(err, "Group message failed."));
            notify("error", "Group message failed", friendlyError(err), { key: "group-send" });
            log(`Group error: ${err.message}`, "error");
          },
        });
      });
    });
  }

  function bindContacts() {
    $("#btn-sync-contacts").addEventListener("click", () => {
      runContactSync("#btn-sync-contacts");
    });
  }

  async function loadSettings() {
    if (!state.auth.authenticated) return;
    try {
      const settings = await apiRequest("/api/settings");
      setToggle("toggle-lang", settings.language);
      setToggle("toggle-theme", settings.theme);
      setToggle("toggle-layout", settings.layout);
      $("#proxy-toggle").checked = settings.proxy_enabled;
      $("#proxy-fields").style.display = settings.proxy_enabled ? "block" : "none";
      $("#proxy-raw").value = formatLegacyProxyValue(settings);
      $("#setting-delay-min").value = settings.delay_min;
      $("#setting-delay-max").value = settings.delay_max;
    } catch (err) {
      log(`Settings load error: ${err.message}`, "error");
    }
  }

  function bindSettings() {
    $$(".toggle-group").forEach((group) => {
      if (group.id === "toggle-message-mode") return;
      group.querySelectorAll(".toggle-btn").forEach((btn) => {
        btn.addEventListener("click", () => {
          group.querySelectorAll(".toggle-btn").forEach((item) => item.classList.remove("toggle-btn--active"));
          btn.classList.add("toggle-btn--active");
        });
      });
    });

    $("#proxy-toggle").addEventListener("change", (event) => {
      $("#proxy-fields").style.display = event.target.checked ? "block" : "none";
    });

    $("#btn-save-settings").addEventListener("click", async () => {
      const settingsPayload = {
        language: getToggle("toggle-lang"),
        theme: getToggle("toggle-theme"),
        layout: getToggle("toggle-layout"),
        proxy_enabled: $("#proxy-toggle").checked,
        proxy_raw: $("#proxy-raw").value.trim() || null,
        proxy_address: null,
        proxy_port: null,
        delay_min: parseFloat($("#setting-delay-min").value) || 15,
        delay_max: parseFloat($("#setting-delay-max").value) || 30,
      };
      await withBusy("#btn-save-settings", "Saving...", async () => {
        try {
          const saved = await apiRequest("/api/settings", {
            method: "POST",
            body: settingsPayload,
          });
          $("#proxy-raw").value = formatLegacyProxyValue(saved);
          notify("success", "Settings saved");
          log("Settings saved.", "success");
        } catch (err) {
          notify("error", "Could not save settings", friendlyError(err));
          log(`Settings save error: ${err.message}`, "error");
        }
      });
    });
  }

  async function initialize() {
    checkHealth();
    bindNavigation();
    bindAuth();
    bindLogin();
    bindMessaging();
    bindFriends();
    bindGroups();
    bindContacts();
    bindSettings();
    renderCampaignSelection();
    renderAccountSyncCard();
    setMessageMode("manual");
    handleSignedOut("Sign in to unlock workspace data and automation actions.", true);
    await bootstrapSession();
  }

  initialize();
})();
