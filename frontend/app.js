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
    lastCampaignPreview: null,
    campaignHistoryCache: [],
    manualPickerState: {
      contacts: [],
      filtered: [],
      loaded: false,
      loading: false,
      error: "",
      mode: "manual",
    },
    campaignSelectionState: {
      selectedContacts: [],
      previewContacts: [],
    },
  };

  function esc(str) {
    const d = document.createElement("div");
    d.textContent = str ?? "";
    return d.innerHTML;
  }

  function log(message, type = "") {
    const logEl = $("#activity-log");
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
    $("#contacts-meta-count").textContent = String(data.stored_contact_count || 0);
    $("#contacts-meta-status").textContent = data.last_sync_status || "Never synced";
    $("#contacts-meta-time").textContent = data.last_sync_at ? formatTimestamp(data.last_sync_at) : "No stored sync yet";
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

  function resetCampaignProgressUI(total = 0) {
    state.campaignProgressSeen = new Set();
    $("#campaign-progress").style.display = "block";
    $("#campaign-progress-count").textContent = total ? `0/${total} sent` : "Starting";
    $("#campaign-progress-list").innerHTML = '<div class="campaign-progress__item">Preparing campaign...</div>';
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
    while (list.children.length > 30) {
      list.lastChild.remove();
    }
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
    $("#btn-login-sync-contacts").style.display = "none";
    $("#login-qr").style.display = "none";
    $("#login-qr-image").removeAttribute("src");
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
    $("#campaign-preview").style.display = "none";
    $("#campaign-progress").style.display = "none";
    if (!silent) {
      log(message, "error");
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
      const message = (data && typeof data.detail === "string" && data.detail) || "Request failed.";
      if (res.status === 401) {
        handleSignedOut("Your session expired. Sign in again.");
      }
      const error = new Error(message);
      error.status = res.status;
      error.payload = data;
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

  function renderZaloLoginState(data) {
    const icon = $("#login-state-icon");
    const text = $("#login-state-text");
    const detail = $("#login-state-detail");
    const btnStart = $("#btn-login-start");
    const btnStop = $("#btn-login-stop");
    const btnSync = $("#btn-login-sync-contacts");
    const qrPanel = $("#login-qr");
    const qrImage = $("#login-qr-image");
    const hasQrImage = typeof data.qr_image_base64 === "string"
      && data.qr_image_base64.startsWith("data:image/png;base64,");

    qrPanel.style.display = hasQrImage ? "block" : "none";
    if (hasQrImage) {
      qrImage.src = data.qr_image_base64;
    } else {
      qrImage.removeAttribute("src");
    }

    if (!state.auth.authenticated) {
      text.textContent = "Dashboard sign-in required";
      detail.textContent = "Sign in first, then manage the workspace browser session.";
      icon.className = "login-state";
      btnStart.disabled = true;
      btnStop.disabled = true;
      btnSync.style.display = "none";
      $("#login-info").style.display = "none";
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

    if (data.state === "authenticated") {
      text.textContent = "Authenticated";
      detail.textContent = data.message || "Workspace session is ready.";
      icon.className = "login-state login-state--ok";
      btnStart.disabled = true;
      btnStop.disabled = true;
      btnSync.style.display = "inline-flex";
      if (state.lastRenderedLoginState !== "authenticated") {
        log(`Workspace Zalo session ready${data.profile_name ? ` for ${data.profile_name}` : ""}.`, "success");
      }
      state.lastRenderedLoginState = "authenticated";
      return;
    }

    if (data.state === "waiting_qr") {
      text.textContent = "Waiting for login";
      detail.textContent = data.message || "Scan the QR code or complete the login in the browser window.";
      icon.className = "login-state login-state--waiting";
      btnStart.disabled = true;
      btnStop.disabled = false;
      btnSync.style.display = "none";
      state.lastRenderedLoginState = "waiting_qr";
      return;
    }

    if (data.state === "expired") {
      text.textContent = "Session Expired";
      detail.textContent = data.message || "Re-authentication is required.";
      icon.className = "login-state login-state--err";
      btnStart.disabled = false;
      btnStop.disabled = false;
      btnSync.style.display = "none";
      state.lastRenderedLoginState = "expired";
      return;
    }

    if (data.state === "error") {
      text.textContent = "Session Error";
      detail.textContent = data.message || "Workspace session check failed.";
      icon.className = "login-state login-state--err";
      btnStart.disabled = false;
      btnStop.disabled = false;
      btnSync.style.display = "none";
      state.lastRenderedLoginState = "error";
      return;
    }

    text.textContent = "Not connected";
    detail.textContent = data.message || 'Click "Connect Zalo" to begin.';
    icon.className = "login-state";
    btnStart.disabled = false;
    btnStop.disabled = true;
    btnSync.style.display = "none";
    state.lastRenderedLoginState = "idle";
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
      showTaskResult("msg-result", job.result || {}, "message");
      log(job.result?.message || "Message job completed.", "success");
      return;
    }
    if (job.status === "failed" || job.status === "cancelled") {
      showTaskResultError("msg-result", extractJobError(job));
      log(`Messaging error: ${extractJobError(job)}`, "error");
      return;
    }
    showPendingJob("msg-result", job, "Messaging");
  }

  function renderFriendJobResult(job) {
    if (job.status === "succeeded") {
      showTaskResult("friend-result", job.result || {}, "friend");
      log(job.result?.message || "Friend request job completed.", job.result?.failed > 0 ? "error" : "success");
      return;
    }
    if (job.status === "failed" || job.status === "cancelled") {
      showTaskResultError("friend-result", extractJobError(job));
      log(`Friend request error: ${extractJobError(job)}`, "error");
      return;
    }
    showPendingJob("friend-result", job, "Friend request");
  }

  function renderGroupJobResult(job) {
    const el = $("#group-result");
    el.style.display = "block";
    if (job.status === "succeeded") {
      el.className = `task-result ${(job.result && job.result.success) ? "task-result--success" : "task-result--fail"}`;
      el.innerHTML = `<div class="task-result__title">${job.result?.success ? "OK Message Sent" : "ERR Failed"}</div><div class="task-result__detail">${esc(job.result?.message || "Group job completed.")}</div>`;
      log(job.result?.message || "Group job completed.", job.result?.success ? "success" : "error");
      return;
    }
    if (job.status === "failed" || job.status === "cancelled") {
      showTaskResultError("group-result", extractJobError(job));
      log(`Group error: ${extractJobError(job)}`, "error");
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
      applyContactSyncResult(job.result || {});
      showTaskResultInfo("contacts-result", "Contact Sync Completed", job.result?.message || "Contact sync completed.");
      log(job.result?.message || "Contact sync completed.", "success");
      return;
    }
    if (job.status === "failed" || job.status === "cancelled") {
      $("#contacts-block").style.display = "none";
      setContactsStatus("error", extractJobError(job));
      showTaskResultError("contacts-result", extractJobError(job));
      log(`Contact sync error: ${extractJobError(job)}`, "error");
      return;
    }
    setContactsStatus("progress", latestJobMessage(job));
    showPendingJob("contacts-result", job, "Contact sync");
  }

  function renderCampaignExecutionJob(job) {
    if (job.status === "succeeded") {
      const result = job.result || {};
      const campaign = result.campaign || {};
      showTaskResult("msg-result", {
        total: result.total || campaign.matched_count || 0,
        sent: result.sent || campaign.sent_count || 0,
        failed: result.failed || campaign.failed_count || 0,
        results: (result.results || []).map((item) => ({
          target: item.target,
          success: item.success,
          error: item.error,
        })),
        message: result.message || "Campaign completed.",
      }, "message");
      log(result.message || "Campaign completed.", (result.failed || 0) > 0 ? "error" : "success");
      return;
    }
    if (job.status === "failed" || job.status === "cancelled") {
      showTaskResultError("msg-result", extractJobError(job));
      log(`Campaign execution error: ${extractJobError(job)}`, "error");
      return;
    }
    showPendingJob("msg-result", job, "Campaign");
  }

  async function pollCampaignProgress(campaignId) {
    const data = await apiRequest(`/api/campaigns/${campaignId}/progress`);
    $("#campaign-progress").style.display = "block";
    $("#campaign-progress-count").textContent = data.total ? `${data.sent}/${data.total} sent` : data.status;
    (data.events || []).forEach((event) => {
      appendCampaignProgressEvent(event);
      if (event.message) {
        log(
          event.message,
          event.level === "error" || event.success === false ? "error" : event.success === true ? "success" : ""
        );
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

  function getManualTargetLookup() {
    return new Set(getManualTargetLines().map((line) => normalizeSearchText(line)));
  }

  function appendManualTarget(name) {
    const textarea = $("#msg-targets");
    const existing = getManualTargetLines();
    const lookup = new Set(existing.map((line) => normalizeSearchText(line)));
    const normalized = normalizeSearchText(name);
    if (!normalized || lookup.has(normalized)) return false;
    existing.push(name);
    textarea.value = `${existing.join("\n")}\n`;
    return true;
  }

  function contactIsSelectedForPicker(contact) {
    if (state.manualPickerState.mode === "campaign") {
      return state.campaignSelectionState.selectedContacts.some((selected) => selected.identity_key === contact.identity_key);
    }
    return getManualTargetLookup().has(normalizeSearchText(contact.name));
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

    list.innerHTML = state.manualPickerState.filtered.map((contact) => {
      const isAdded = contactIsSelectedForPicker(contact);
      const subtitleParts = [
        contact.identity_source || "unknown",
        contact.last_seen_at ? formatTimestamp(contact.last_seen_at) : "No last seen timestamp",
      ];
      return `
        <div class="picker-contact">
          <div class="picker-contact__main">
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
            <span class="campaign-pill ${contact.unread ? "campaign-pill--warn" : ""}">${contact.unread ? "Unread" : "Seen"}</span>
            <span class="picker-contact__state ${isAdded ? "picker-contact__state--added" : ""}">${isAdded ? "Added" : "Ready"}</span>
            <button class="btn btn--secondary btn--sm" type="button" data-contact-identity="${esc(contact.identity_key || "")}" data-contact-name="${esc(contact.name)}">${isAdded ? "Added" : "Add"}</button>
          </div>
        </div>
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
      const data = await apiRequest("/api/contacts");
      state.manualPickerState.contacts = data.contacts || [];
      state.manualPickerState.loaded = true;
      filterManualPickerContacts();
      if (state.manualPickerState.contacts.length) {
        setContactPickerStatus(
          `Loaded ${state.manualPickerState.contacts.length} stored contact(s). Select contacts to add them into ${state.manualPickerState.mode === "campaign" ? "campaign recipients" : "the manual target list"}.`,
          "success"
        );
      } else {
        setContactPickerStatus("No stored contacts yet. Sync contacts first in the Contacts tab.", "empty");
      }
    } catch (err) {
      state.manualPickerState.error = err.message;
      setContactPickerStatus(err.message, "error");
      log(`Contact picker load error: ${err.message}`, "error");
    } finally {
      state.manualPickerState.loading = false;
      renderManualPickerList();
    }
  }

  async function openManualPicker(mode = "manual") {
    if (!state.auth.authenticated) {
      setAuthMessage("Sign in first to load workspace contacts.", "error");
      return;
    }
    state.manualPickerState.mode = mode;
    $("#contact-picker-title").textContent = mode === "campaign" ? "Choose Campaign Contacts" : "Choose Contacts";
    $(".picker-modal__sub").textContent = mode === "campaign"
      ? "Search your stored contacts and add them into the selected campaign recipients."
      : "Search your stored contacts and add names into the manual target list.";
    $("#contact-picker-modal").style.display = "flex";
    $("#contact-picker-modal").setAttribute("aria-hidden", "false");
    document.body.classList.add("modal-open");
    $("#contact-picker-search").value = "";
    $("#contact-picker-search").focus();
    await loadManualPickerContacts();
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
      search: $("#campaign-search").value.trim() || null,
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
    $("#campaign-search").value = filters.search || "";
    $("#campaign-unread-only").checked = Boolean(filters.unread_only);
    $("#campaign-identity-source").value = filters.identity_source || "all";
    $("#campaign-sort-by").value = filters.sort_by || "name";
    $("#campaign-sort-order").value = filters.sort_order || "asc";
  }

  function addCampaignContacts(contacts) {
    const existing = new Map(state.campaignSelectionState.selectedContacts.map((contact) => [contact.identity_key, contact]));
    let added = 0;
    for (const contact of contacts) {
      if (!contact || !contact.identity_key || existing.has(contact.identity_key)) continue;
      existing.set(contact.identity_key, contact);
      added += 1;
    }
    state.campaignSelectionState.selectedContacts = Array.from(existing.values());
    renderCampaignSelection();
    renderCampaignPreview(state.campaignSelectionState.previewContacts);
    return added;
  }

  function clearCampaignSelection() {
    state.campaignSelectionState.selectedContacts = [];
    renderCampaignSelection();
    renderCampaignPreview(state.campaignSelectionState.previewContacts);
  }

  function removeCampaignContact(identityKey) {
    state.campaignSelectionState.selectedContacts = state.campaignSelectionState.selectedContacts.filter((contact) => contact.identity_key !== identityKey);
    renderCampaignSelection();
    renderCampaignPreview(state.campaignSelectionState.previewContacts);
  }

  function renderCampaignSelection() {
    const list = $("#campaign-selection-list");
    const count = $("#campaign-selected-count");
    const selected = state.campaignSelectionState.selectedContacts;
    count.textContent = `${selected.length} selected`;
    if (!selected.length) {
      list.innerHTML = '<p class="field-hint">No campaign recipients selected yet. Preview matches, then add the contacts you want.</p>';
      return;
    }
    list.innerHTML = selected.map((contact) => `
      <div class="campaign-history__item">
        <div class="campaign-history__row">
          <div class="campaign-history__meta">
            <span class="campaign-history__name">${esc(contact.name)}</span>
            <span class="campaign-history__sub">${esc(contact.identity_source || "unknown")} | ${contact.last_seen_at ? esc(formatTimestamp(contact.last_seen_at)) : "No last seen timestamp"}</span>
          </div>
          <div class="campaign-history__actions">
            <span class="campaign-pill ${contact.unread ? "campaign-pill--warn" : "campaign-pill--muted"}">${contact.unread ? "Unread" : "Seen"}</span>
            <button class="btn btn--secondary btn--sm" type="button" data-campaign-remove="${esc(contact.identity_key)}">Remove</button>
          </div>
        </div>
      </div>
    `).join("");
  }

  async function previewCampaignMatches() {
    const filters = getCampaignDiscoveryFilters();
    const query = buildContactsQuery(filters);
    const data = await apiRequest(`/api/contacts${query ? `?${query}` : ""}`);
    state.campaignSelectionState.previewContacts = data.contacts || [];
    state.lastCampaignPreview = {
      filters,
      contacts: state.campaignSelectionState.previewContacts,
      storedContactCount: data.stored_contact_count || 0,
    };
    renderCampaignPreview(state.campaignSelectionState.previewContacts);
    return state.lastCampaignPreview;
  }

  function renderCampaignPreview(contacts) {
    const preview = $("#campaign-preview");
    const list = $("#campaign-preview-list");
    const count = $("#campaign-preview-count");
    const selectedIds = new Set(state.campaignSelectionState.selectedContacts.map((contact) => contact.identity_key));
    preview.style.display = "block";
    count.textContent = `${contacts.length} match(es)`;
    if (!contacts.length) {
      list.innerHTML = '<p class="field-hint">No contacts matched the current campaign filters.</p>';
      return;
    }
    list.innerHTML = contacts.slice(0, 20).map((contact) => `
      <div class="campaign-contact">
        <div class="campaign-contact__row">
          <div class="campaign-contact__meta">
            <span class="campaign-contact__name">${esc(contact.name)}</span>
            <span class="campaign-contact__sub">${esc(contact.identity_source || "unknown")} | ${contact.last_seen_at ? esc(formatTimestamp(contact.last_seen_at)) : "No last seen timestamp"}</span>
          </div>
          <div class="campaign-contact__actions">
            <span class="campaign-pill ${contact.unread ? "campaign-pill--warn" : "campaign-pill--muted"}">${contact.unread ? "Unread" : "Seen"}</span>
            <button class="btn btn--secondary btn--sm" type="button" data-campaign-add="${esc(contact.identity_key)}">${selectedIds.has(contact.identity_key) ? "Added" : "Add"}</button>
          </div>
        </div>
      </div>
    `).join("");
    if (contacts.length > 20) {
      list.insertAdjacentHTML("beforeend", `<p class="field-hint">Showing the first 20 contacts out of ${contacts.length} matched contacts.</p>`);
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
      const visibleCampaigns = state.campaignHistoryCache.slice(0, 4);
      list.innerHTML = visibleCampaigns.map((campaign) => `
        <div class="campaign-history__item">
          <div class="campaign-history__row">
            <div class="campaign-history__meta">
              <span class="campaign-history__name">${esc(campaign.name)}</span>
              <span class="campaign-history__sub">${esc(campaign.status)} | ${campaign.selected_contact_ids?.length || campaign.matched_count} selected | ${formatTimestamp(campaign.created_at)}</span>
            </div>
            <div class="campaign-history__actions">
              <span class="campaign-pill ${campaign.failed_count > 0 ? "campaign-pill--warn" : "campaign-pill--ok"}">${campaign.sent_count}/${campaign.matched_count}</span>
              <button class="btn btn--secondary btn--sm" type="button" data-campaign-load="${campaign.campaign_id}">Load</button>
            </div>
          </div>
        </div>
      `).join("");
      if (state.campaignHistoryCache.length > visibleCampaigns.length) {
        list.insertAdjacentHTML("beforeend", `<p class="field-hint">Showing latest ${visibleCampaigns.length} of ${state.campaignHistoryCache.length} campaigns.</p>`);
      }
    } catch (err) {
      log(`Campaign history load error: ${err.message}`, "error");
    }
  }

  function loadCampaignIntoBuilder(campaignId) {
    const campaign = state.campaignHistoryCache.find((item) => Number(item.campaign_id) === Number(campaignId));
    if (!campaign) {
      log("Campaign not found in history.", "error");
      return;
    }
    $("#campaign-name").value = campaign.name || "";
    $("#msg-content").value = campaign.message || "";
    setCampaignFilters(campaign.filters || {});
    state.campaignSelectionState.selectedContacts = campaign.matched_contacts || [];
    state.campaignSelectionState.previewContacts = campaign.matched_contacts || [];
    renderCampaignSelection();
    renderCampaignPreview(state.campaignSelectionState.previewContacts);
    setMessageMode("campaign");
    $("#toggle-message-mode").querySelectorAll(".toggle-btn").forEach((btn) => {
      btn.classList.toggle("toggle-btn--active", btn.dataset.value === "campaign");
    });
    log(`Loaded campaign '${campaign.name}'.`, "success");
  }

  async function createCampaignDraft() {
    const name = $("#campaign-name").value.trim();
    const message = $("#msg-content").value.trim();
    if (!name) throw new Error("Campaign name is required.");
    if (!message) throw new Error("Campaign message cannot be empty.");
    if (!state.campaignSelectionState.selectedContacts.length) {
      throw new Error("Select at least one campaign recipient before saving.");
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
      onUpdate: (job) => renderCampaignExecutionJob(job),
      onComplete: async (job) => {
        renderCampaignExecutionJob(job);
        stopCampaignProgressPolling();
        await loadCampaignHistory();
        await pollCampaignProgress(campaignId).catch(() => {});
      },
      onError: (err) => {
        stopCampaignProgressPolling();
        showTaskResultError("msg-result", err.message);
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
      const btn = $("#btn-auth-login");
      btn.disabled = true;
      btn.textContent = "Signing In...";
      try {
        const session = await apiRequest("/api/auth/login", {
          method: "POST",
          body: { email, password },
        });
        applyAuthSession(session, "success");
        log(`Signed in as ${session.user.email}.`, "success");
        await hydrateWorkspace();
      } catch (err) {
        setAuthMessage(err.message, "error");
        log(`Login error: ${err.message}`, "error");
      } finally {
        btn.disabled = false;
        btn.textContent = "Sign In";
      }
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
      const btn = $("#btn-auth-register");
      btn.disabled = true;
      btn.textContent = "Creating...";
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
        log(`Registered ${session.user.email}${workspace ? ` with workspace '${workspace.name}'` : ""}.`, "success");
        await hydrateWorkspace();
      } catch (err) {
        setAuthMessage(err.message, "error");
        log(`Registration error: ${err.message}`, "error");
      } finally {
        btn.disabled = false;
        btn.textContent = "Sign Up";
      }
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
      log("Signed out.", "success");
    });

    $("#btn-create-workspace").addEventListener("click", async () => {
      const name = $("#new-workspace-name").value.trim();
      if (!name) {
        setAuthMessage("Workspace name is required.", "error");
        return;
      }
      const btn = $("#btn-create-workspace");
      btn.disabled = true;
      btn.textContent = "Creating...";
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
        log(`Created workspace '${result.workspace.name}'.`, "success");
        await hydrateWorkspace();
      } catch (err) {
        setAuthMessage(err.message, "error");
        log(`Workspace create error: ${err.message}`, "error");
      } finally {
        btn.disabled = false;
        btn.textContent = "Create";
      }
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
        log(`Switched to workspace '${result.workspace.name}'.`, "success");
        await hydrateWorkspace();
      } catch (err) {
        event.target.value = previousValue || "";
        setAuthMessage(err.message, "error");
        log(`Workspace switch error: ${err.message}`, "error");
      } finally {
        event.target.disabled = false;
      }
    });
  }

  function bindLogin() {
    $("#btn-login-start").addEventListener("click", async () => {
      if (!state.auth.authenticated) {
        setAuthMessage("Sign in first to start the workspace login browser.", "error");
        return;
      }
      const btnStart = $("#btn-login-start");
      btnStart.disabled = true;
      $("#btn-login-stop").disabled = false;
      $("#login-state-text").textContent = "Starting browser...";
      $("#login-state-detail").textContent = "Please wait...";
      $("#login-state-icon").className = "login-state login-state--waiting";
      log("Starting workspace login browser...");
      try {
        const data = await apiRequest("/api/login/start", { method: "POST" });
        renderZaloLoginState(data);
        log("Zalo login started. Scan the QR code shown on this page.");
        startLoginPolling();
      } catch (err) {
        renderZaloLoginState({ state: "error", message: err.message });
        log(err.message, "error");
      }
    });

    $("#btn-login-stop").addEventListener("click", async () => {
      stopLoginPolling();
      try {
        const data = await apiRequest("/api/login/stop", { method: "POST" });
        renderZaloLoginState(data);
      } catch (err) {
        renderZaloLoginState({ state: "error", message: err.message });
      }
    });

    $("#btn-login-sync-contacts").addEventListener("click", () => {
      const contactsNav = $('.nav-item[data-module="contacts"]');
      if (contactsNav && !contactsNav.disabled) {
        contactsNav.click();
        $("#btn-sync-contacts").click();
      }
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
    $("#contact-picker-backdrop").addEventListener("click", closeManualPicker);
    $("#contact-picker-search").addEventListener("input", () => {
      filterManualPickerContacts();
      renderManualPickerList();
    });
    $("#contact-picker-list").addEventListener("click", (event) => {
      const btn = event.target.closest("button[data-contact-name]");
      if (!btn) return;
      const name = btn.dataset.contactName || "";
      if (!name) return;
      if (state.manualPickerState.mode === "campaign") {
        const identityKey = btn.dataset.contactIdentity || "";
        const contact = state.manualPickerState.contacts.find((item) => item.identity_key === identityKey);
        if (!contact) return;
        const added = addCampaignContacts([contact]);
        renderManualPickerList();
        log(added ? `Added '${contact.name}' to campaign recipients.` : `'${contact.name}' is already selected for this campaign.`, added ? "success" : "");
        return;
      }
      const added = appendManualTarget(name);
      renderManualPickerList();
      log(added ? `Added '${name}' to manual targets.` : `'${name}' is already in the manual target list.`, added ? "success" : "");
    });

    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && $("#contact-picker-modal").style.display !== "none") {
        closeManualPicker();
      }
    });

    $("#campaign-preview-list").addEventListener("click", (event) => {
      const btn = event.target.closest("button[data-campaign-add]");
      if (!btn) return;
      const identityKey = btn.dataset.campaignAdd;
      const contact = state.campaignSelectionState.previewContacts.find((item) => item.identity_key === identityKey);
      if (!contact) return;
      const added = addCampaignContacts([contact]);
      log(added ? `Added '${contact.name}' to campaign recipients.` : `'${contact.name}' is already selected for this campaign.`, added ? "success" : "");
    });

    $("#campaign-selection-list").addEventListener("click", (event) => {
      const btn = event.target.closest("button[data-campaign-remove]");
      if (!btn) return;
      const identityKey = btn.dataset.campaignRemove;
      const contact = state.campaignSelectionState.selectedContacts.find((item) => item.identity_key === identityKey);
      removeCampaignContact(identityKey);
      if (contact) {
        log(`Removed '${contact.name}' from campaign recipients.`);
      }
    });

    $("#campaign-history-list").addEventListener("click", (event) => {
      const btn = event.target.closest("button[data-campaign-load]");
      if (!btn) return;
      loadCampaignIntoBuilder(btn.dataset.campaignLoad);
    });

    $("#btn-campaign-clear-selection").addEventListener("click", () => {
      if (!state.campaignSelectionState.selectedContacts.length) return;
      clearCampaignSelection();
      log("Cleared campaign recipient selection.");
    });

    $("#btn-campaign-add-all").addEventListener("click", () => {
      if (!state.campaignSelectionState.previewContacts.length) {
        log("Preview matches first, then add contacts to the campaign.", "error");
        return;
      }
      const added = addCampaignContacts(state.campaignSelectionState.previewContacts);
      log(added > 0 ? `Added ${added} contact(s) to campaign recipients.` : "All previewed contacts are already selected.", added > 0 ? "success" : "");
    });

    $("#btn-msg-send").addEventListener("click", async () => {
      const raw = $("#msg-targets").value.trim();
      const message = $("#msg-content").value.trim();
      if (!raw) return alert("Enter target phone numbers or names.");
      if (!message) return alert("Enter a message.");
      const targets = raw.split("\n").map((line) => line.trim()).filter(Boolean);
      const delayMin = parseFloat($("#msg-delay-min").value) || 15;
      const delayMax = parseFloat($("#msg-delay-max").value) || 30;
      const btn = $("#btn-msg-send");
      btn.disabled = true;
      btn.innerHTML = '<span class="spinner-inline"></span> Queueing...';
      try {
        await queueBackgroundJob({
          label: "Messaging",
          submit: () => apiRequest("/api/message/send", {
            method: "POST",
            body: { targets, message, delay_min: delayMin, delay_max: delayMax },
          }),
          onQueued: (submission) => {
            showTaskResultInfo("msg-result", "Messaging queued", `Job ${submission.job_id} is waiting for the worker.`);
          },
          onUpdate: (job) => renderMessageJobResult(job),
          onComplete: (job) => renderMessageJobResult(job),
          onError: (err) => {
            showTaskResultError("msg-result", err.message);
            log(`Messaging error: ${err.message}`, "error");
          },
        });
      } finally {
        btn.disabled = false;
        btn.innerHTML = "Send Messages";
      }
    });

    $("#btn-campaign-preview").addEventListener("click", async () => {
      try {
        const preview = await previewCampaignMatches();
        log(`Campaign preview matched ${preview.contacts.length} contact(s).`, "success");
      } catch (err) {
        log(`Campaign preview error: ${err.message}`, "error");
      }
    });

    $("#btn-campaign-save").addEventListener("click", async () => {
      try {
        const data = await createCampaignDraft();
        log(data.message || `Campaign '${data.campaign.name}' saved.`, "success");
      } catch (err) {
        log(`Campaign save error: ${err.message}`, "error");
      }
    });

    $("#btn-campaign-execute").addEventListener("click", async () => {
      const btn = $("#btn-campaign-execute");
      btn.disabled = true;
      btn.innerHTML = '<span class="spinner-inline"></span> Queueing...';
      try {
        const submission = await executeCampaignDraft();
        showTaskResultInfo("msg-result", "Campaign queued", `Job ${submission.job_id} is waiting for the worker.`);
      } catch (err) {
        stopCampaignProgressPolling();
        showTaskResultError("msg-result", err.message);
        log(`Campaign execution error: ${err.message}`, "error");
      } finally {
        btn.disabled = false;
        btn.innerHTML = "Save + Execute";
      }
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
      if (!raw) return alert("Enter phone numbers.");
      const phoneNumbers = raw.split("\n").map((line) => line.trim()).filter(Boolean);
      const greeting = $("#friend-greeting").value.trim() || null;
      const btn = $("#btn-friend-send");
      btn.disabled = true;
      btn.innerHTML = '<span class="spinner-inline"></span> Queueing...';
      try {
        await queueBackgroundJob({
          label: "Friend request",
          submit: () => apiRequest("/api/friends/add", {
            method: "POST",
            body: { phone_numbers: phoneNumbers, greeting_message: greeting },
          }),
          onQueued: (submission) => showTaskResultInfo("friend-result", "Friend requests queued", `Job ${submission.job_id} is waiting for the worker.`),
          onUpdate: (job) => renderFriendJobResult(job),
          onComplete: (job) => renderFriendJobResult(job),
          onError: (err) => {
            showTaskResultError("friend-result", err.message);
            log(`Friend request error: ${err.message}`, "error");
          },
        });
      } finally {
        btn.disabled = false;
        btn.innerHTML = "Send Friend Requests";
      }
    });
  }

  function bindGroups() {
    $("#btn-group-send").addEventListener("click", async () => {
      const groupName = $("#group-name").value.trim();
      const message = $("#group-message").value.trim();
      if (!groupName) return alert("Enter a group name.");
      if (!message) return alert("Enter a message.");
      const btn = $("#btn-group-send");
      btn.disabled = true;
      btn.innerHTML = '<span class="spinner-inline"></span> Queueing...';
      try {
        await queueBackgroundJob({
          label: "Group message",
          submit: () => apiRequest("/api/groups/message", {
            method: "POST",
            body: { group_name: groupName, message },
          }),
          onQueued: (submission) => showTaskResultInfo("group-result", "Group message queued", `Job ${submission.job_id} is waiting for the worker.`),
          onUpdate: (job) => renderGroupJobResult(job),
          onComplete: (job) => renderGroupJobResult(job),
          onError: (err) => {
            showTaskResultError("group-result", err.message);
            log(`Group error: ${err.message}`, "error");
          },
        });
      } finally {
        btn.disabled = false;
        btn.innerHTML = "Send to Group";
      }
    });
  }

  function bindContacts() {
    $("#btn-sync-contacts").addEventListener("click", async () => {
      const btn = $("#btn-sync-contacts");
      btn.disabled = true;
      btn.innerHTML = '<span class="spinner-inline"></span> Queueing...';
      try {
        await queueBackgroundJob({
          label: "Contact sync",
          submit: () => apiRequest("/api/contacts/sync", { method: "POST" }),
          onQueued: (submission) => {
            $("#contacts-block").style.display = "none";
            setContactsStatus("progress", `Sync job ${submission.job_id} queued. Waiting for the worker.`);
            showTaskResultInfo("contacts-result", "Contact Sync Queued", `Job ${submission.job_id} is waiting for the worker.`);
          },
          onUpdate: (job) => renderContactSyncJob(job),
          onComplete: (job) => renderContactSyncJob(job),
          onError: (err) => {
            $("#contacts-block").style.display = "none";
            setContactsStatus("error", err.message);
            showTaskResultError("contacts-result", err.message);
            log(`Contact sync error: ${err.message}`, "error");
          },
        });
      } finally {
        btn.disabled = false;
        btn.innerHTML = "Sync Contacts";
      }
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
      try {
        const saved = await apiRequest("/api/settings", {
          method: "POST",
          body: settingsPayload,
        });
        $("#proxy-raw").value = formatLegacyProxyValue(saved);
        log("Settings saved.", "success");
      } catch (err) {
        log(`Settings save error: ${err.message}`, "error");
      }
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
    setMessageMode("manual");
    handleSignedOut("Sign in to unlock workspace data and automation actions.", true);
    await bootstrapSession();
  }

  initialize();
})();
