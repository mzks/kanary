const DEFAULT_REFRESH_MS = 5000;
const DASHBOARD_STATES = new Set(["FIRING", "ACKED", "SILENCED"]);

const state = {
  alerts: [],
  plugins: [],
  silences: [],
  meta: null,
  route: "dashboard",
  selectedRuleId: null,
  alertFilter: "",
  stateFilter: "",
  sourceFilter: "",
  ruleFilter: "",
  outputFilter: "",
  refreshMs: DEFAULT_REFRESH_MS,
  refreshTimer: null,
  timeZone: "browser",
};

function init() {
  bindControls();
  initializeTimeZone();
  restoreRoute();
  scheduleRefresh();
  refreshAll();
  window.addEventListener("hashchange", handleRouteChange);
}

function bindControls() {
  document.getElementById("refresh-now-button").addEventListener("click", refreshAll);
  document.getElementById("refresh-interval").addEventListener("change", handleRefreshIntervalChange);
  document.getElementById("timezone-select").addEventListener("change", handleTimeZoneChange);
  document.getElementById("alert-filter").addEventListener("input", (event) => {
    state.alertFilter = event.target.value.toLowerCase();
    renderAlertsPage();
  });
  document.getElementById("state-filter").addEventListener("change", (event) => {
    state.stateFilter = event.target.value;
    renderAlertsPage();
  });
  document.getElementById("source-filter").addEventListener("input", (event) => {
    state.sourceFilter = event.target.value.toLowerCase();
    renderSourcesPage();
  });
  document.getElementById("rule-filter").addEventListener("input", (event) => {
    state.ruleFilter = event.target.value.toLowerCase();
    renderRulesPage();
  });
  document.getElementById("output-filter").addEventListener("input", (event) => {
    state.outputFilter = event.target.value.toLowerCase();
    renderOutputsPage();
  });
  document.getElementById("ack-button").addEventListener("click", submitAck);
  document.getElementById("unack-button").addEventListener("click", submitUnack);
  document.getElementById("silence-for-button").addEventListener("click", submitSilenceFor);
  document.getElementById("silence-window-button").addEventListener("click", submitSilenceWindow);
  document.getElementById("admin-duration-button").addEventListener("click", submitAdminDurationSilence);
  document.getElementById("admin-window-button").addEventListener("click", submitAdminWindowSilence);
  document.getElementById("admin-reload-button").addEventListener("click", reloadEngine);
  document.getElementById("source-modal-close").addEventListener("click", closeSourceModal);
  for (const element of document.querySelectorAll("[data-close-source]")) {
    element.addEventListener("click", closeSourceModal);
  }
}

function initializeTimeZone() {
  const select = document.getElementById("timezone-select");
  const browserZone = Intl.DateTimeFormat().resolvedOptions().timeZone || "Local";
  select.innerHTML = "";
  addTimeZoneOption(select, "browser", `Browser Local (${browserZone})`);
  addTimeZoneOption(select, "UTC", "UTC+00:00 (London)");
  for (let hourOffset = -12; hourOffset <= 14; hourOffset += 1) {
    if (hourOffset === 0) {
      continue;
    }
    const offsetLabel = formatOffsetLabel(hourOffset * 60);
    addTimeZoneOption(
      select,
      `offset:${offsetLabel}`,
      `UTC${offsetLabel}${timeZoneCityLabel(offsetLabel)}`
    );
  }
  select.value = state.timeZone;
}

function restoreRoute() {
  if (!window.location.hash) {
    window.location.hash = "#dashboard";
    return;
  }
  handleRouteChange();
}

function handleRouteChange() {
  const hash = window.location.hash.replace(/^#/, "");
  if (hash.startsWith("alert/")) {
    state.route = "detail";
    state.selectedRuleId = decodeURIComponent(hash.slice("alert/".length));
  } else {
    state.route = hash || "dashboard";
  }
  renderRoute();
}

function renderRoute() {
  for (const page of document.querySelectorAll(".page")) {
    page.classList.add("hidden");
  }
  const page = document.getElementById(`page-${state.route}`) || document.getElementById("page-dashboard");
  page.classList.remove("hidden");

  for (const link of document.querySelectorAll(".nav-link")) {
    link.classList.toggle("active", link.dataset.route === state.route);
  }

  if (state.route === "detail") {
    renderDetailPage();
  }
}

function scheduleRefresh() {
  if (state.refreshTimer !== null) {
    window.clearInterval(state.refreshTimer);
    state.refreshTimer = null;
  }
  if (state.refreshMs > 0) {
    state.refreshTimer = window.setInterval(refreshAll, state.refreshMs);
  }
}

function handleRefreshIntervalChange(event) {
  state.refreshMs = Number(event.target.value);
  scheduleRefresh();
  if (state.refreshMs > 0) {
    setRefreshStatus(`Refreshing every ${Math.round(state.refreshMs / 1000)} s`, false);
  } else {
    setRefreshStatus("Manual refresh mode", false);
  }
}

function handleTimeZoneChange(event) {
  state.timeZone = event.target.value || "browser";
  renderDashboardPage();
  renderAlertsPage();
  renderSourcesPage();
  renderRulesPage();
  renderOutputsPage();
  renderSilencesPage();
  if (state.route === "detail") {
    renderDetailPage();
  }
}

async function refreshAll() {
  try {
    const [alertsPayload, pluginsPayload, silencesPayload, metaPayload] = await Promise.all([
      getJson("/alerts"),
      getJson("/plugins"),
      getJson("/silences"),
      getJson("/meta"),
    ]);
    state.alerts = alertsPayload.alerts || [];
    state.plugins = pluginsPayload.plugins || [];
    state.silences = silencesPayload.silences || [];
    state.meta = metaPayload || null;

    if (state.selectedRuleId && !state.alerts.find((alert) => alert.rule_id === state.selectedRuleId)) {
      state.selectedRuleId = null;
    }

    renderBuildMeta();
    renderDashboardPage();
    renderAlertsPage();
    renderSourcesPage();
    renderRulesPage();
    renderOutputsPage();
    renderSilencesPage();
    if (state.route === "detail") {
      renderDetailPage();
    }
    setRefreshStatus(`Updated ${new Date().toLocaleTimeString()}`, false);
  } catch (error) {
    setRefreshStatus(`Load failed: ${error.message}`, true);
  }
}

function setRefreshStatus(message, isError) {
  const element = document.getElementById("refresh-status");
  element.textContent = message;
  element.classList.toggle("status-error", Boolean(isError));
}

function renderBuildMeta() {
  const element = document.getElementById("viewer-build-meta");
  const meta = state.meta;
  if (!meta) {
    element.classList.add("hidden");
    element.innerHTML = "";
    return;
  }

  const parts = [];
  if (meta.version) {
    parts.push(`<span>Kanary ${escapeHtml(String(meta.version))}</span>`);
  }
  if (meta.git_commit) {
    parts.push(`<span>commit ${escapeHtml(shortCommit(String(meta.git_commit)))}</span>`);
  }
  if (meta.repository_url) {
    parts.push(
      `<a href="${escapeAttribute(String(meta.repository_url))}" target="_blank" rel="noopener noreferrer">GitHub Repository</a>`
    );
  } else if (meta.homepage_url) {
    parts.push(
      `<a href="${escapeAttribute(String(meta.homepage_url))}" target="_blank" rel="noopener noreferrer">Project Homepage</a>`
    );
  }
  if (meta.documentation_url) {
    parts.push(
      `<a href="${escapeAttribute(String(meta.documentation_url))}" target="_blank" rel="noopener noreferrer">Documentation</a>`
    );
  }

  if (parts.length === 0) {
    element.classList.add("hidden");
    element.innerHTML = "";
    return;
  }

  element.classList.remove("hidden");
  element.innerHTML = `
    <strong>Project Metadata</strong>
    <div class="viewer-build-meta-links">${parts.join('<span aria-hidden="true">·</span>')}</div>
  `;
}

function renderDashboardPage() {
  const activeAlerts = state.alerts.filter((alert) => DASHBOARD_STATES.has(alert.state));
  const counts = countByState(activeAlerts);
  const severityCounts = countBySeverity(activeAlerts);
  const failedPlugins = state.plugins.filter((plugin) => plugin.state === "failed").length;
  const cards = [
    { label: "FIRING", value: counts.FIRING || 0, note: "Requires attention now", className: "firing" },
    { label: "ACKED", value: counts.ACKED || 0, note: "Someone already responded", className: "acked" },
    { label: "SILENCED", value: counts.SILENCED || 0, note: "Muted by operator action", className: "silenced" },
    { label: "FAILED PLUGINS", value: failedPlugins, note: "Runtime components in failed state", className: "failed" },
  ];
  document.getElementById("dashboard-cards").innerHTML = cards
    .map(
      (card) => `
        <article class="hero-card ${card.className}">
          <div class="hero-label">${escapeHtml(card.label)}</div>
          <strong>${escapeHtml(String(card.value))}</strong>
          <div class="hero-note">${escapeHtml(card.note)}</div>
        </article>
      `
    )
    .join("");

  document.getElementById("dashboard-severity-breakdown").innerHTML = ["CRITICAL", "ERROR", "WARN", "INFO"]
    .map(
      (label) => `
        <span class="severity-chip severity-chip-${label}">
          <span class="severity-chip-label">${escapeHtml(label)}</span>
          <strong>${escapeHtml(String(severityCounts[label] || 0))}</strong>
        </span>
      `
    )
    .join("");

  const container = document.getElementById("dashboard-active-alerts");
  if (activeAlerts.length === 0) {
    container.innerHTML = `<div class="muted">No firing, acknowledged, or silenced alerts. Suppressed dependency fallout, scheduled silences, and plugin health remain available from the navigation.</div>`;
    return;
  }

  container.innerHTML = activeAlerts
    .sort(compareAlerts)
    .map(
      (alert) => `
        <article class="alert-card">
          <div class="alert-card-header">
            <div>
              <div class="alert-card-title">${escapeHtml(alert.rule_id)}</div>
              <div class="alert-card-meta">
                <span class="state-pill state-${escapeHtml(alert.state)}">${escapeHtml(alert.state)}</span>
                <span class="severity-badge severity-${severityLabel(alert.severity)}">${escapeHtml(severityLabel(alert.severity))}</span>
                <span>${escapeHtml(alert.acked_by || "Unacked")}</span>
              </div>
            </div>
            <button class="button button-secondary" data-open-rule="${escapeHtml(alert.rule_id)}">Open</button>
          </div>
          <div class="alert-card-message">${escapeHtml(alert.message || "")}</div>
          <div class="alert-card-meta">
            <span>Outputs: ${escapeHtml((alert.matched_outputs || []).join(", ") || "-")}</span>
            <span>Silences: ${escapeHtml((alert.active_silence_ids || []).join(", ") || "-")}</span>
          </div>
        </article>
      `
    )
    .join("");

  for (const button of container.querySelectorAll("[data-open-rule]")) {
    button.addEventListener("click", () => openDetail(button.dataset.openRule));
  }
}

function renderAlertsPage() {
  const tbody = document.getElementById("alerts-body");
  const alerts = state.alerts
    .filter(matchesAlertFilter)
    .sort(compareAlerts);

  tbody.innerHTML = alerts
    .map(
      (alert) => `
        <tr>
          <td>${escapeHtml(alert.rule_id)}</td>
          <td><span class="state-pill state-${escapeHtml(alert.state)}">${escapeHtml(alert.state)}</span></td>
          <td><span class="severity-badge severity-${severityLabel(alert.severity)}">${escapeHtml(severityLabel(alert.severity))}</span></td>
          <td>${escapeHtml(alert.acked_by || "-")}</td>
          <td>${escapeHtml((alert.active_silence_ids || []).join(", ") || "-")}</td>
          <td>${escapeHtml((alert.matched_outputs || []).join(", ") || "-")}</td>
          <td>${escapeHtml(alert.message || "")}</td>
          <td class="action-cell"><button class="button button-secondary" data-open-rule="${escapeHtml(alert.rule_id)}">Detail</button></td>
        </tr>
      `
    )
    .join("");

  for (const button of tbody.querySelectorAll("[data-open-rule]")) {
    button.addEventListener("click", () => openDetail(button.dataset.openRule));
  }
}

async function renderDetailPage() {
  const alert = state.alerts.find((item) => item.rule_id === state.selectedRuleId);
  const empty = document.getElementById("detail-empty");
  const content = document.getElementById("detail-content");
  document.getElementById("detail-title").textContent = state.selectedRuleId || "Alert";
  if (!alert) {
    empty.classList.remove("hidden");
    content.classList.add("hidden");
    return;
  }

  empty.classList.add("hidden");
  content.classList.remove("hidden");

  document.getElementById("detail-summary").innerHTML = [
    row("Rule", alert.rule_id),
    row("State", `<span class="state-pill state-${escapeHtml(alert.state)}">${escapeHtml(alert.state)}</span>`, true),
    row("Severity", severityLabel(alert.severity)),
    row("Acked By", alert.acked_by || "-"),
    row("Owner", alert.owner || "-"),
    row("Silences", (alert.active_silence_ids || []).join(", ") || "-"),
    row("Outputs", (alert.matched_outputs || []).join(", ") || "-"),
    row("Description", alert.description || "-"),
    row("Runbook", alert.runbook || "-"),
    row("File", alert.definition_file || "-"),
    row("Source", `<button class="button button-secondary" id="detail-source-button">View Rule Source</button>`, true),
    row("Message", alert.message || "-"),
  ].join("");
  document.getElementById("detail-source-button").addEventListener("click", () => openSourceModal("rule", alert.rule_id));

  document.getElementById("payload-content").textContent = JSON.stringify(alert.payload || {}, null, 2);

  try {
    const history = await getJson(`/history/${encodeURIComponent(alert.rule_id)}`);
    renderHistory(history);
  } catch (error) {
    document.getElementById("history-content").innerHTML = `<div class="history-item">History failed: ${escapeHtml(error.message)}</div>`;
  }
}

function renderSourcesPage() {
  const tbody = document.getElementById("sources-body");
  const plugins = state.plugins
    .filter((plugin) => plugin.type === "source")
    .filter((plugin) => matchesPluginFilter(plugin, state.sourceFilter));
  tbody.innerHTML = plugins
    .map(
      (plugin) => `
        <tr class="${escapeHtml(pluginTableRowClass(plugin))}">
          <td>${escapeHtml(plugin.plugin_id)}</td>
          <td><span class="state-pill state-${escapeHtml(pluginStateToAlertState(plugin.state))}">${escapeHtml(plugin.state)}</span></td>
          <td title="${escapeHtml(plugin.last_updated_at || "-")}">${escapeHtml(formatDateTime(plugin.last_updated_at))}</td>
          <td>${escapeHtml(plugin.definition_file || "-")}</td>
          <td>${formatPluginError(plugin)}</td>
          <td class="action-cell"><button class="button button-secondary" data-open-source="${escapeHtml(plugin.plugin_id)}" data-source-type="source">Source</button></td>
        </tr>
      `
    )
    .join("");

  for (const button of tbody.querySelectorAll("[data-open-source]")) {
    button.addEventListener("click", () => openSourceModal(button.dataset.sourceType, button.dataset.openSource));
  }
}

function renderRulesPage() {
  const tbody = document.getElementById("rules-body");
  const plugins = state.plugins
    .filter((plugin) => plugin.type === "rule")
    .filter((plugin) => matchesPluginFilter(plugin, state.ruleFilter));
  tbody.innerHTML = plugins
    .map(
      (plugin) => `
        <tr class="${escapeHtml(pluginTableRowClass(plugin))}">
          <td>${escapeHtml(plugin.plugin_id)}</td>
          <td><span class="state-pill state-${escapeHtml(pluginStateToAlertState(plugin.state))}">${escapeHtml(plugin.state)}</span></td>
          <td title="${escapeHtml(plugin.last_updated_at || "-")}">${escapeHtml(formatDateTime(plugin.last_updated_at))}</td>
          <td>${escapeHtml(plugin.definition_file || "-")}</td>
          <td>${formatPluginError(plugin)}</td>
          <td class="action-cell"><button class="button button-secondary" data-open-source="${escapeHtml(plugin.plugin_id)}" data-source-type="rule">Source</button></td>
        </tr>
      `
    )
    .join("");

  for (const button of tbody.querySelectorAll("[data-open-source]")) {
    button.addEventListener("click", () => openSourceModal(button.dataset.sourceType, button.dataset.openSource));
  }
}

function renderOutputsPage() {
  const tbody = document.getElementById("outputs-body");
  const summary = document.getElementById("outputs-summary");
  const outputs = state.plugins
    .filter((plugin) => plugin.type === "output")
    .filter(matchesOutputFilter)
    .sort(comparePluginHealth);

  const failedOutputs = outputs.filter((plugin) => plugin.state === "failed");
  summary.innerHTML = failedOutputs.length > 0
    ? `
      <div class="status-banner status-banner-failed">
        <div class="status-banner-title">${escapeHtml(String(failedOutputs.length))} output plugin${failedOutputs.length === 1 ? "" : "s"} failed</div>
        <div class="status-banner-body">Recent output failures are shown first. The Last Error column includes the current exception message from the runtime.</div>
      </div>
    `
    : `
      <div class="status-banner status-banner-ok">
        <div class="status-banner-title">All output plugins are ready</div>
        <div class="status-banner-body">No current delivery failure is reported by the runtime.</div>
      </div>
    `;

  tbody.innerHTML = outputs
    .map(
      (plugin) => `
        <tr class="${escapeHtml(pluginTableRowClass(plugin))}">
          <td>${escapeHtml(plugin.plugin_id)}</td>
          <td><span class="state-pill state-${escapeHtml(pluginStateToAlertState(plugin.state))}">${escapeHtml(plugin.state)}</span></td>
          <td>${escapeHtml(String(plugin.run_count))}</td>
          <td title="${escapeHtml(plugin.last_updated_at || "-")}">${escapeHtml(formatDateTime(plugin.last_updated_at))}</td>
          <td title="${escapeHtml(plugin.last_failure_at || "-")}">${escapeHtml(formatDateTime(plugin.last_failure_at))}</td>
          <td>${escapeHtml(plugin.definition_file || "-")}</td>
          <td>${formatPluginError(plugin)}</td>
          <td class="action-cell"><button class="button button-secondary" data-open-source="${escapeHtml(plugin.plugin_id)}" data-source-type="output">Source</button></td>
        </tr>
      `
    )
    .join("");

  for (const button of tbody.querySelectorAll("[data-open-source]")) {
    button.addEventListener("click", () => openSourceModal(button.dataset.sourceType, button.dataset.openSource));
  }
}

function renderSilencesPage() {
  const tbody = document.getElementById("silences-body");
  tbody.innerHTML = state.silences
    .sort((left, right) => String(left.start_at).localeCompare(String(right.start_at)))
    .map((silence) => {
      const targets = [...(silence.rule_patterns || []), ...(silence.tags || []).map((tag) => `#${tag}`)].join(", ") || "-";
      const status = silence.cancelled_at ? "CANCELLED" : silence.active ? "ACTIVE" : "SCHEDULED";
      return `
        <tr>
          <td>${escapeHtml(shortId(silence.silence_id))}</td>
          <td><span class="state-pill state-${escapeHtml(statusToColor(status))}">${escapeHtml(status)}</span></td>
          <td title="${escapeHtml((silence.start_at || "-") + " -> " + (silence.end_at || "-"))}">${escapeHtml(formatWindow(silence.start_at, silence.end_at))}</td>
          <td>${escapeHtml(targets)}</td>
          <td>${escapeHtml(silence.created_by || "-")}</td>
          <td>${escapeHtml(silence.reason || "-")}</td>
          <td class="action-cell">${silence.cancelled_at ? "" : `<button class="button button-danger" data-cancel-silence="${escapeHtml(silence.silence_id)}">Cancel</button>`}</td>
        </tr>
      `;
    })
    .join("");

  for (const button of tbody.querySelectorAll("[data-cancel-silence]")) {
    button.addEventListener("click", async () => {
      const operator = window.prompt("Operator for cancelling this silence", "Keita");
      if (!operator) {
        return;
      }
      const reason = window.prompt("Reason for cancelling this silence", "") || "";
      await postJson(`/silences/${button.dataset.cancelSilence}/cancel`, { operator, reason });
      refreshAll();
    });
  }
}

function renderHistory(history) {
  const container = document.getElementById("history-content");
  if (history.enabled === false) {
    container.innerHTML = `
      <div class="history-item">
        <div class="history-meta">History is disabled</div>
        <div>Start KANARY with <code>--state-db /path/to/kanary.db</code> or set <code>KANARY_SQLITE_PATH</code> to persist alert and operator history.</div>
      </div>
    `;
    return;
  }
  const entries = [
    ...(history.operator_actions || []).map((action) => ({
      kind: "action",
      at: action.created_at || "",
      html: `
        <div class="history-item history-item-${escapeHtml(historyActionClass(action.action_type))}">
          <div class="history-meta" title="${escapeHtml(action.created_at || "-")}">${escapeHtml(formatDateTime(action.created_at))} action</div>
          <div class="history-title">${escapeHtml(historyActionLabel(action.action_type))}</div>
          <div>${escapeHtml(action.action_type)} by ${escapeHtml(action.operator)}</div>
          <div>${escapeHtml(action.reason || "")}</div>
        </div>
      `,
    })),
    ...(history.alert_events || []).map((event) => ({
      kind: "event",
      at: event.occurred_at || "",
      html: `
        <div class="history-item history-item-${escapeHtml(historyStateClass(event.current_state))}">
          <div class="history-meta" title="${escapeHtml(event.occurred_at || "-")}">${escapeHtml(formatDateTime(event.occurred_at))} event</div>
          <div class="history-title">${escapeHtml((event.previous_state || "-") + " -> " + event.current_state)}</div>
          <div>${escapeHtml(event.message || "")}</div>
        </div>
      `,
    })),
  ]
    .sort((left, right) => parseIsoTime(right.at) - parseIsoTime(left.at));
  container.innerHTML = entries.map((entry) => entry.html).join("") || `<div class="history-item">No history</div>`;
}

async function submitAck() {
  const alert = getSelectedAlert();
  if (!alert) {
    return;
  }
  const operator = document.getElementById("ack-operator").value.trim();
  const reason = document.getElementById("ack-reason").value.trim();
  if (!operator) {
    window.alert("Operator is required.");
    return;
  }
  await postJson(`/alerts/${encodeURIComponent(alert.rule_id)}/ack`, { operator, reason });
  refreshAll();
}

async function submitUnack() {
  const alert = getSelectedAlert();
  if (!alert) {
    return;
  }
  const operator = document.getElementById("ack-operator").value.trim();
  const reason = document.getElementById("ack-reason").value.trim();
  if (!operator) {
    window.alert("Operator is required.");
    return;
  }
  await postJson(`/alerts/${encodeURIComponent(alert.rule_id)}/unack`, { operator, reason });
  refreshAll();
}

async function submitSilenceFor() {
  const alert = getSelectedAlert();
  if (!alert) {
    return;
  }
  const operator = document.getElementById("silence-for-operator").value.trim();
  const minutes = Number(document.getElementById("silence-for-minutes").value);
  const reason = document.getElementById("silence-for-reason").value.trim();
  if (!operator || !minutes) {
    window.alert("Operator and minutes are required.");
    return;
  }
  await postJson("/silences/duration", {
    operator,
    duration_minutes: minutes,
    reason,
    rule_patterns: [alert.rule_id],
  });
  refreshAll();
}

async function submitSilenceWindow() {
  const alert = getSelectedAlert();
  if (!alert) {
    return;
  }
  const operator = document.getElementById("silence-window-operator").value.trim();
  const startAt = document.getElementById("silence-window-start").value;
  const endAt = document.getElementById("silence-window-end").value;
  const reason = document.getElementById("silence-window-reason").value.trim();
  if (!operator || !startAt || !endAt) {
    window.alert("Operator, start, and end are required.");
    return;
  }
  await postJson("/silences/window", {
    operator,
    start_at: localDateTimeToIso(startAt),
    end_at: localDateTimeToIso(endAt),
    reason,
    rule_patterns: [alert.rule_id],
  });
  refreshAll();
}

async function submitAdminDurationSilence() {
  const operator = document.getElementById("admin-duration-operator").value.trim();
  const rulePatterns = parseCsv(document.getElementById("admin-duration-rules").value);
  const tags = parseCsv(document.getElementById("admin-duration-tags").value);
  const minutes = Number(document.getElementById("admin-duration-minutes").value);
  const startAt = document.getElementById("admin-duration-start").value;
  const reason = document.getElementById("admin-duration-reason").value.trim();
  if (!operator || !minutes || (rulePatterns.length === 0 && tags.length === 0)) {
    window.alert("Operator, minutes, and at least one rule pattern or tag are required.");
    return;
  }
  await postJson("/silences/duration", {
    operator,
    duration_minutes: minutes,
    start_at: startAt ? localDateTimeToIso(startAt) : undefined,
    reason,
    rule_patterns: rulePatterns,
    tags,
  });
  refreshAll();
  window.location.hash = "#silences";
}

async function submitAdminWindowSilence() {
  const operator = document.getElementById("admin-window-operator").value.trim();
  const rulePatterns = parseCsv(document.getElementById("admin-window-rules").value);
  const tags = parseCsv(document.getElementById("admin-window-tags").value);
  const startAt = document.getElementById("admin-window-start").value;
  const endAt = document.getElementById("admin-window-end").value;
  const reason = document.getElementById("admin-window-reason").value.trim();
  if (!operator || !startAt || !endAt || (rulePatterns.length === 0 && tags.length === 0)) {
    window.alert("Operator, start, end, and at least one rule pattern or tag are required.");
    return;
  }
  await postJson("/silences/window", {
    operator,
    start_at: localDateTimeToIso(startAt),
    end_at: localDateTimeToIso(endAt),
    reason,
    rule_patterns: rulePatterns,
    tags,
  });
  refreshAll();
  window.location.hash = "#silences";
}

async function reloadEngine() {
  await postJson("/reload", {});
  refreshAll();
}

async function openSourceModal(pluginType, pluginId) {
  try {
    const payload = await getJson(`/plugins/${encodeURIComponent(pluginType)}/${encodeURIComponent(pluginId)}/source`);
    renderSourceModal(payload);
    const modal = document.getElementById("source-modal");
    modal.classList.remove("hidden");
    modal.setAttribute("aria-hidden", "false");
  } catch (error) {
    window.alert(`Source view failed: ${error.message}`);
  }
}

function closeSourceModal() {
  const modal = document.getElementById("source-modal");
  modal.classList.add("hidden");
  modal.setAttribute("aria-hidden", "true");
}

function renderSourceModal(payload) {
  document.getElementById("source-modal-title").textContent = `${payload.type} ${payload.plugin_id}`;
  document.getElementById("source-modal-meta").textContent =
    `${payload.symbol_name} · ${payload.definition_file} · lines ${payload.start_line}-${payload.end_line}`;
  const lines = String(payload.source_text || "").split("\n");
  document.getElementById("source-modal-body").innerHTML = lines
    .map((line, index) => {
      const lineNumber = Number(payload.start_line || 1) + index;
      return `
        <div class="source-line">
          <span class="source-line-number">${escapeHtml(String(lineNumber))}</span>
          <code class="source-line-code">${highlightPythonLine(line)}</code>
        </div>
      `;
    })
    .join("");
}

function openDetail(ruleId) {
  state.selectedRuleId = ruleId;
  window.location.hash = `#alert/${encodeURIComponent(ruleId)}`;
}

function matchesAlertFilter(alert) {
  if (state.stateFilter && alert.state !== state.stateFilter) {
    return false;
  }
  if (!state.alertFilter) {
    return true;
  }
  return matchesTextFilter([
    alert.rule_id,
    alert.state,
    alert.message || "",
    (alert.matched_outputs || []).join(" "),
    alert.acked_by || "",
    alert.owner || "",
    (alert.tags || []).join(" "),
  ], state.alertFilter);
}

function matchesPluginFilter(plugin, filterValue) {
  return matchesTextFilter([
    plugin.type,
    plugin.plugin_id,
    plugin.state,
    plugin.definition_file || "",
    plugin.last_error || "",
  ], filterValue);
}

function matchesOutputFilter(plugin) {
  return matchesTextFilter([
    plugin.plugin_id,
    plugin.state,
    plugin.definition_file || "",
    plugin.last_error || "",
  ], state.outputFilter);
}

function historyActionLabel(actionType) {
  return {
    ack: "Acknowledged",
    unack: "Acknowledgement Removed",
    create_silence: "Silence Created",
    cancel_silence: "Silence Cancelled",
  }[actionType] || "Operator Action";
}

function historyActionClass(actionType) {
  return {
    ack: "acked",
    unack: "firing",
    create_silence: "silenced",
    cancel_silence: "resolved",
  }[actionType] || "neutral";
}

function historyStateClass(stateName) {
  return {
    FIRING: "firing",
    ACKED: "acked",
    SILENCED: "silenced",
    SUPPRESSED: "suppressed",
    OK: "ok",
    RESOLVED: "resolved",
  }[stateName] || "neutral";
}

function parseIsoTime(value) {
  const parsed = Date.parse(value || "");
  return Number.isNaN(parsed) ? 0 : parsed;
}

function compareAlerts(left, right) {
  return alertPriority(left) - alertPriority(right) || left.rule_id.localeCompare(right.rule_id);
}

function comparePluginHealth(left, right) {
  const leftFailed = left.state === "failed" ? 0 : 1;
  const rightFailed = right.state === "failed" ? 0 : 1;
  if (leftFailed !== rightFailed) {
    return leftFailed - rightFailed;
  }
  return parseIsoTime(right.last_updated_at) - parseIsoTime(left.last_updated_at) || left.plugin_id.localeCompare(right.plugin_id);
}

function alertPriority(alert) {
  return {
    FIRING: 0,
    ACKED: 1,
    SILENCED: 2,
    SUPPRESSED: 3,
    OK: 4,
    RESOLVED: 5,
  }[alert.state] ?? 10;
}

function countByState(alerts) {
  return alerts.reduce((counts, alert) => {
    counts[alert.state] = (counts[alert.state] || 0) + 1;
    return counts;
  }, {});
}

function countBySeverity(alerts) {
  return alerts.reduce((counts, alert) => {
    const label = severityLabel(alert.severity);
    counts[label] = (counts[label] || 0) + 1;
    return counts;
  }, {});
}

function getSelectedAlert() {
  return state.alerts.find((alert) => alert.rule_id === state.selectedRuleId);
}

function row(label, value, raw = false) {
  return `
    <div class="detail-row">
      <div class="detail-label">${escapeHtml(label)}</div>
      <div>${raw ? value : escapeHtml(value)}</div>
    </div>
  `;
}

function formatWindow(startAt, endAt) {
  return `${formatDateTime(startAt)} -> ${formatDateTime(endAt)}`;
}

function shortId(value) {
  return value ? value.slice(0, 8) : "-";
}

function formatDateTime(value) {
  if (!value) {
    return "-";
  }
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) {
    return String(value);
  }
  const options = {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  };
  if (state.timeZone.startsWith("offset:")) {
    return formatFixedOffsetDateTime(parsed, state.timeZone.slice("offset:".length));
  }
  if (state.timeZone !== "browser") {
    options.timeZone = state.timeZone;
    options.timeZoneName = "short";
  }
  return parsed.toLocaleString(undefined, options);
}

function addTimeZoneOption(select, value, label) {
  const option = document.createElement("option");
  option.value = value;
  option.textContent = label;
  select.appendChild(option);
}

function formatOffsetLabel(totalMinutes) {
  const sign = totalMinutes >= 0 ? "+" : "-";
  const absoluteMinutes = Math.abs(totalMinutes);
  const hours = String(Math.floor(absoluteMinutes / 60)).padStart(2, "0");
  const minutes = String(absoluteMinutes % 60).padStart(2, "0");
  return `${sign}${hours}:${minutes}`;
}

function timeZoneCityLabel(offsetLabel) {
  const city = {
    "-12:00": "Baker Island",
    "-11:00": "Pago Pago",
    "-10:00": "Honolulu",
    "-09:00": "Anchorage",
    "-08:00": "Los Angeles",
    "-07:00": "Denver",
    "-06:00": "Chicago",
    "-05:00": "New York",
    "-04:00": "Halifax",
    "-03:00": "Buenos Aires",
    "-02:00": "South Georgia",
    "-01:00": "Azores",
    "+01:00": "Zurich",
    "+02:00": "Athens",
    "+03:00": "Riyadh",
    "+04:00": "Dubai",
    "+05:00": "Karachi",
    "+06:00": "Dhaka",
    "+07:00": "Bangkok",
    "+08:00": "Singapore",
    "+09:00": "Tokyo",
    "+10:00": "Sydney",
    "+11:00": "Noumea",
    "+12:00": "Auckland",
    "+13:00": "McMurdo",
    "+14:00": "Kiritimati",
  }[offsetLabel];
  return city ? ` (${city})` : "";
}

function formatFixedOffsetDateTime(date, offsetLabel) {
  const match = /^([+-])(\d{2}):(\d{2})$/.exec(offsetLabel);
  if (!match) {
    return date.toISOString();
  }
  const sign = match[1] === "+" ? 1 : -1;
  const offsetMinutes = sign * (Number(match[2]) * 60 + Number(match[3]));
  const shifted = new Date(date.getTime() + offsetMinutes * 60 * 1000);
  const year = shifted.getUTCFullYear();
  const month = String(shifted.getUTCMonth() + 1).padStart(2, "0");
  const day = String(shifted.getUTCDate()).padStart(2, "0");
  const hours = String(shifted.getUTCHours()).padStart(2, "0");
  const minutes = String(shifted.getUTCMinutes()).padStart(2, "0");
  const seconds = String(shifted.getUTCSeconds()).padStart(2, "0");
  return `${year}-${month}-${day} ${hours}:${minutes}:${seconds} UTC${offsetLabel}`;
}

function parseCsv(value) {
  return value
    .split(",")
    .map((entry) => entry.trim())
    .filter(Boolean);
}

function matchesTextFilter(values, filterValue) {
  const normalizedFilter = String(filterValue || "").trim().toLowerCase();
  if (!normalizedFilter) {
    return true;
  }
  const candidates = values.map((value) => String(value || "").toLowerCase());
  if (hasGlob(normalizedFilter)) {
    return candidates.some((candidate) => globToRegExp(normalizedFilter).test(candidate));
  }
  return candidates.some((candidate) => candidate.includes(normalizedFilter));
}

function hasGlob(value) {
  return value.includes("*") || value.includes("?") || value.includes("[");
}

function globToRegExp(pattern) {
  let regex = "^";
  for (let index = 0; index < pattern.length; index += 1) {
    const char = pattern[index];
    if (char === "*") {
      regex += ".*";
      continue;
    }
    if (char === "?") {
      regex += ".";
      continue;
    }
    if (char === "[") {
      const endIndex = pattern.indexOf("]", index + 1);
      if (endIndex > index + 1) {
        regex += pattern.slice(index, endIndex + 1);
        index = endIndex;
        continue;
      }
    }
    regex += escapeRegExp(char);
  }
  regex += "$";
  return new RegExp(regex);
}

function escapeRegExp(value) {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function localDateTimeToIso(value) {
  return new Date(value).toISOString();
}

function severityLabel(value) {
  return { 10: "INFO", 20: "WARN", 30: "ERROR", 40: "CRITICAL" }[value] || String(value);
}

function pluginStateToAlertState(stateName) {
  if (stateName === "failed") return "FIRING";
  if (stateName === "ready") return "OK";
  return "SUPPRESSED";
}

function pluginTableRowClass(plugin) {
  return plugin.state === "failed" ? "table-row-failed" : "";
}

function formatPluginError(plugin) {
  const errorText = plugin.last_error || "-";
  if (plugin.state !== "failed") {
    return escapeHtml(errorText);
  }
  const detail = plugin.last_error_detail
    ? `
      <details class="plugin-error-detail">
        <summary>Traceback</summary>
        <pre class="plugin-error-trace">${escapeHtml(plugin.last_error_detail)}</pre>
      </details>
    `
    : "";
  return `
    <div class="plugin-error-block">
      <div class="plugin-error-label">Plugin failed</div>
      <div class="plugin-error-text">${escapeHtml(errorText)}</div>
      ${detail}
    </div>
  `;
}

function statusToColor(status) {
  if (status === "ACTIVE") return "SILENCED";
  if (status === "CANCELLED") return "SUPPRESSED";
  return "ACKED";
}

async function getJson(path) {
  const response = await fetch(path, { headers: { Accept: "application/json" } });
  if (!response.ok) {
    throw new Error(await response.text());
  }
  return response.json();
}

async function postJson(path, body) {
  const response = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "application/json" },
    body: JSON.stringify(body),
  });
  if (!response.ok) {
    throw new Error(await response.text());
  }
  return response.json();
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function escapeAttribute(value) {
  return escapeHtml(value);
}

function shortCommit(value) {
  return value.length > 12 ? value.slice(0, 12) : value;
}

function highlightPythonLine(line) {
  const tokenPattern = /(@[\w.]+|"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'|#[^\n]*|\b(?:False|None|True|and|as|assert|async|await|break|case|class|continue|def|elif|else|except|finally|for|from|if|import|in|is|lambda|match|not|or|pass|raise|return|try|while|with|yield)\b|\b\d+(?:\.\d+)?\b)/g;
  let cursor = 0;
  let html = "";
  for (const match of line.matchAll(tokenPattern)) {
    const token = match[0];
    const start = match.index || 0;
    html += escapeHtml(line.slice(cursor, start));
    html += wrapPythonToken(token);
    cursor = start + token.length;
  }
  html += escapeHtml(line.slice(cursor));
  return html;
}

function wrapPythonToken(token) {
  const escaped = escapeHtml(token);
  if (token.startsWith("#")) {
    return `<span class="tok-comment">${escaped}</span>`;
  }
  if (token.startsWith("@")) {
    return `<span class="tok-decorator">${escaped}</span>`;
  }
  if (token.startsWith("'") || token.startsWith('"')) {
    return `<span class="tok-string">${escaped}</span>`;
  }
  if (/^\d/.test(token)) {
    return `<span class="tok-number">${escaped}</span>`;
  }
  return `<span class="tok-keyword">${escaped}</span>`;
}

window.addEventListener("DOMContentLoaded", init);
