/**
 * MMBZalo - Dashboard Client Logic
 * Handles: Login flow, Messaging, Friend Requests, Groups, Contacts, Campaigns, Settings
 */

(() => {
  "use strict";

  const $ = (sel) => document.querySelector(sel);
  const $$ = (sel) => document.querySelectorAll(sel);
  let loginPollInterval = null;
  let campaignProgressPollInterval = null;
  let campaignProgressSeen = new Set();
  let lastCampaignPreview = null;
  let campaignHistoryCache = [];
  const manualPickerState = {
    contacts: [],
    filtered: [],
    loaded: false,
    loading: false,
    error: "",
  };
  const campaignSelectionState = {
    selectedContacts: [],
    previewContacts: [],
  };

  async function checkHealth() {
    try {
      const res = await fetch("/api/health");
      if (res.ok) {
        $("#health-badge .status-dot").className = "status-dot status-dot--ok";
        $("#health-badge span:last-child").textContent = "API Online";
      }
    } catch {
      $("#health-badge .status-dot").className = "status-dot status-dot--err";
      $("#health-badge span:last-child").textContent = "API Offline";
    }
  }

  function log(msg, type = "") {
    const logEl = $("#activity-log");
    const empty = logEl.querySelector(".log-empty");
    if (empty) empty.remove();
    const now = new Date().toLocaleTimeString("en-GB", { hour12: false });
    const entry = document.createElement("div");
    entry.className = "log-entry";
    entry.innerHTML = `<span class="log-time">${now}</span><span class="log-msg ${type ? "log-msg--" + type : ""}">${esc(msg)}</span>`;
    logEl.prepend(entry);
    while (logEl.children.length > 50) logEl.lastChild.remove();
  }

  function resetCampaignProgressUI(total = 0) {
    campaignProgressSeen = new Set();
    $("#campaign-progress").style.display = "block";
    $("#campaign-progress-count").textContent = total ? `0/${total} sent` : "Starting";
    $("#campaign-progress-list").innerHTML = '<div class="campaign-progress__item">Preparing campaign...</div>';
  }

  function appendCampaignProgressEvent(event) {
    if (!event || campaignProgressSeen.has(event.sequence)) return;
    campaignProgressSeen.add(event.sequence);
    const list = $("#campaign-progress-list");
    const preparing = list.querySelector(".campaign-progress__item");
    if (preparing && preparing.textContent === "Preparing campaign...") preparing.remove();
    const item = document.createElement("div");
    item.className = `campaign-progress__item ${event.success === true ? "campaign-progress__item--success" : ""} ${event.success === false ? "campaign-progress__item--error" : ""}`;
    const route = event.route ? `<span class="campaign-progress__route">${esc(event.route.replace("_", " "))}</span>` : "";
    item.innerHTML = `<span>${esc(event.message || "")}</span>${route}`;
    list.prepend(item);
    while (list.children.length > 30) list.lastChild.remove();
    if (event.message) log(event.message, event.level === "error" || event.success === false ? "error" : event.success === true ? "success" : "");
  }

  async function pollCampaignProgress(campaignId) {
    const res = await fetch(`/api/campaigns/${campaignId}/progress`);
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Failed to load campaign progress.");
    $("#campaign-progress").style.display = "block";
    $("#campaign-progress-count").textContent = data.total ? `${data.sent}/${data.total} sent` : data.status;
    (data.events || []).forEach(appendCampaignProgressEvent);
    if (data.status === "completed" || data.status === "failed") {
      stopCampaignProgressPolling();
    }
    return data;
  }

  function startCampaignProgressPolling(campaignId, total = 0) {
    stopCampaignProgressPolling();
    resetCampaignProgressUI(total);
    pollCampaignProgress(campaignId).catch(() => {});
    campaignProgressPollInterval = setInterval(() => {
      pollCampaignProgress(campaignId).catch((err) => {
        log(`Campaign progress error: ${err.message}`, "error");
        stopCampaignProgressPolling();
      });
    }, 1000);
  }

  function stopCampaignProgressPolling() {
    if (campaignProgressPollInterval) {
      clearInterval(campaignProgressPollInterval);
      campaignProgressPollInterval = null;
    }
  }

  function setContactsStatus(state, message) {
    const el = $("#contacts-status");
    el.className = `contacts-status contacts-status--${state}`;
    el.textContent = message;
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

  function formatLegacyProxyValue(settings) {
    if (settings.proxy_raw) return settings.proxy_raw;
    if (settings.proxy_address && settings.proxy_port) {
      return `${settings.proxy_address}:${settings.proxy_port}`;
    }
    return "";
  }

  async function readErrorMessage(res, fallback) {
    try {
      const data = await res.json();
      if (typeof data.detail === "string" && data.detail) return data.detail;
      if (Array.isArray(data.detail) && data.detail.length) {
        return data.detail.map((item) => item.msg || fallback).join("; ");
      }
    } catch {}
    return fallback;
  }

  function esc(str) {
    const d = document.createElement("div");
    d.textContent = str ?? "";
    return d.innerHTML;
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

  function setContactPickerStatus(message, type = "neutral") {
    const el = $("#contact-picker-status");
    el.textContent = message;
    el.dataset.state = type;
  }

  function filterManualPickerContacts() {
    const query = normalizeSearchText($("#contact-picker-search").value);
    manualPickerState.filtered = manualPickerState.contacts.filter((contact) => {
      return !query || normalizeSearchText(contact.name).includes(query);
    });
  }

  function renderManualPickerList() {
    const list = $("#contact-picker-list");
    const lookup = getManualTargetLookup();

    if (manualPickerState.loading) {
      list.innerHTML = '<div class="picker-empty">Loading stored contacts...</div>';
      return;
    }

    if (manualPickerState.error) {
      list.innerHTML = `<div class="picker-empty">${esc(manualPickerState.error)}</div>`;
      return;
    }

    if (!manualPickerState.contacts.length) {
      list.innerHTML = '<div class="picker-empty">No stored contacts yet. Sync contacts first in the Contacts tab.</div>';
      return;
    }

    if (!manualPickerState.filtered.length) {
      list.innerHTML = '<div class="picker-empty">No stored contacts matched the current search.</div>';
      return;
    }

    list.innerHTML = manualPickerState.filtered.map((contact) => {
      const isAdded = lookup.has(normalizeSearchText(contact.name));
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
            <button class="btn btn--secondary btn--sm" type="button" data-contact-name="${esc(contact.name)}">${isAdded ? "Added" : "Add"}</button>
          </div>
        </div>
      `;
    }).join("");
  }

  async function loadManualPickerContacts() {
    manualPickerState.loading = true;
    manualPickerState.error = "";
    manualPickerState.contacts = [];
    manualPickerState.filtered = [];
    setContactPickerStatus("Loading stored contacts...", "loading");
    renderManualPickerList();

    try {
      const res = await fetch("/api/contacts");
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "Failed to load stored contacts.");
      manualPickerState.contacts = data.contacts || [];
      manualPickerState.loaded = true;
      filterManualPickerContacts();
      if (manualPickerState.contacts.length) {
        setContactPickerStatus(`Loaded ${manualPickerState.contacts.length} stored contact(s). Select names to add them into the manual target list.`, "success");
      } else {
        setContactPickerStatus("No stored contacts yet. Sync contacts first in the Contacts tab.", "empty");
      }
    } catch (err) {
      manualPickerState.error = err.message;
      setContactPickerStatus(err.message, "error");
      log(`Contact picker load error: ${err.message}`, "error");
    } finally {
      manualPickerState.loading = false;
      renderManualPickerList();
    }
  }

  async function openManualPicker() {
    const modal = $("#contact-picker-modal");
    modal.style.display = "flex";
    modal.setAttribute("aria-hidden", "false");
    document.body.classList.add("modal-open");
    $("#contact-picker-search").value = "";
    $("#contact-picker-search").focus();
    await loadManualPickerContacts();
  }

  function closeManualPicker() {
    const modal = $("#contact-picker-modal");
    modal.style.display = "none";
    modal.setAttribute("aria-hidden", "true");
    document.body.classList.remove("modal-open");
  }

  function showTaskResult(elId, data, type) {
    const el = $(`#${elId}`);
    el.style.display = "block";
    const isSuccess = data.failed === 0;
    el.className = `task-result ${isSuccess ? "task-result--success" : "task-result--info"}`;
    const icon = isSuccess ? "OK" : "WARN";
    const list = data.results || [];
    const items = list.length > 0
      ? `<ul class="task-result__items">${
          list.map((r) => {
            const label = type === "friend" ? r.phone : r.target;
            return r.success
              ? `<li class="success">OK ${esc(label)}</li>`
              : `<li class="fail">ERR ${esc(label)} - ${esc(r.error || "Unknown error")}</li>`;
          }).join("")
        }</ul>`
      : "";

    el.innerHTML = `
      <div class="task-result__title">${icon} ${esc(data.message)}</div>
      <div class="task-result__detail">Total: ${data.total} | Sent: ${data.sent} | Failed: ${data.failed}</div>
      ${items}`;
  }

  function showTaskResultError(elId, message) {
    const el = $(`#${elId}`);
    el.style.display = "block";
    el.className = "task-result task-result--fail";
    el.innerHTML = `<div class="task-result__title">ERR Error</div><div class="task-result__detail">${esc(message)}</div>`;
  }

  function updateContactsMeta(data) {
    $("#contacts-meta-count").textContent = String(data.stored_contact_count || 0);
    $("#contacts-meta-status").textContent = data.last_sync_status || "Never synced";
    $("#contacts-meta-time").textContent = data.last_sync_at ? formatTimestamp(data.last_sync_at) : "No stored sync yet";
  }

  function renderContacts(data) {
    $("#contacts-block").style.display = "block";
    $("#contacts-count-badge").textContent = `${data.stored_contact_count || data.contact_count} stored`;
    $("#contacts-tbody").innerHTML = (data.contacts || []).map((c, index) => `
      <tr>
        <td>${index + 1}</td>
        <td>${c.avatar_url ? `<img src="${c.avatar_url}" class="contact-avatar" alt="" />` : "-"}</td>
        <td class="contact-name">${esc(c.name)}</td>
        <td>${c.last_message ? esc(c.last_message) : "-"}</td>
        <td>${c.unread ? '<span class="unread-dot"></span>' : "-"}</td>
      </tr>
    `).join("");
  }

  async function loadStoredContacts() {
    try {
      const res = await fetch("/api/contacts");
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "Failed");
      updateContactsMeta(data);
      if (data.contacts && data.contacts.length > 0) {
        renderContacts(data);
        setContactsStatus(data.last_sync_status === "partial" ? "partial" : "success", data.message || `Loaded ${data.contact_count} stored contact(s).`);
      } else {
        $("#contacts-block").style.display = "none";
        setContactsStatus("empty", data.message || "No stored contacts yet. Run a live sync to populate the contact store.");
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
      selected_ids: campaignSelectionState.selectedContacts.map((contact) => contact.identity_key).filter(Boolean),
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
    const existing = new Map(campaignSelectionState.selectedContacts.map((contact) => [contact.identity_key, contact]));
    let added = 0;
    for (const contact of contacts) {
      if (!contact || !contact.identity_key || existing.has(contact.identity_key)) continue;
      existing.set(contact.identity_key, contact);
      added += 1;
    }
    campaignSelectionState.selectedContacts = Array.from(existing.values());
    renderCampaignSelection();
    renderCampaignPreview(campaignSelectionState.previewContacts);
    return added;
  }

  function removeCampaignContact(identityKey) {
    campaignSelectionState.selectedContacts = campaignSelectionState.selectedContacts.filter(
      (contact) => contact.identity_key !== identityKey,
    );
    renderCampaignSelection();
    renderCampaignPreview(campaignSelectionState.previewContacts);
  }

  function clearCampaignSelection() {
    campaignSelectionState.selectedContacts = [];
    renderCampaignSelection();
    renderCampaignPreview(campaignSelectionState.previewContacts);
  }

  function renderCampaignSelection() {
    const list = $("#campaign-selection-list");
    const count = $("#campaign-selected-count");
    const contacts = campaignSelectionState.selectedContacts;
    count.textContent = `${contacts.length} selected`;

    if (!contacts.length) {
      list.innerHTML = '<p class="field-hint">No campaign recipients selected yet. Preview matches, then add the contacts you want.</p>';
      return;
    }

    list.innerHTML = contacts.map((contact) => `
      <div class="campaign-selection__item">
        <div class="campaign-selection__row">
          <div class="campaign-selection__meta">
            <span class="campaign-selection__name">${esc(contact.name)}</span>
            <span class="campaign-selection__sub">${esc(contact.identity_source || "unknown")} | ${contact.last_seen_at ? esc(formatTimestamp(contact.last_seen_at)) : "No last seen timestamp"}</span>
          </div>
          <div class="campaign-selection__actions-inline">
            <span class="campaign-pill ${contact.unread ? "campaign-pill--warn" : "campaign-pill--muted"}">${contact.unread ? "Unread" : "Seen"}</span>
            <button class="btn btn--secondary btn--sm" type="button" data-campaign-remove="${esc(contact.identity_key)}">Remove</button>
          </div>
        </div>
      </div>
    `).join("");
  }

  function buildContactsQuery(filters) {
    const params = new URLSearchParams();
    if (filters.search) params.set("search", filters.search);
    if (filters.unread_only) params.set("unread_only", "true");
    if (filters.identity_source && filters.identity_source !== "all") params.set("identity_source", filters.identity_source);
    if (filters.sort_by) params.set("sort_by", filters.sort_by);
    if (filters.sort_order) params.set("sort_order", filters.sort_order);
    if (filters.selected_ids && filters.selected_ids.length > 0) params.set("selected_ids", filters.selected_ids.join(","));
    return params.toString();
  }

  async function previewCampaignMatches() {
    const filters = getCampaignDiscoveryFilters();
    const query = buildContactsQuery(filters);
    const res = await fetch(`/api/contacts${query ? `?${query}` : ""}`);
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Failed");
    campaignSelectionState.previewContacts = data.contacts || [];
    lastCampaignPreview = { filters, contacts: campaignSelectionState.previewContacts, storedContactCount: data.stored_contact_count || 0 };
    renderCampaignPreview(campaignSelectionState.previewContacts);
    return lastCampaignPreview;
  }

  function renderCampaignPreview(contacts) {
    const preview = $("#campaign-preview");
    const list = $("#campaign-preview-list");
    const count = $("#campaign-preview-count");
    const selectedIds = new Set(campaignSelectionState.selectedContacts.map((contact) => contact.identity_key));
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
    try {
      const res = await fetch("/api/campaigns");
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "Failed");
      campaignHistoryCache = data.campaigns || [];
      const list = $("#campaign-history-list");
      if (!campaignHistoryCache.length) {
        list.innerHTML = '<p class="field-hint">No campaigns yet.</p>';
        return;
      }
      const visibleCampaigns = campaignHistoryCache.slice(0, 4);
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
      if (campaignHistoryCache.length > visibleCampaigns.length) {
        list.insertAdjacentHTML("beforeend", `<p class="field-hint">Showing latest ${visibleCampaigns.length} of ${campaignHistoryCache.length} campaigns.</p>`);
      }
    } catch (err) {
      log(`Campaign history load error: ${err.message}`, "error");
    }
  }

  function loadCampaignIntoBuilder(campaignId) {
    const campaign = campaignHistoryCache.find((item) => Number(item.campaign_id) === Number(campaignId));
    if (!campaign) {
      log("Campaign not found in history.", "error");
      return;
    }

    $("#campaign-name").value = campaign.name || "";
    $("#msg-content").value = campaign.message || "";
    setCampaignFilters(campaign.filters || {});
    campaignSelectionState.selectedContacts = (campaign.matched_contacts || []).slice();
    renderCampaignSelection();
    campaignSelectionState.previewContacts = [];
    $("#campaign-preview").style.display = "none";
    log(`Loaded campaign '${campaign.name}' into the builder.`, "success");
  }

  async function createCampaignDraft() {
    const name = $("#campaign-name").value.trim();
    const message = $("#msg-content").value.trim();
    if (!name) throw new Error("Campaign name is required.");
    if (!message) throw new Error("Campaign message cannot be empty.");
    if (!campaignSelectionState.selectedContacts.length) {
      throw new Error("Select at least one campaign recipient before saving.");
    }
    const filters = getCampaignFilters();
    const res = await fetch("/api/campaigns", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        name,
        message,
        filters,
      }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Failed");
    await loadCampaignHistory();
    return data;
  }

  async function executeCampaignDraft() {
    const campaignResult = await createCampaignDraft();
    const delayMin = parseFloat($("#campaign-delay-min").value);
    const delayMax = parseFloat($("#campaign-delay-max").value);
    const campaignId = campaignResult.campaign.campaign_id;
    startCampaignProgressPolling(campaignId, campaignResult.campaign.matched_count || 0);
    const res = await fetch(`/api/campaigns/${campaignId}/execute`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        delay_min: Number.isFinite(delayMin) ? delayMin : 1,
        delay_max: Number.isFinite(delayMax) ? delayMax : 3,
      }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Failed");
    await pollCampaignProgress(campaignId).catch(() => {});
    stopCampaignProgressPolling();
    await loadCampaignHistory();
    return data;
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
        $$(".nav-item").forEach((item) => item.classList.remove("nav-item--active"));
        btn.classList.add("nav-item--active");
        const target = btn.dataset.module;
        $$(".module").forEach((m) => {
          m.style.display = m.id === `mod-${target}` ? "block" : "none";
        });
        if (target === "contacts") loadStoredContacts();
        if (target === "messaging") loadCampaignHistory();
      });
    });
  }

  function bindLogin() {
    const btnLoginStart = $("#btn-login-start");
    const btnLoginStop = $("#btn-login-stop");
    const loginStateIcon = $("#login-state-icon");
    const loginStateText = $("#login-state-text");
    const loginStateDetail = $("#login-state-detail");
    const loginInfo = $("#login-info");
    const loginName = $("#login-name");

    btnLoginStart.addEventListener("click", async () => {
      btnLoginStart.disabled = true;
      btnLoginStop.disabled = false;
      loginStateText.textContent = "Starting browser...";
      loginStateDetail.textContent = "Please wait...";
      loginStateIcon.className = "login-state login-state--waiting";
      log("Starting login browser...");
      try {
        const res = await fetch("/api/login/start", { method: "POST" });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || "Failed");
        loginStateText.textContent = "Waiting for login";
        loginStateDetail.textContent = "Scan QR code or enter phone number in the browser window.";
        log("Browser opened - waiting for QR/phone login.");
        startLoginPolling({ btnLoginStart, btnLoginStop, loginStateIcon, loginStateText, loginStateDetail, loginInfo, loginName });
      } catch (err) {
        loginStateText.textContent = "Error";
        loginStateDetail.textContent = err.message;
        loginStateIcon.className = "login-state login-state--err";
        btnLoginStart.disabled = false;
        log(err.message, "error");
      }
    });

    btnLoginStop.addEventListener("click", async () => {
      stopLoginPolling();
      try { await fetch("/api/login/stop", { method: "POST" }); } catch {}
      btnLoginStart.disabled = false;
      btnLoginStop.disabled = true;
      loginStateText.textContent = "Not connected";
      loginStateDetail.textContent = 'Click "Start Login" to begin.';
      loginStateIcon.className = "login-state";
      loginInfo.style.display = "none";
      log("Login browser closed.");
    });
  }

  function startLoginPolling(ui) {
    stopLoginPolling();
    loginPollInterval = setInterval(async () => {
      try {
        const res = await fetch("/api/login/status");
        const data = await res.json();
        if (data.state === "authenticated") {
          stopLoginPolling();
          ui.loginStateText.textContent = "Authenticated";
          ui.loginStateDetail.textContent = data.profile_name ? `Logged in as: ${data.profile_name}` : "Session is active.";
          ui.loginStateIcon.className = "login-state login-state--ok";
          ui.loginStateIcon.innerHTML = '<svg viewBox="0 0 48 48" fill="none" stroke="currentColor" stroke-width="2"><circle cx="24" cy="24" r="20"/><path d="M14 24l6 6 14-14" stroke-linecap="round" stroke-linejoin="round"/></svg>';
          ui.btnLoginStart.disabled = true;
          if (data.profile_name) {
            ui.loginInfo.style.display = "flex";
            ui.loginName.textContent = data.profile_name;
          }
          log(`Authenticated as: ${data.profile_name || "Unknown"}`, "success");
        } else if (data.state === "error" || data.state === "expired") {
          stopLoginPolling();
          ui.loginStateText.textContent = data.state === "expired" ? "Session Expired" : "Error";
          ui.loginStateDetail.textContent = data.message;
          ui.loginStateIcon.className = "login-state login-state--err";
          ui.btnLoginStart.disabled = false;
          log(data.message, "error");
        } else if (data.state === "idle") {
          stopLoginPolling();
          ui.loginStateText.textContent = "Not connected";
          ui.loginStateDetail.textContent = data.message;
          ui.loginStateIcon.className = "login-state";
          ui.btnLoginStart.disabled = false;
          ui.btnLoginStop.disabled = true;
        }
      } catch {}
    }, 2500);
  }

  function stopLoginPolling() {
    if (loginPollInterval) {
      clearInterval(loginPollInterval);
      loginPollInterval = null;
    }
  }

  function bindMessaging() {
    $("#btn-open-contact-picker").addEventListener("click", () => {
      openManualPicker();
    });

    $("#btn-clear-msg-targets").addEventListener("click", () => {
      const textarea = $("#msg-targets");
      if (!textarea.value.trim()) return;
      textarea.value = "";
      renderManualPickerList();
      log("Cleared manual target list.");
    });

    $("#btn-close-contact-picker").addEventListener("click", () => {
      closeManualPicker();
    });

    $("#contact-picker-backdrop").addEventListener("click", () => {
      closeManualPicker();
    });

    $("#contact-picker-search").addEventListener("input", () => {
      filterManualPickerContacts();
      renderManualPickerList();
    });

    $("#contact-picker-list").addEventListener("click", (event) => {
      const btn = event.target.closest("button[data-contact-name]");
      if (!btn) return;
      const name = btn.dataset.contactName || "";
      if (!name) return;
      const added = appendManualTarget(name);
      renderManualPickerList();
      log(
        added ? `Added '${name}' to manual targets.` : `'${name}' is already in the manual target list.`,
        added ? "success" : ""
      );
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
      const contact = campaignSelectionState.previewContacts.find((item) => item.identity_key === identityKey);
      if (!contact) return;
      const added = addCampaignContacts([contact]);
      log(
        added ? `Added '${contact.name}' to campaign recipients.` : `'${contact.name}' is already selected for this campaign.`,
        added ? "success" : ""
      );
    });

    $("#campaign-selection-list").addEventListener("click", (event) => {
      const btn = event.target.closest("button[data-campaign-remove]");
      if (!btn) return;
      const identityKey = btn.dataset.campaignRemove;
      const contact = campaignSelectionState.selectedContacts.find((item) => item.identity_key === identityKey);
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
      if (!campaignSelectionState.selectedContacts.length) return;
      clearCampaignSelection();
      log("Cleared campaign recipient selection.");
    });

    $("#btn-campaign-add-all").addEventListener("click", () => {
      if (!campaignSelectionState.previewContacts.length) {
        log("Preview matches first, then add contacts to the campaign.", "error");
        return;
      }
      const added = addCampaignContacts(campaignSelectionState.previewContacts);
      log(
        added > 0 ? `Added ${added} contact(s) to campaign recipients.` : "All previewed contacts are already selected.",
        added > 0 ? "success" : ""
      );
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
      btn.innerHTML = '<span class="spinner-inline"></span> Sending...';
      log(`Sending message to ${targets.length} target(s)...`);

      try {
        const res = await fetch("/api/message/send", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ targets, message, delay_min: delayMin, delay_max: delayMax }),
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || "Failed");
        showTaskResult("msg-result", data, "message");
        log(`Messaging done: ${data.sent}/${data.total} sent, ${data.failed} failed.`, data.failed > 0 ? "error" : "success");
      } catch (err) {
        showTaskResultError("msg-result", err.message);
        log(`Messaging error: ${err.message}`, "error");
      } finally {
        btn.disabled = false;
        btn.innerHTML = 'Send Messages';
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
      btn.innerHTML = '<span class="spinner-inline"></span> Executing...';
      try {
        const data = await executeCampaignDraft();
        showTaskResult("msg-result", {
          total: data.campaign.matched_count,
          sent: data.campaign.sent_count,
          failed: data.campaign.failed_count,
          results: data.campaign.results.map((item) => ({
            target: item.target,
            success: item.success,
            error: item.error,
          })),
          message: data.message,
        }, "message");
        log(data.message, data.campaign.failed_count > 0 ? "error" : "success");
      } catch (err) {
        stopCampaignProgressPolling();
        showTaskResultError("msg-result", err.message);
        log(`Campaign execution error: ${err.message}`, "error");
      } finally {
        btn.disabled = false;
        btn.innerHTML = 'Save + Execute';
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
      btn.innerHTML = '<span class="spinner-inline"></span> Sending...';
      log(`Sending ${phoneNumbers.length} friend request(s)...`);
      try {
        const res = await fetch("/api/friends/add", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ phone_numbers: phoneNumbers, greeting_message: greeting }),
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || "Failed");
        showTaskResult("friend-result", data, "friend");
        log(`Friend requests: ${data.sent}/${data.total} sent.`, data.failed > 0 ? "error" : "success");
      } catch (err) {
        showTaskResultError("friend-result", err.message);
        log(`Friend request error: ${err.message}`, "error");
      } finally {
        btn.disabled = false;
        btn.innerHTML = 'Send Friend Requests';
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
      btn.innerHTML = '<span class="spinner-inline"></span> Sending...';
      log(`Sending message to group "${groupName}"...`);
      try {
        const res = await fetch("/api/groups/message", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ group_name: groupName, message }),
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || "Failed");
        const el = $("#group-result");
        el.style.display = "block";
        if (data.success) {
          el.className = "task-result task-result--success";
          el.innerHTML = `<div class="task-result__title">OK Message Sent</div><div class="task-result__detail">${esc(data.message)}</div>`;
          log(`Group message sent to "${groupName}".`, "success");
        } else {
          el.className = "task-result task-result--fail";
          el.innerHTML = `<div class="task-result__title">ERR Failed</div><div class="task-result__detail">${esc(data.message)}</div>`;
          log(`Group message failed: ${data.message}`, "error");
        }
      } catch (err) {
        showTaskResultError("group-result", err.message);
        log(`Group error: ${err.message}`, "error");
      } finally {
        btn.disabled = false;
        btn.innerHTML = 'Send to Group';
      }
    });
  }

  function bindContacts() {
    $("#btn-sync-contacts").addEventListener("click", async () => {
      const btn = $("#btn-sync-contacts");
      btn.disabled = true;
      setContactsStatus("progress", "Live sync in progress. Stored contacts will refresh when Zalo sync completes.");
      $("#contacts-block").style.display = "none";
      btn.innerHTML = '<span class="spinner-inline"></span> Syncing...';
      log("Syncing contacts...");
      try {
        const res = await fetch("/api/contacts/sync", { method: "POST" });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || "Failed");
        updateContactsMeta(data);
        if (data.sync_status === "success" && data.contacts && data.contacts.length > 0) {
          renderContacts(data);
          setContactsStatus("success", data.message || `Synced ${data.contact_count} contact(s).`);
          log(`Synced ${data.contact_count} contact(s).`, "success");
        } else if (data.sync_status === "partial" && data.contacts && data.contacts.length > 0) {
          renderContacts(data);
          setContactsStatus("partial", data.message || `Collected ${data.contact_count} contact(s), but sync is incomplete.`);
          log(data.message || `Collected ${data.contact_count} contact(s), but sync is incomplete.`, "error");
        } else if (data.sync_status === "empty") {
          $("#contacts-block").style.display = "none";
          setContactsStatus("empty", data.message || "The contact list appears to be empty.");
          log(data.message || "No contacts found.", "success");
        } else {
          $("#contacts-block").style.display = "none";
          setContactsStatus("error", data.message || "Contact sync failed.");
          log(data.message || "Contact sync failed.", "error");
        }
      } catch (err) {
        $("#contacts-block").style.display = "none";
        setContactsStatus("error", err.message);
        log(`Contact sync error: ${err.message}`, "error");
      } finally {
        btn.disabled = false;
        btn.innerHTML = 'Sync Contacts';
      }
    });
  }

  async function loadSettings() {
    try {
      const res = await fetch("/api/settings");
      if (!res.ok) return;
      const s = await res.json();
      setToggle("toggle-lang", s.language);
      setToggle("toggle-theme", s.theme);
      setToggle("toggle-layout", s.layout);
      $("#proxy-toggle").checked = s.proxy_enabled;
      $("#proxy-fields").style.display = s.proxy_enabled ? "block" : "none";
      $("#proxy-raw").value = formatLegacyProxyValue(s);
      $("#setting-delay-min").value = s.delay_min;
      $("#setting-delay-max").value = s.delay_max;
    } catch {}
  }

  function bindSettings() {
    $$(".toggle-group").forEach((group) => {
      if (group.id === "toggle-message-mode") return;
      group.querySelectorAll(".toggle-btn").forEach((btn) => {
        btn.addEventListener("click", () => {
          group.querySelectorAll(".toggle-btn").forEach((b) => b.classList.remove("toggle-btn--active"));
          btn.classList.add("toggle-btn--active");
        });
      });
    });

    $("#proxy-toggle").addEventListener("change", (e) => {
      $("#proxy-fields").style.display = e.target.checked ? "block" : "none";
    });

    $("#btn-save-settings").addEventListener("click", async () => {
      const settings = {
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
        const res = await fetch("/api/settings", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(settings),
        });
        if (!res.ok) {
          throw new Error(await readErrorMessage(res, "Failed to save settings."));
        }
        const saved = await res.json();
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
    bindLogin();
    bindMessaging();
    bindFriends();
    bindGroups();
    bindContacts();
    bindSettings();
    await loadSettings();
    await loadStoredContacts();
    await loadCampaignHistory();
    renderCampaignSelection();
    setMessageMode("manual");
  }

  initialize();
})();
