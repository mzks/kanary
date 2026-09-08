import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";

const source = fs.readFileSync(new URL("../src/kanary/web/app.js", import.meta.url), "utf8");

function createContext() {
  const status = { textContent: "" };
  const context = vm.createContext({
    console,
    document: {
      getElementById(id) {
        return id === "history-status" ? status : null;
      },
    },
    requestedUrls: [],
    responses: [],
    URLSearchParams,
    window: {
      addEventListener() {},
    },
  });
  vm.runInContext(source, context);
  return context;
}

{
  const context = createContext();
  const result = vm.runInContext(`mergeHistoryEntries([], [
    {
      kind: "operator_action",
      id: 4,
      action_type: "ack",
      rule_id: "database.down",
      occurred_at: "2026-01-01T00:00:00Z",
      operator: "alice",
      reason: "checking"
    },
    {
      kind: "alert_event",
      id: 7,
      rule_id: "database.down",
      occurred_at: "2026-01-01T00:00:00Z",
      current_state: "ACKED",
      transition: null
    }
  ])`, context);

  assert.equal(result.length, 1);
  assert.equal(result[0].kind, "alert_event");
  assert.equal(result[0].action_type, "ack");
  assert.equal(result[0].operator, "alice");
  assert.equal(result[0].reason, "checking");
}

{
  const context = createContext();
  context.responses = [
    {
      enabled: true,
      entries: [{ kind: "alert_event", id: 11, occurred_at: "2026-01-01T00:00:01Z" }],
      latest_alert_id: 11,
      latest_action_id: 20,
      alert_has_more: true,
      action_has_more: false,
    },
    {
      enabled: true,
      entries: [{ kind: "alert_event", id: 12, occurred_at: "2026-01-01T00:00:02Z" }],
      latest_alert_id: 12,
      latest_action_id: 20,
      alert_has_more: false,
      action_has_more: false,
    },
  ];
  await vm.runInContext(`
    state.route = "history";
    state.historyLoaded = true;
    state.historyCursor = { alertId: 10, actionId: 20 };
    renderHistoryPage = () => {};
    getJson = async (url) => {
      requestedUrls.push(url);
      return responses.shift();
    };
    loadHistory();
  `, context);

  assert.equal(context.requestedUrls.length, 2);
  assert.match(context.requestedUrls[0], /after_alert_id=10/);
  assert.match(context.requestedUrls[1], /after_alert_id=11/);
  assert.equal(vm.runInContext("state.historyEntries.length", context), 2);
  assert.equal(vm.runInContext("state.historyCatchingUp", context), false);
}

{
  const context = createContext();
  const matches = vm.runInContext(`
    state.historyFilter = "database.*";
    state.historyStateFilter = "";
    historyEntryMatchesFilters({
      kind: "operator_action",
      action_type: "cancel_silence",
      details: { rule_patterns: ["database.*"] }
    });
  `, context);

  assert.equal(matches, true);
}
