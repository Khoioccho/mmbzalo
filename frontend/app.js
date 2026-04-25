/**
 * MMBZalo — Dashboard Client Logic
 * Handles: Login flow, Messaging, Friend Requests, Groups, Contacts, Settings
 */

(() => {
  "use strict";

  const $ = (sel) => document.querySelector(sel);
  const $$ = (sel) => document.querySelectorAll(sel);

  // ═══════════════════════════════════════════════════════════════
  //  HEALTH CHECK
  // ═══════════════════════════════════════════════════════════════

  async function checkHealth() {
    try {
      const res = await fetch("/api/health");
      if (res.ok) {
        const dot = $("#health-badge .status-dot");
        dot.className = "status-dot status-dot--ok";
        $("#health-badge span:last-child").textContent = "API Online";
      }
    } catch {
      const dot = $("#health-badge .status-dot");
      dot.className = "status-dot status-dot--err";
      $("#health-badge span:last-child").textContent = "API Offline";
    }
  }
  checkHealth();

  // ═══════════════════════════════════════════════════════════════
  //  MODULE NAVIGATION
  // ═══════════════════════════════════════════════════════════════

  const navItems = $$(".nav-item");
  const modules = $$(".module");

  navItems.forEach((btn) => {
    btn.addEventListener("click", () => {
      navItems.forEach((b) => b.classList.remove("nav-item--active"));
      btn.classList.add("nav-item--active");

      const target = btn.dataset.module;
      modules.forEach((m) => {
        m.style.display = m.id === `mod-${target}` ? "block" : "none";
      });
    });
  });

  // ═══════════════════════════════════════════════════════════════
  //  ACTIVITY LOG
  // ═══════════════════════════════════════════════════════════════

  function log(msg, type = "") {
    const logEl = $("#activity-log");
    const empty = logEl.querySelector(".log-empty");
    if (empty) empty.remove();

    const now = new Date().toLocaleTimeString("en-GB", { hour12: false });
    const entry = document.createElement("div");
    entry.className = "log-entry";
    entry.innerHTML = `<span class="log-time">${now}</span><span class="log-msg ${type ? "log-msg--" + type : ""}">${esc(msg)}</span>`;
    logEl.prepend(entry);

    // Keep only last 50 entries
    while (logEl.children.length > 50) logEl.lastChild.remove();
  }

  // ═══════════════════════════════════════════════════════════════
  //  LOGIN FLOW
  // ═══════════════════════════════════════════════════════════════

  const btnLoginStart = $("#btn-login-start");
  const btnLoginStop = $("#btn-login-stop");
  const loginStateIcon = $("#login-state-icon");
  const loginStateText = $("#login-state-text");
  const loginStateDetail = $("#login-state-detail");
  const loginInfo = $("#login-info");
  const loginName = $("#login-name");

  let loginPollInterval = null;

  btnLoginStart.addEventListener("click", async () => {
    btnLoginStart.disabled = true;
    btnLoginStop.disabled = false;
    loginStateText.textContent = "Starting browser…";
    loginStateDetail.textContent = "Please wait…";
    loginStateIcon.className = "login-state login-state--waiting";
    log("Starting login browser…");

    try {
      const res = await fetch("/api/login/start", { method: "POST" });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "Failed");

      loginStateText.textContent = "Waiting for login";
      loginStateDetail.textContent = "Scan QR code or enter phone number in the browser window.";
      log("Browser opened — waiting for QR/phone login.");

      // Start polling for login status
      startLoginPolling();

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
    try {
      await fetch("/api/login/stop", { method: "POST" });
    } catch {}
    btnLoginStart.disabled = false;
    btnLoginStop.disabled = true;
    loginStateText.textContent = "Not connected";
    loginStateDetail.textContent = 'Click "Start Login" to begin.';
    loginStateIcon.className = "login-state";
    loginInfo.style.display = "none";
    log("Login browser closed.");
  });

  function startLoginPolling() {
    stopLoginPolling();
    loginPollInterval = setInterval(async () => {
      try {
        const res = await fetch("/api/login/status");
        const data = await res.json();

        if (data.state === "authenticated") {
          stopLoginPolling();
          loginStateText.textContent = "Authenticated";
          loginStateDetail.textContent = data.profile_name
            ? `Logged in as: ${data.profile_name}`
            : "Session is active.";
          loginStateIcon.className = "login-state login-state--ok";
          loginStateIcon.innerHTML = '<svg viewBox="0 0 48 48" fill="none" stroke="currentColor" stroke-width="2"><circle cx="24" cy="24" r="20"/><path d="M14 24l6 6 14-14" stroke-linecap="round" stroke-linejoin="round"/></svg>';
          btnLoginStart.disabled = true;

          if (data.profile_name) {
            loginInfo.style.display = "flex";
            loginName.textContent = data.profile_name;
          }

          log(`Authenticated as: ${data.profile_name || "Unknown"}`, "success");

        } else if (data.state === "error" || data.state === "expired") {
          stopLoginPolling();
          loginStateText.textContent = data.state === "expired" ? "Session Expired" : "Error";
          loginStateDetail.textContent = data.message;
          loginStateIcon.className = "login-state login-state--err";
          btnLoginStart.disabled = false;
          log(data.message, "error");

        } else if (data.state === "idle") {
          stopLoginPolling();
          loginStateText.textContent = "Not connected";
          loginStateDetail.textContent = data.message;
          loginStateIcon.className = "login-state";
          btnLoginStart.disabled = false;
          btnLoginStop.disabled = true;
        }
        // "waiting_qr" → keep polling
      } catch {}
    }, 2500);
  }

  function stopLoginPolling() {
    if (loginPollInterval) {
      clearInterval(loginPollInterval);
      loginPollInterval = null;
    }
  }

  // ═══════════════════════════════════════════════════════════════
  //  MESSAGING
  // ═══════════════════════════════════════════════════════════════

  $("#btn-msg-send").addEventListener("click", async () => {
    const raw = $("#msg-targets").value.trim();
    const message = $("#msg-content").value.trim();
    if (!raw) return alert("Enter target phone numbers or names.");
    if (!message) return alert("Enter a message.");

    const targets = raw.split("\n").map((l) => l.trim()).filter(Boolean);
    const delayMin = parseFloat($("#msg-delay-min").value) || 15;
    const delayMax = parseFloat($("#msg-delay-max").value) || 30;

    const btn = $("#btn-msg-send");
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner-inline"></span> Sending…';
    log(`Sending message to ${targets.length} target(s)…`);

    try {
      const res = await fetch("/api/message/send", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ targets, message, delay_min: delayMin, delay_max: delayMax }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "Failed");

      showTaskResult("msg-result", data, "message");
      log(`Messaging done: ${data.sent}/${data.total} sent, ${data.failed} failed.`,
          data.failed > 0 ? "error" : "success");

    } catch (err) {
      showTaskResultError("msg-result", err.message);
      log(`Messaging error: ${err.message}`, "error");
    } finally {
      btn.disabled = false;
      btn.innerHTML = '<svg viewBox="0 0 20 20" fill="currentColor" width="18" height="18"><path d="M10.894 2.553a1 1 0 0 0-1.788 0l-7 14a1 1 0 0 0 1.169 1.409l5-1.429A1 1 0 0 0 9 15.571V11a1 1 0 1 1 2 0v4.571a1 1 0 0 0 .725.962l5 1.428a1 1 0 0 0 1.17-1.408l-7-14z"/></svg> Send Messages';
    }
  });

  // ═══════════════════════════════════════════════════════════════
  //  FRIEND REQUESTS
  // ═══════════════════════════════════════════════════════════════

  $("#btn-friend-send").addEventListener("click", async () => {
    const raw = $("#friend-phones").value.trim();
    if (!raw) return alert("Enter phone numbers.");

    const phoneNumbers = raw.split("\n").map((l) => l.trim()).filter(Boolean);
    const greeting = $("#friend-greeting").value.trim() || null;

    const btn = $("#btn-friend-send");
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner-inline"></span> Sending…';
    log(`Sending ${phoneNumbers.length} friend request(s)…`);

    try {
      const res = await fetch("/api/friends/add", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ phone_numbers: phoneNumbers, greeting_message: greeting }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "Failed");

      showTaskResult("friend-result", data, "friend");
      log(`Friend requests: ${data.sent}/${data.total} sent.`,
          data.failed > 0 ? "error" : "success");

    } catch (err) {
      showTaskResultError("friend-result", err.message);
      log(`Friend request error: ${err.message}`, "error");
    } finally {
      btn.disabled = false;
      btn.innerHTML = '<svg viewBox="0 0 20 20" fill="currentColor" width="18" height="18"><path d="M8 9a3 3 0 1 0 0-6 3 3 0 0 0 0 6zm5-3a2 2 0 1 1 4 0 2 2 0 0 1-4 0zm-1.07 7.94A5 5 0 0 0 3 15v3h10v-3a5 5 0 0 0-.07-.06zM15 11h2v2h2v2h-2v2h-2v-2h-2v-2h2v-2z"/></svg> Send Friend Requests';
    }
  });

  // ═══════════════════════════════════════════════════════════════
  //  GROUPS
  // ═══════════════════════════════════════════════════════════════

  $("#btn-group-send").addEventListener("click", async () => {
    const groupName = $("#group-name").value.trim();
    const message = $("#group-message").value.trim();
    if (!groupName) return alert("Enter a group name.");
    if (!message) return alert("Enter a message.");

    const btn = $("#btn-group-send");
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner-inline"></span> Sending…';
    log(`Sending message to group "${groupName}"…`);

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
        el.innerHTML = `<div class="task-result__title">✓ Message Sent</div><div class="task-result__detail">${esc(data.message)}</div>`;
        log(`Group message sent to "${groupName}".`, "success");
      } else {
        el.className = "task-result task-result--fail";
        el.innerHTML = `<div class="task-result__title">✗ Failed</div><div class="task-result__detail">${esc(data.message)}</div>`;
        log(`Group message failed: ${data.message}`, "error");
      }

    } catch (err) {
      showTaskResultError("group-result", err.message);
      log(`Group error: ${err.message}`, "error");
    } finally {
      btn.disabled = false;
      btn.innerHTML = '<svg viewBox="0 0 20 20" fill="currentColor" width="18" height="18"><path d="M10.894 2.553a1 1 0 0 0-1.788 0l-7 14a1 1 0 0 0 1.169 1.409l5-1.429A1 1 0 0 0 9 15.571V11a1 1 0 1 1 2 0v4.571a1 1 0 0 0 .725.962l5 1.428a1 1 0 0 0 1.17-1.408l-7-14z"/></svg> Send to Group';
    }
  });

  // ═══════════════════════════════════════════════════════════════
  //  CONTACTS
  // ═══════════════════════════════════════════════════════════════

  $("#btn-sync-contacts").addEventListener("click", async () => {
    const btn = $("#btn-sync-contacts");
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner-inline"></span> Syncing…';
    log("Syncing contacts…");

    try {
      const res = await fetch("/api/contacts");
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "Failed");

      if (data.contacts && data.contacts.length > 0) {
        renderContacts(data);
        log(`Synced ${data.contact_count} contact(s).`, "success");
      } else {
        $("#contacts-block").style.display = "none";
        log("No contacts found.", "error");
      }

    } catch (err) {
      log(`Contact sync error: ${err.message}`, "error");
    } finally {
      btn.disabled = false;
      btn.innerHTML = '<svg viewBox="0 0 20 20" fill="currentColor" width="18" height="18"><path fill-rule="evenodd" d="M4 2a1 1 0 0 1 1 1v2.101a7.002 7.002 0 0 1 11.601 2.566 1 1 0 1 1-1.885.666A5.002 5.002 0 0 0 5.999 7H9a1 1 0 0 1 0 2H3a1 1 0 0 1-1-1V3a1 1 0 0 1 1-1zm.008 9.057a1 1 0 0 1 1.276.61A5.002 5.002 0 0 0 14.001 13H11a1 1 0 1 1 0-2h5a1 1 0 0 1 1 1v5a1 1 0 1 1-2 0v-2.101a7.002 7.002 0 0 1-11.601-2.566 1 1 0 0 1 .61-1.276z" clip-rule="evenodd"/></svg> Sync Contacts';
    }
  });

  function renderContacts(data) {
    const block = $("#contacts-block");
    block.style.display = "block";
    $("#contacts-count-badge").textContent = `${data.contact_count} found`;

    const tbody = $("#contacts-tbody");
    tbody.innerHTML = data.contacts
      .map((c, i) => `
        <tr>
          <td>${i + 1}</td>
          <td>${c.avatar_url ? `<img src="${c.avatar_url}" class="contact-avatar" alt="" />` : "—"}</td>
          <td class="contact-name">${esc(c.name)}</td>
          <td>${c.last_message ? esc(c.last_message) : "—"}</td>
          <td>${c.unread ? '<span class="unread-dot"></span>' : "—"}</td>
        </tr>`)
      .join("");
  }

  // ═══════════════════════════════════════════════════════════════
  //  SETTINGS
  // ═══════════════════════════════════════════════════════════════

  // Load settings on start
  (async function loadSettings() {
    try {
      const res = await fetch("/api/settings");
      if (res.ok) {
        const s = await res.json();
        setToggle("toggle-lang", s.language);
        setToggle("toggle-theme", s.theme);
        setToggle("toggle-layout", s.layout);
        $("#proxy-toggle").checked = s.proxy_enabled;
        $("#proxy-fields").style.display = s.proxy_enabled ? "block" : "none";
        if (s.proxy_address) $("#proxy-address").value = s.proxy_address;
        if (s.proxy_port) $("#proxy-port").value = s.proxy_port;
        $("#setting-delay-min").value = s.delay_min;
        $("#setting-delay-max").value = s.delay_max;
      }
    } catch {}
  })();

  // Toggle groups
  $$(".toggle-group").forEach((group) => {
    group.querySelectorAll(".toggle-btn").forEach((btn) => {
      btn.addEventListener("click", () => {
        group.querySelectorAll(".toggle-btn").forEach((b) => b.classList.remove("toggle-btn--active"));
        btn.classList.add("toggle-btn--active");
      });
    });
  });

  // Proxy toggle
  $("#proxy-toggle").addEventListener("change", (e) => {
    $("#proxy-fields").style.display = e.target.checked ? "block" : "none";
  });

  // Save settings
  $("#btn-save-settings").addEventListener("click", async () => {
    const settings = {
      language: getToggle("toggle-lang"),
      theme: getToggle("toggle-theme"),
      layout: getToggle("toggle-layout"),
      proxy_enabled: $("#proxy-toggle").checked,
      proxy_address: $("#proxy-address").value || null,
      proxy_port: parseInt($("#proxy-port").value) || null,
      delay_min: parseFloat($("#setting-delay-min").value) || 15,
      delay_max: parseFloat($("#setting-delay-max").value) || 30,
    };

    try {
      const res = await fetch("/api/settings", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(settings),
      });
      if (res.ok) {
        log("Settings saved.", "success");
      }
    } catch (err) {
      log(`Settings save error: ${err.message}`, "error");
    }
  });

  function getToggle(groupId) {
    const active = $(`#${groupId} .toggle-btn--active`);
    return active ? active.dataset.value : "";
  }

  function setToggle(groupId, value) {
    const group = $(`#${groupId}`);
    if (!group) return;
    group.querySelectorAll(".toggle-btn").forEach((b) => {
      b.classList.toggle("toggle-btn--active", b.dataset.value === value);
    });
  }

  // ═══════════════════════════════════════════════════════════════
  //  HELPERS
  // ═══════════════════════════════════════════════════════════════

  function esc(str) {
    const d = document.createElement("div");
    d.textContent = str;
    return d.innerHTML;
  }

  function showTaskResult(elId, data, type) {
    const el = $(`#${elId}`);
    el.style.display = "block";

    const isSuccess = data.failed === 0;
    el.className = `task-result ${isSuccess ? "task-result--success" : "task-result--info"}`;

    const icon = isSuccess ? "✓" : "⚠";
    let items = "";
    const list = data.results || [];
    if (list.length > 0) {
      items = '<ul class="task-result__items">' +
        list.map((r) => {
          const label = type === "friend" ? r.phone : r.target;
          return r.success
            ? `<li class="success">✓ ${esc(label)}</li>`
            : `<li class="fail">✗ ${esc(label)} — ${esc(r.error || "Unknown error")}</li>`;
        }).join("") + "</ul>";
    }

    el.innerHTML = `
      <div class="task-result__title">${icon} ${esc(data.message)}</div>
      <div class="task-result__detail">Total: ${data.total} | Sent: ${data.sent} | Failed: ${data.failed}</div>
      ${items}`;
  }

  function showTaskResultError(elId, message) {
    const el = $(`#${elId}`);
    el.style.display = "block";
    el.className = "task-result task-result--fail";
    el.innerHTML = `<div class="task-result__title">✗ Error</div><div class="task-result__detail">${esc(message)}</div>`;
  }

})();
