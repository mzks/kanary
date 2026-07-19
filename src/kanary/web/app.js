const DEFAULT_REFRESH_MS = 5000;
const DASHBOARD_STATES = new Set(["FIRING", "ACKED", "SILENCED"]);

const state = {
  alerts: [],
  plugins: [],
  silences: [],
  meta: null,
  pendingActions: new Set(),
  lastActionSnapshots: new Map(),
  expandedPluginErrors: new Set(),
  route: "dashboard",
  selectedRuleId: null,
  alertFilter: "",
  stateFilter: "",
  sourceFilter: "",
  ruleFilter: "",
  outputFilter: "",
  silenceFilter: "",
  hidePastSilences: true,
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
  document.getElementById("silence-filter").addEventListener("input", (event) => {
    state.silenceFilter = event.target.value.toLowerCase();
    renderSilencesPage();
  });
  document.getElementById("silence-hide-past").addEventListener("change", (event) => {
    state.hidePastSilences = Boolean(event.target.checked);
    renderSilencesPage();
  });
  document.getElementById("ack-button").addEventListener("click", submitAck);
  document.getElementById("unack-button").addEventListener("click", submitUnack);
  document.getElementById("ack-operator").addEventListener("input", syncActionButtonState);
  document.getElementById("ack-reason").addEventListener("input", syncActionButtonState);
  document.getElementById("silence-for-button").addEventListener("click", submitSilenceFor);
  document.getElementById("silence-window-button").addEventListener("click", submitSilenceWindow);
  document.getElementById("admin-duration-button").addEventListener("click", submitAdminDurationSilence);
  document.getElementById("admin-window-button").addEventListener("click", submitAdminWindowSilence);
  document.getElementById("admin-reload-dirty-button").addEventListener("click", reloadDirtyPlugins);
  document.getElementById("admin-reload-all-button").addEventListener("click", reloadAllPlugins);
  document.getElementById("admin-restart-engine-button").addEventListener("click", restartEngine);
  document.getElementById("source-modal-close").addEventListener("click", closeSourceModal);
  document.getElementById("plugin-info-modal-close").addEventListener("click", closePluginInfoModal);
  document.getElementById("alert-state-modal-close").addEventListener("click", closeAlertStateModal);
  for (const element of document.querySelectorAll("[data-close-source]")) {
    element.addEventListener("click", closeSourceModal);
  }
  for (const element of document.querySelectorAll("[data-close-plugin-info]")) {
    element.addEventListener("click", closePluginInfoModal);
  }
  for (const element of document.querySelectorAll("[data-close-alert-state]")) {
    element.addEventListener("click", closeAlertStateModal);
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
    renderSidebarPluginStatus();
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

function renderSidebarPluginStatus() {
  renderPluginNavBadge("source", "sources");
  renderPluginNavBadge("rule", "rules");
  renderPluginNavBadge("output", "outputs");
}

function renderPluginNavBadge(pluginType, routeName) {
  const plugins = state.plugins.filter((plugin) => plugin.type === pluginType);
  const total = plugins.length;
  const ready = plugins.filter((plugin) => plugin.state === "READY").length;
  const pending = plugins.filter((plugin) => ["DISCOVERED", "DIRTY", "PENDING_REMOVE"].includes(plugin.state)).length;
  const countElement = document.getElementById(`nav-${routeName}-count`);
  const dirtyElement = document.getElementById(`nav-${routeName}-dirty`);

  if (countElement) {
    countElement.textContent = `${ready}/${total}`;
    countElement.title = `${ready} ready plugin${ready === 1 ? "" : "s"} / ${total} registered`;
  }

  if (dirtyElement) {
    dirtyElement.textContent = String(pending);
    dirtyElement.classList.toggle("hidden", pending <= 0);
    dirtyElement.title = pending > 0
      ? `${pending} plugin${pending === 1 ? "" : "s"} pending apply or unload`
      : "";
  }
}

function setRefreshStatus(message, isError) {
  const element = document.getElementById("refresh-status");
  element.textContent = message;
  element.classList.toggle("status-error", Boolean(isError));
}

function renderBuildMeta() {
  const element = document.getElementById("viewer-build-meta");
  const outputEmitBanner = document.getElementById("output-emit-disabled-banner");
  const meta = state.meta;
  outputEmitBanner.classList.toggle("hidden", !meta || meta.output_emit_enabled !== false);
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
    element.classList.toggle("hidden", !meta.plugin_load_error);
  } else {
    element.classList.remove("hidden");
  }

  const loadErrorBlock = meta.plugin_load_error
    ? `
      <div class="viewer-build-warning">
        <strong>Plugin Load Error</strong>
        <div>${escapeHtml(String(meta.plugin_load_error))}</div>
      </div>
    `
    : "";
  element.innerHTML = `
    ${parts.length > 0 ? `
      <strong>Project Metadata</strong>
      <div class="viewer-build-meta-links">${parts.join('<span aria-hidden="true">·</span>')}</div>
    ` : ""}
    ${loadErrorBlock}
  `;
}

function renderDashboardPage() {
  const activeAlerts = state.alerts.filter((alert) => DASHBOARD_STATES.has(alert.state));
  const counts = countByState(activeAlerts);
  const severityCounts = countBySeverity(activeAlerts);
  const failedPlugins = state.plugins.filter((plugin) => plugin.state === "FAILED").length;
  const cards = [
    {
      label: "FIRING",
      value: counts.FIRING || 0,
      className: "firing",
      ...activeAlertAgeNote(activeAlerts, "FIRING", "Requires attention now"),
    },
    {
      label: "ACKED",
      value: counts.ACKED || 0,
      className: "acked",
      ...activeAlertAgeNote(activeAlerts, "ACKED", "Someone already responded"),
    },
    {
      label: "SILENCED",
      value: counts.SILENCED || 0,
      className: "silenced",
      ...activeAlertAgeNote(activeAlerts, "SILENCED", "Muted by operator action"),
    },
    { label: "FAILED PLUGINS", value: failedPlugins, note: "Runtime components in failed state", className: "failed" },
  ];
  document.getElementById("dashboard-cards").innerHTML = cards
    .map(
      (card) => `
        <article class="hero-card ${card.className}">
          <div class="hero-label">${escapeHtml(card.label)}</div>
          <strong>${escapeHtml(String(card.value))}</strong>
          <div class="hero-note"${card.noteTitle ? ` title="${escapeHtml(card.noteTitle)}"` : ""}>${escapeHtml(card.note)}</div>
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
    container.innerHTML = `<div class="muted">No firing, acknowledged, or silenced alerts. No news is good news. Relax! ;)</div>`;
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
                <span title="${escapeHtml(alert.active_since || "-")}">Active since ${escapeHtml(formatRelativeTime(alert.active_since))}</span>
              </div>
            </div>
            <button class="button button-secondary" data-open-rule="${escapeHtml(alert.rule_id)}">Open</button>
          </div>
          <div class="alert-card-message">${renderLinkedText(alert.message || "-")}</div>
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
          <td class="plugin-primary-cell">
            <div class="plugin-title">${escapeHtml(alert.rule_id)}</div>
          </td>
          <td>${formatAlertStateCell(alert)}</td>
          <td><span class="severity-badge severity-${severityLabel(alert.severity)}">${escapeHtml(severityLabel(alert.severity))}</span></td>
          <td>${formatTagList(alert.tags, { empty: "-" })}</td>
          <td>${formatChipList(alert.matched_outputs, { empty: "-", chipClass: "meta-chip meta-chip-output", family: "output" })}</td>
          <td>${renderLinkedText(alert.message || "-")}</td>
          <td class="action-cell"><button class="button button-secondary" data-open-rule="${escapeHtml(alert.rule_id)}">Detail</button></td>
        </tr>
      `
    )
    .join("");

  for (const button of tbody.querySelectorAll("[data-open-rule]")) {
    button.addEventListener("click", () => openDetail(button.dataset.openRule));
  }
  for (const button of tbody.querySelectorAll("[data-open-alert-state]")) {
    button.addEventListener("click", () => openAlertStateModal(button.dataset.openAlertState));
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
    row("Active Since", alert.active_since ? `${formatRelativeTime(alert.active_since)} (${formatDateTime(alert.active_since)})` : "-"),
    row("Acked By", alert.acked_by || "-"),
    row("Owner", alert.owner || "-"),
    row("Tags", formatTagList(alert.tags, { empty: "-" }), true),
    row("Silences", (alert.active_silence_ids || []).join(", ") || "-"),
    row("Outputs", (alert.matched_outputs || []).join(", ") || "-"),
    row("Description", renderLinkedText(alert.description || "-"), true),
    row("Runbook", renderLinkedText(alert.runbook || "-"), true),
    row("File", formatDefinitionFile(alert.definition_file), true),
    row("Source", `<button class="button button-secondary" id="detail-source-button">View Rule Source</button>`, true),
    row("Message", renderLinkedText(alert.message || "-"), true),
  ].join("");
  document.getElementById("detail-source-button").addEventListener("click", () => openSourceModal("rule", alert.rule_id));
  updateDetailActionAvailability(alert);

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
          <td class="plugin-primary-cell">
            <div class="plugin-title">${escapeHtml(plugin.plugin_id)}</div>
          </td>
          <td>${formatPluginRuntime(plugin)}</td>
          <td>${formatPlainDescription(plugin.description)}</td>
          <td>${formatSourceInfo(plugin)}</td>
          <td class="action-cell"><button class="button button-compact" data-apply-plugin="${escapeHtml(plugin.plugin_id)}" data-plugin-type="source">Apply</button></td>
          <td class="action-cell"><button class="button button-secondary button-compact" data-open-source="${escapeHtml(plugin.plugin_id)}" data-source-type="source">Source</button></td>
        </tr>
      `
    )
    .join("");

  for (const button of tbody.querySelectorAll("[data-apply-plugin]")) {
    button.addEventListener("click", () => applyPlugin(button.dataset.pluginType, button.dataset.applyPlugin));
  }
  for (const button of tbody.querySelectorAll("[data-open-source]")) {
    button.addEventListener("click", () => openSourceModal(button.dataset.sourceType, button.dataset.openSource));
  }
  for (const button of tbody.querySelectorAll("[data-open-plugin-info]")) {
    button.addEventListener("click", () => openPluginInfoModal(button.dataset.pluginType, button.dataset.pluginId));
  }
  bindPluginErrorDetailToggles(tbody);
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
          <td class="plugin-primary-cell">
            <div class="plugin-title">${escapeHtml(plugin.plugin_id)}</div>
          </td>
          <td>${formatPluginRuntime(plugin)}</td>
          <td>${formatPlainDescription(plugin.description)}</td>
          <td>${formatInputList(plugin.inputs)}</td>
          <td>${formatTagList(plugin.tags, { empty: "-" })}</td>
          <td>${formatChipList(plugin.matched_outputs, { empty: "-", chipClass: "meta-chip meta-chip-output", family: "output" })}</td>
          <td>${formatRuleInfo(plugin)}</td>
          <td class="action-cell"><button class="button button-compact" data-apply-plugin="${escapeHtml(plugin.plugin_id)}" data-plugin-type="rule">Apply</button></td>
          <td class="action-cell"><button class="button button-secondary button-compact" data-open-source="${escapeHtml(plugin.plugin_id)}" data-source-type="rule">Source</button></td>
        </tr>
      `
    )
    .join("");

  for (const button of tbody.querySelectorAll("[data-apply-plugin]")) {
    button.addEventListener("click", () => applyPlugin(button.dataset.pluginType, button.dataset.applyPlugin));
  }
  for (const button of tbody.querySelectorAll("[data-open-source]")) {
    button.addEventListener("click", () => openSourceModal(button.dataset.sourceType, button.dataset.openSource));
  }
  for (const button of tbody.querySelectorAll("[data-open-plugin-info]")) {
    button.addEventListener("click", () => openPluginInfoModal(button.dataset.pluginType, button.dataset.pluginId));
  }
  bindPluginErrorDetailToggles(tbody);
}

function renderOutputsPage() {
  const tbody = document.getElementById("outputs-body");
  const summary = document.getElementById("outputs-summary");
  const outputs = state.plugins
    .filter((plugin) => plugin.type === "output")
    .filter(matchesOutputFilter)
    .sort(comparePluginHealth);
  summary.innerHTML = "";

  tbody.innerHTML = outputs
    .map(
      (plugin) => `
        <tr class="${escapeHtml(pluginTableRowClass(plugin))}">
          <td class="plugin-primary-cell">
            <div class="plugin-title">${escapeHtml(plugin.plugin_id)}</div>
          </td>
          <td>${formatPluginRuntime(plugin)}</td>
          <td>${formatMinimumSeverity(plugin.minimum_severity)}</td>
          <td>${formatChipList(plugin.include_tags, { empty: "*", tagColors: true })}</td>
          <td>${formatChipList(plugin.exclude_tags, { empty: "-", tagColors: true })}</td>
          <td>${formatPlainDescription(plugin.description)}</td>
          <td>${formatOutputInfo(plugin)}</td>
          <td class="action-cell"><button class="button button-compact" data-apply-plugin="${escapeHtml(plugin.plugin_id)}" data-plugin-type="output">Apply</button></td>
          <td class="action-cell"><button class="button button-secondary button-compact" data-open-source="${escapeHtml(plugin.plugin_id)}" data-source-type="output">Source</button></td>
        </tr>
      `
    )
    .join("");

  for (const button of tbody.querySelectorAll("[data-apply-plugin]")) {
    button.addEventListener("click", () => applyPlugin(button.dataset.pluginType, button.dataset.applyPlugin));
  }
  for (const button of tbody.querySelectorAll("[data-open-source]")) {
    button.addEventListener("click", () => openSourceModal(button.dataset.sourceType, button.dataset.openSource));
  }
  for (const button of tbody.querySelectorAll("[data-open-plugin-info]")) {
    button.addEventListener("click", () => openPluginInfoModal(button.dataset.pluginType, button.dataset.pluginId));
  }
  bindPluginErrorDetailToggles(tbody);
}

function renderSilencesPage() {
  const tbody = document.getElementById("silences-body");
  tbody.innerHTML = state.silences
    .filter(matchesSilenceFilter)
    .sort(compareSilenceRows)
    .map((silence) => {
      const targets = [...(silence.rule_patterns || []), ...(silence.tags || []).map((tag) => `#${tag}`)].join(", ") || "-";
      const status = silenceDisplayStatus(silence);
      return `
        <tr>
          <td>${escapeHtml(shortId(silence.silence_id))}</td>
          <td><span class="state-pill state-${escapeHtml(statusToColor(status))}">${escapeHtml(status)}</span></td>
          <td title="${escapeHtml((silence.start_at || "-") + " -> " + (silence.end_at || "-"))}">${escapeHtml(formatWindow(silence.start_at, silence.end_at))}</td>
          <td>${escapeHtml(targets)}</td>
          <td>${escapeHtml(silence.created_by || "-")}</td>
          <td>${escapeHtml(silence.reason || "-")}</td>
          <td class="action-cell">${silence.cancelled_at || status === "EXPIRED" ? "" : `<button class="button button-danger" data-cancel-silence="${escapeHtml(silence.silence_id)}">Cancel</button>`}</td>
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
    ...(history.output_dispatches || []).map((dispatch) => ({
      kind: "dispatch",
      at: dispatch.occurred_at || "",
      html: `
        <div class="history-item">
          <div class="history-meta" title="${escapeHtml(dispatch.occurred_at || "-")}">${escapeHtml(formatDateTime(dispatch.occurred_at))} output routing</div>
          <div class="history-title">${escapeHtml((dispatch.previous_state || "-") + " -> " + dispatch.current_state)}</div>
          <div>Matched: ${escapeHtml((dispatch.matched_outputs || []).join(", ") || "-")}</div>
          <div>Delivered: ${escapeHtml((dispatch.delivered_outputs || []).join(", ") || "-")}</div>
          <div>Emit skipped: ${escapeHtml((dispatch.emit_skipped_outputs || []).join(", ") || "-")}</div>
        </div>
      `,
    })),
  ]
    .sort((left, right) => parseIsoTime(right.at) - parseIsoTime(left.at));
  container.innerHTML = entries.map((entry) => entry.html).join("") || `<div class="history-item">No history</div>`;
}

function updateDetailActionAvailability(alert) {
  const ackButton = document.getElementById("ack-button");
  const unackButton = document.getElementById("unack-button");
  const ackNotice = document.getElementById("ack-notice");
  const alreadyAcknowledged = alert.state === "ACKED";
  if (ackButton) {
    ackButton.disabled = state.pendingActions.has("acknowledge") || alreadyAcknowledged;
  }
  if (unackButton) {
    unackButton.disabled = alert.state !== "ACKED" || state.pendingActions.has("acknowledge");
  }
  if (ackNotice) {
    if (alreadyAcknowledged) {
      ackNotice.textContent = alert.acked_by
        ? `Acknowledged by ${alert.acked_by}. Use UNACK to reopen it.`
        : "This alert is acknowledged. Use UNACK to reopen it.";
      ackNotice.classList.remove("hidden");
    } else {
      ackNotice.textContent = "";
      ackNotice.classList.add("hidden");
    }
  }
}

async function submitAck() {
  const alert = getSelectedAlert();
  if (!alert) {
    return;
  }
  if (alert.state === "ACKED" || state.pendingActions.has("acknowledge")) {
    return;
  }
  const operator = document.getElementById("ack-operator").value.trim();
  const reason = document.getElementById("ack-reason").value.trim();
  if (!operator) {
    window.alert("Operator is required.");
    return;
  }
  await runPendingAction("acknowledge", ["ack-button", "unack-button"], async () => {
    const latestAlert = await fetchLatestAlert(alert.rule_id);
    if (latestAlert && latestAlert.state === "ACKED") {
      setRefreshStatus("Skipped ACK: the alert is already acknowledged.", false);
      await refreshAll();
      return;
    }
    await postJson(`/alerts/${encodeURIComponent(alert.rule_id)}/ack`, { operator, reason });
    await refreshAll();
  });
}

async function submitUnack() {
  const alert = getSelectedAlert();
  if (!alert) {
    return;
  }
  if (alert.state !== "ACKED" || state.pendingActions.has("acknowledge")) {
    return;
  }
  const operator = document.getElementById("ack-operator").value.trim();
  const reason = document.getElementById("ack-reason").value.trim();
  if (!operator) {
    window.alert("Operator is required.");
    return;
  }
  await runPendingAction("acknowledge", ["ack-button", "unack-button"], async () => {
    const latestAlert = await fetchLatestAlert(alert.rule_id);
    if (latestAlert && latestAlert.state !== "ACKED") {
      setRefreshStatus("Skipped UNACK: the alert is not currently acknowledged.", false);
      await refreshAll();
      return;
    }
    const actionInput = { operator, reason };
    if (shouldSkipRepeatedAlertAction("unack", alert.rule_id, latestAlert, actionInput)) {
      setRefreshStatus("Skipped duplicate UNACK: the alert content and ACK input have not changed.", false);
      await refreshAll();
      return;
    }
    await postJson(`/alerts/${encodeURIComponent(alert.rule_id)}/unack`, { operator, reason });
    await refreshAll();
    rememberAlertActionSnapshot("unack", alert.rule_id, actionInput);
  });
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
  await runPendingAction("detail-silence-duration", ["silence-for-button"], async () => {
    const latestAlert = await fetchLatestAlert(alert.rule_id);
    if (shouldSkipRepeatedAlertAction("silence-duration", alert.rule_id, latestAlert)) {
      setRefreshStatus("Skipped duplicate silence: the alert content has not changed.", false);
      return;
    }
    await postJson("/silences/duration", {
      operator,
      duration_minutes: minutes,
      reason,
      rule_patterns: [alert.rule_id],
    });
    await refreshAll();
    rememberAlertActionSnapshot("silence-duration", alert.rule_id);
  });
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
  await runPendingAction("detail-silence-window", ["silence-window-button"], async () => {
    const latestAlert = await fetchLatestAlert(alert.rule_id);
    if (shouldSkipRepeatedAlertAction("silence-window", alert.rule_id, latestAlert)) {
      setRefreshStatus("Skipped duplicate silence window: the alert content has not changed.", false);
      return;
    }
    await postJson("/silences/window", {
      operator,
      start_at: localDateTimeToIso(startAt),
      end_at: localDateTimeToIso(endAt),
      reason,
      rule_patterns: [alert.rule_id],
    });
    await refreshAll();
    rememberAlertActionSnapshot("silence-window", alert.rule_id);
  });
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
  await runPendingAction("admin-silence-duration", ["admin-duration-button"], async () => {
    await postJson("/silences/duration", {
      operator,
      duration_minutes: minutes,
      start_at: startAt ? localDateTimeToIso(startAt) : undefined,
      reason,
      rule_patterns: rulePatterns,
      tags,
    });
    await refreshAll();
    window.location.hash = "#silences";
  });
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
  await runPendingAction("admin-silence-window", ["admin-window-button"], async () => {
    await postJson("/silences/window", {
      operator,
      start_at: localDateTimeToIso(startAt),
      end_at: localDateTimeToIso(endAt),
      reason,
      rule_patterns: rulePatterns,
      tags,
    });
    await refreshAll();
    window.location.hash = "#silences";
  });
}

async function reloadDirtyPlugins() {
  await runPendingAction("reload-dirty", ["admin-reload-dirty-button"], async () => {
    await postJson("/reload", { dirty: true });
    await refreshAll();
  });
}

async function reloadAllPlugins() {
  await runPendingAction("reload-all", ["admin-reload-all-button"], async () => {
    await postJson("/reload", { all: true });
    await refreshAll();
  });
}

async function restartEngine() {
  const confirmed = window.confirm(
    "Restart the engine now?\n\nAll sources, rules, and outputs will be reinitialized. Without a state DB, in-memory state may be lost."
  );
  if (!confirmed) {
    return;
  }
  await runPendingAction("reload-full", ["admin-restart-engine-button"], async () => {
    await postJson("/reload", { full: true });
    await refreshAll();
  });
}

async function applyPlugin(pluginType, pluginId) {
  await runPendingAction(`apply-${pluginType}-${pluginId}`, [], async () => {
    await postJson("/reload", { [pluginType]: pluginId });
    await refreshAll();
  });
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

function openPluginInfoModal(pluginType, pluginId) {
  const plugin = state.plugins.find((item) => item.type === pluginType && item.plugin_id === pluginId);
  if (!plugin) {
    window.alert("Plugin info is not available.");
    return;
  }
  renderPluginInfoModal(plugin);
  const modal = document.getElementById("plugin-info-modal");
  modal.classList.remove("hidden");
  modal.setAttribute("aria-hidden", "false");
}

function closePluginInfoModal() {
  const modal = document.getElementById("plugin-info-modal");
  modal.classList.add("hidden");
  modal.setAttribute("aria-hidden", "true");
}

function openAlertStateModal(ruleId) {
  const alert = state.alerts.find((item) => item.rule_id === ruleId);
  if (!alert) {
    window.alert("Alert details are not available.");
    return;
  }
  renderAlertStateModal(alert);
  const modal = document.getElementById("alert-state-modal");
  modal.classList.remove("hidden");
  modal.setAttribute("aria-hidden", "false");
}

function closeAlertStateModal() {
  const modal = document.getElementById("alert-state-modal");
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

function renderPluginInfoModal(plugin) {
  document.getElementById("plugin-info-modal-title").textContent = `${plugin.type} ${plugin.plugin_id}`;
  document.getElementById("plugin-info-modal-meta").textContent = `${plugin.state} · ${plugin.definition_file_name || plugin.definition_file || "-"}`;
  document.getElementById("plugin-info-modal-body").innerHTML = pluginInfoRows(plugin)
    .map(([label, value, raw]) => infoRow(label, value, raw))
    .join("");
}

function renderAlertStateModal(alert) {
  document.getElementById("alert-state-modal-title").textContent = alert.rule_id;
  document.getElementById("alert-state-modal-meta").textContent = `${alert.state} · ${severityLabel(alert.severity)}`;
  document.getElementById("alert-state-modal-body").innerHTML = alertStateRows(alert)
    .map(([label, value, raw]) => infoRow(label, value, raw))
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
    alert.description || "",
    alert.runbook || "",
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
    plugin.description || "",
    (plugin.inputs || []).join(" "),
    (plugin.resolved_sources || []).join(" "),
    (plugin.matched_outputs || []).join(" "),
    (plugin.tags || []).join(" "),
    plugin.owner || "",
    plugin.runbook || "",
    plugin.definition_file || "",
    plugin.last_error || "",
  ], filterValue);
}

function matchesOutputFilter(plugin) {
  return matchesTextFilter([
    plugin.plugin_id,
    plugin.state,
    plugin.description || "",
    (plugin.include_tags || []).join(" "),
    (plugin.exclude_tags || []).join(" "),
    (plugin.exclude_states || []).join(" "),
    (plugin.exclude_transitions || []).join(" "),
    plugin.minimum_severity || "",
    plugin.definition_file || "",
    plugin.last_error || "",
  ], state.outputFilter);
}

function matchesSilenceFilter(silence) {
  const status = silenceDisplayStatus(silence);
  if (state.hidePastSilences && (status === "EXPIRED" || status === "CANCELLED")) {
    return false;
  }
  return matchesTextFilter([
    silence.silence_id,
    status,
    silence.created_by || "",
    silence.reason || "",
    (silence.rule_patterns || []).join(" "),
    (silence.tags || []).join(" "),
  ], state.silenceFilter);
}

function formatPluginRuntime(plugin) {
  return `
    <div class="plugin-runtime-block runtime-badge-group">
      <span class="state-pill plugin-state-${escapeHtml(plugin.state)}">${escapeHtml(plugin.state)}</span>
    </div>
  `;
}

function formatAlertStateCell(alert) {
  const hasDetails = alertHasStateDetails(alert);
  return `
    <div class="state-with-detail">
      <span class="state-pill state-${escapeHtml(alert.state)}">${escapeHtml(alert.state)}</span>
      ${hasDetails ? `<button class="state-detail-button" data-open-alert-state="${escapeAttribute(alert.rule_id)}" aria-label="Show state details for ${escapeAttribute(alert.rule_id)}">i</button>` : ""}
    </div>
  `;
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
  const leftFailed = left.state === "FAILED" ? 0 : 1;
  const rightFailed = right.state === "FAILED" ? 0 : 1;
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

function activeAlertAgeNote(alerts, alertState, fallback) {
  const matching = alerts
    .filter((alert) => alert.state === alertState && parseIsoTime(alert.active_since) > 0)
    .sort((left, right) => parseIsoTime(left.active_since) - parseIsoTime(right.active_since));
  if (matching.length === 0) {
    return { note: fallback, noteTitle: "" };
  }
  const oldest = matching[0];
  if (matching.length === 1) {
    return {
      note: `Active since ${formatRelativeTime(oldest.active_since)}`,
      noteTitle: oldest.active_since,
    };
  }
  const newest = matching[matching.length - 1];
  return {
    note: `Oldest ${formatRelativeTime(oldest.active_since)} · Newest ${formatRelativeTime(newest.active_since)}`,
    noteTitle: `Oldest: ${oldest.active_since}\nNewest: ${newest.active_since}`,
  };
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

function formatRelativeTime(value) {
  const timestamp = parseIsoTime(value);
  if (timestamp <= 0) {
    return "-";
  }
  const elapsedSeconds = Math.max(0, Math.round((Date.now() - timestamp) / 1000));
  if (elapsedSeconds < 45) {
    return "just now";
  }
  const elapsedMinutes = Math.round(elapsedSeconds / 60);
  if (elapsedMinutes < 60) {
    return `${elapsedMinutes}m ago`;
  }
  const hours = Math.floor(elapsedMinutes / 60);
  const minutes = elapsedMinutes % 60;
  if (hours < 24) {
    return minutes === 0 ? `${hours}h ago` : `${hours}h ${minutes}m ago`;
  }
  const days = Math.floor(hours / 24);
  return `${days}d ago`;
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

function pluginTableRowClass(plugin) {
  return plugin.state === "FAILED" ? "table-row-failed" : "";
}

function formatPluginError(plugin) {
  const errorText = plugin.last_error || "-";
  if (plugin.state !== "FAILED") {
    return errorText === "-" ? "" : formatKeyValueLine("Last Error", errorText);
  }
  const isExpanded = state.expandedPluginErrors.has(plugin.plugin_id);
  const detail = plugin.last_error_detail
    ? `
      <details class="plugin-error-detail" data-plugin-error-detail="${escapeAttribute(plugin.plugin_id)}" ${isExpanded ? "open" : ""}>
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

function formatDescriptionLine(value, options = {}) {
  const hasValue = Boolean(String(value || "").trim());
  if (!hasValue && options.empty === false) {
    return "";
  }
  return formatKeyValueLine("Description", hasValue ? renderLinkedText(value) : "-", true);
}

function formatPlainDescription(value) {
  const text = String(value || "").trim();
  return text ? `<div class="plain-text-cell">${renderLinkedText(text)}</div>` : `<span class="muted">-</span>`;
}

function formatTagLine(tags) {
  return formatKeyValueLine("Tags", formatTagList(tags, { empty: "-" }), true);
}

function formatKeyValueLine(label, value, raw = false) {
  if (!value) {
    return "";
  }
  return `
    <div class="plugin-context-row">
      <div class="plugin-context-label">${escapeHtml(label)}</div>
      <div class="plugin-context-value">${raw ? value : escapeHtml(value)}</div>
    </div>
  `;
}

function formatDefinitionFile(definitionFile) {
  if (!definitionFile) {
    return "";
  }
  return formatKeyValueLine(
    "File",
    `<span class="plugin-file-name" title="${escapeAttribute(definitionFile)}">${escapeHtml(baseName(definitionFile))}</span>`,
    true,
  );
}

function formatTagList(tags, options = {}) {
  return formatChipList(tags, { ...options, tagColors: true, chipClass: "tag-chip" });
}

function formatChipList(values, options = {}) {
  const items = Array.isArray(values) ? values.filter(Boolean) : [];
  if (items.length === 0) {
    return options.empty ? `<span class="muted">${escapeHtml(options.empty)}</span>` : "";
  }
  return `
    <div class="tag-list">
      ${items.map((item) => formatChip(item, options)).join("")}
    </div>
  `;
}

function formatChip(value, options = {}) {
  const baseClass = options.chipClass || "meta-chip";
  const style = options.tagColors || options.family ? ` style="${escapeAttribute(tagColorStyle(String(value), options.family || "tag"))}"` : "";
  return `<span class="${baseClass}"${style}>${escapeHtml(String(value))}</span>`;
}

function tagColorStyle(tag, family = "tag") {
  const palettes = {
    tag: [
      { bg: "#dbe9f6", border: "#a9bfd8", text: "#325a78" },
      { bg: "#deeddc", border: "#a7c79f", text: "#41693b" },
      { bg: "#f5edcf", border: "#dcc986", text: "#75611f" },
      { bg: "#e8e0ef", border: "#c6b0d6", text: "#654b77" },
      { bg: "#f1dfd8", border: "#d8ad9f", text: "#804d3d" },
      { bg: "#dcedea", border: "#a6ccc3", text: "#336459" },
    ],
    output: [
      { bg: "#f4e3d6", border: "#dcb28f", text: "#875126" },
      { bg: "#e2e9f4", border: "#afc2dc", text: "#4a6988" },
      { bg: "#e7edd7", border: "#bfd09d", text: "#5b6d33" },
      { bg: "#efe2d9", border: "#cfaf9a", text: "#7a5646" },
      { bg: "#ebe1ef", border: "#c5afd3", text: "#68517a" },
    ],
  };
  const palette = palettes[family] || palettes.tag;
  const tone = palette[hashString(String(tag)) % palette.length];
  return `background:${tone.bg};border-color:${tone.border};color:${tone.text};`;
}

function hashString(value) {
  let hash = 0;
  for (let index = 0; index < value.length; index += 1) {
    hash = ((hash << 5) - hash + value.charCodeAt(index)) >>> 0;
  }
  return hash;
}

function formatInfoButton(pluginType, pluginId) {
  return `<button class="button button-secondary button-compact button-info" data-open-plugin-info="1" data-plugin-type="${escapeAttribute(pluginType)}" data-plugin-id="${escapeAttribute(pluginId)}">Info</button>`;
}

function alertHasStateDetails(alert) {
  return Boolean(
    alert.acked_by
    || alert.acked_at
    || alert.ack_reason
    || (Array.isArray(alert.active_silences) && alert.active_silences.length > 0)
  );
}

function alertStateRows(alert) {
  const rows = [
    ["State", `<span class="state-pill state-${escapeHtml(alert.state)}">${escapeHtml(alert.state)}</span>`, true],
    ["Severity", `<span class="severity-badge severity-${escapeHtml(severityLabel(alert.severity))}">${escapeHtml(severityLabel(alert.severity))}</span>`, true],
  ];
  if (alert.acked_by || alert.acked_at || alert.ack_reason) {
    rows.push(
      ["Acked By", alert.acked_by || "-", false],
      ["Acked At", alert.acked_at ? formatDateTime(alert.acked_at) : "-", false],
      ["Ack Reason", alert.ack_reason ? renderLinkedText(alert.ack_reason) : "-", true],
    );
  }
  if (Array.isArray(alert.active_silences) && alert.active_silences.length > 0) {
    rows.push(["Active Silences", formatAlertSilences(alert.active_silences), true]);
  }
  return rows;
}

function formatAlertSilences(silences) {
  return `
    <div class="info-block-list">
      ${silences.map((silence) => `
        <div class="info-block">
          <div class="info-block-title">${escapeHtml(shortId(silence.silence_id || "-"))}</div>
          <div class="info-block-meta">${escapeHtml((silence.created_by || "-"))} · ${escapeHtml(formatWindow(silence.start_at, silence.end_at))}</div>
          <div class="info-block-text">${renderLinkedText(silence.reason || "-")}</div>
        </div>
      `).join("")}
    </div>
  `;
}

function pluginInfoRows(plugin) {
  const rows = [
    ["State", plugin.state, false],
    ["Description", plugin.description || "-", true],
    ["Updated", plugin.last_updated_at ? formatDateTime(plugin.last_updated_at) : "-", false],
    ["File", plugin.definition_file || "-", false],
  ];
  if (plugin.type === "source") {
    rows.push(
      ["Last Success", plugin.last_success_at ? formatDateTime(plugin.last_success_at) : "-", false],
      ["Last Failure", plugin.last_failure_at ? formatDateTime(plugin.last_failure_at) : "-", false],
    );
  }
  if (plugin.type === "rule") {
    rows.push(
      ["Inputs", (plugin.inputs || []).join(", ") || "-", false],
      ["Sources", (plugin.resolved_sources || []).join(", ") || "-", false],
      ["Outputs", (plugin.matched_outputs || []).join(", ") || "-", false],
      ["Runbook", plugin.runbook ? renderLinkedText(plugin.runbook) : "-", true],
    );
  }
  if (plugin.type === "output") {
    rows.push(
      ["Emit Count", String(plugin.run_count || 0), false],
      ["Last Failure", plugin.last_failure_at ? formatDateTime(plugin.last_failure_at) : "-", false],
      ["Minimum Severity", plugin.minimum_severity || "-", false],
      ["Include Tags", (plugin.include_tags || []).join(", ") || "*", false],
      ["Exclude Tags", (plugin.exclude_tags || []).join(", ") || "-", false],
      ["Exclude States", (plugin.exclude_states || []).join(", ") || "-", false],
      ["Exclude Transitions", (plugin.exclude_transitions || []).join(", ") || "-", false],
    );
  }
  if (plugin.last_error) {
    rows.push(["Last Error", plugin.last_error, false]);
  }
  return rows;
}

function infoRow(label, value, raw = false) {
  return `
    <div class="info-row">
      <div class="info-label">${escapeHtml(label)}</div>
      <div class="info-value">${raw ? value : escapeHtml(value)}</div>
    </div>
  `;
}

function formatSourceInfo(plugin) {
  return formatInfoButton(plugin.type, plugin.plugin_id);
}

function formatRuleInfo(plugin) {
  return formatInfoButton(plugin.type, plugin.plugin_id);
}

function formatOutputInfo(plugin) {
  return formatInfoButton(plugin.type, plugin.plugin_id);
}

function formatInputList(inputs) {
  const values = Array.isArray(inputs) ? inputs.filter(Boolean) : [];
  if (values.length === 0) {
    return `<span class="muted">-</span>`;
  }
  return `<div class="inline-code-cell">${values.map((value) => escapeHtml(String(value))).join("<br>")}</div>`;
}

function formatMinimumSeverity(value) {
  if (!value) {
    return `<span class="muted">-</span>`;
  }
  return `<span class="severity-badge severity-${escapeHtml(String(value))}">${escapeHtml(String(value))}</span>`;
}

function renderLinkedText(value) {
  const text = String(value || "");
  if (!text.trim()) {
    return escapeHtml("-");
  }
  const pattern = /\bhttps?:\/\/[^\s<>"']+/g;
  let cursor = 0;
  let html = "";
  for (const match of text.matchAll(pattern)) {
    let url = match[0];
    const start = match.index || 0;
    let trailing = "";
    while (/[),.;!?]$/.test(url)) {
      trailing = url.slice(-1) + trailing;
      url = url.slice(0, -1);
    }
    html += escapeHtml(text.slice(cursor, start)).replaceAll("\n", "<br>");
    html += `<a class="inline-link" href="${escapeAttribute(url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(url)}</a>`;
    html += escapeHtml(trailing);
    cursor = start + match[0].length;
  }
  html += escapeHtml(text.slice(cursor)).replaceAll("\n", "<br>");
  return html;
}

function baseName(value) {
  return String(value || "").split("/").pop() || String(value || "");
}

function bindPluginErrorDetailToggles(container) {
  for (const detail of container.querySelectorAll("[data-plugin-error-detail]")) {
    const pluginId = detail.dataset.pluginErrorDetail;
    detail.addEventListener("toggle", () => {
      if (detail.open) {
        state.expandedPluginErrors.add(pluginId);
      } else {
        state.expandedPluginErrors.delete(pluginId);
      }
    });
  }
}

function statusToColor(status) {
  if (status === "ACTIVE") return "SILENCED";
  if (status === "EXPIRED") return "SUPPRESSED";
  if (status === "CANCELLED") return "SUPPRESSED";
  return "ACKED";
}

function silenceDisplayStatus(silence) {
  if (silence.cancelled_at) {
    return "CANCELLED";
  }
  if (silence.active) {
    return "ACTIVE";
  }
  const now = Date.now();
  const start = parseIsoTime(silence.start_at);
  const end = parseIsoTime(silence.end_at);
  if (Number.isFinite(end) && end <= now) {
    return "EXPIRED";
  }
  if (Number.isFinite(start) && start > now) {
    return "SCHEDULED";
  }
  return "EXPIRED";
}

function silenceStatusRank(silence) {
  const status = silenceDisplayStatus(silence);
  return {
    ACTIVE: 0,
    SCHEDULED: 1,
    EXPIRED: 2,
    CANCELLED: 3,
  }[status] ?? 4;
}

function compareSilenceRows(left, right) {
  const rankDiff = silenceStatusRank(left) - silenceStatusRank(right);
  if (rankDiff !== 0) {
    return rankDiff;
  }
  return parseIsoTime(left.start_at) - parseIsoTime(right.start_at);
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

async function runPendingAction(actionKey, buttonIds, callback) {
  if (state.pendingActions.has(actionKey)) {
    return;
  }
  const buttons = buttonIds
    .map((buttonId) => document.getElementById(buttonId))
    .filter((button) => button !== null);
  const previousLabels = buttons.map((button) => button.textContent);
  state.pendingActions.add(actionKey);
  for (const button of buttons) {
    button.disabled = true;
    button.dataset.pendingLabel = button.textContent;
    button.textContent = "Submitting...";
  }
  try {
    await callback();
  } finally {
    state.pendingActions.delete(actionKey);
    buttons.forEach((button, index) => {
      button.textContent = previousLabels[index];
    });
    syncActionButtonState();
  }
}

function syncActionButtonState() {
  const alert = getSelectedAlert();
  if (alert) {
    updateDetailActionAvailability(alert);
  }

  const pendingMappings = [
    { actionKey: "detail-silence-duration", buttonId: "silence-for-button" },
    { actionKey: "detail-silence-window", buttonId: "silence-window-button" },
    { actionKey: "admin-silence-duration", buttonId: "admin-duration-button" },
    { actionKey: "admin-silence-window", buttonId: "admin-window-button" },
    { actionKey: "reload-dirty", buttonId: "admin-reload-dirty-button" },
    { actionKey: "reload-all", buttonId: "admin-reload-all-button" },
    { actionKey: "reload-full", buttonId: "admin-restart-engine-button" },
  ];
  for (const mapping of pendingMappings) {
    const button = document.getElementById(mapping.buttonId);
    if (!button) {
      continue;
    }
    button.disabled = state.pendingActions.has(mapping.actionKey);
  }
}

async function fetchLatestAlert(ruleId) {
  const payload = await getJson("/alerts");
  const latestAlert = (payload.alerts || []).find((alert) => alert.rule_id === ruleId);
  return latestAlert || null;
}

function rememberAlertActionSnapshot(actionType, ruleId, actionInput = null) {
  const alert = state.alerts.find((item) => item.rule_id === ruleId);
  if (!alert) {
    return;
  }
  state.lastActionSnapshots.set(`${actionType}:${ruleId}`, normalizeAlertSnapshot(alert, actionInput));
}

function shouldSkipRepeatedAlertAction(actionType, ruleId, latestAlert, actionInput = null) {
  if (!latestAlert) {
    return false;
  }
  const key = `${actionType}:${ruleId}`;
  const previousSnapshot = state.lastActionSnapshots.get(key);
  if (!previousSnapshot) {
    return false;
  }
  return JSON.stringify(previousSnapshot) === JSON.stringify(normalizeAlertSnapshot(latestAlert, actionInput));
}

function normalizeAlertSnapshot(alert, actionInput = null) {
  const snapshot = {
    rule_id: alert.rule_id,
    state: alert.state,
    severity: alert.severity,
    message: alert.message || "",
    payload: stripTimestamps(alert.payload || {}),
  };
  if (actionInput) {
    snapshot.action_input = {
      operator: actionInput.operator || "",
      reason: actionInput.reason || "",
    };
  }
  return snapshot;
}

function stripTimestamps(value) {
  if (Array.isArray(value)) {
    return value.map(stripTimestamps);
  }
  if (value && typeof value === "object") {
    const result = {};
    for (const [key, child] of Object.entries(value)) {
      if (key === "timestamp") {
        continue;
      }
      result[key] = stripTimestamps(child);
    }
    return result;
  }
  return value;
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
