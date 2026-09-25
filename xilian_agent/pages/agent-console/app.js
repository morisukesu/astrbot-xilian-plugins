// 昔涟的小本本 —— agent 执行流水面板
// 数据来自插件注册的三个 Web API（经 Dashboard 的 plugin Page bridge 转发）：
//   GET  audit/events   流水列表
//   GET  audit/summary  状态与统计
//   POST audit/clear    清空流水
// 这些接口本身带 Dashboard 登录鉴权，页面又被限制在同源 iframe 里，所以不额外加门。

const bridge = window.AstrBotPluginPage;

const REFRESH_MS = 4000;
const FETCH_LIMIT = 200;

const TOOL_LABELS = {
  xilian_agent_shell: "本机命令",
  xilian_agent_dsh: "DSH",
  xilian_agent_cyrene: "Cyrene",
  xilian_agent_guard: "管家拦截",
  xilian_agent_panel: "面板",
};

const STAGE_LABELS = {
  done: "完成",
  blocked: "已拒绝",
  denied: "没权限",
  stripped: "摘掉工具",
  cleared: "清空",
};

const state = {
  events: [],
  stats: {},
  panel: {},
  level: "",
  kind: "",
  tool: "",
  keyword: "",
  auto: true,
  loading: false,
  clearArmed: false,
};

const el = (id) => document.getElementById(id);

function escapeHtml(value) {
  return String(value === null || value === undefined ? "" : value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function riskOf(event) {
  const risk = String(event?.risk || "safe");
  return ["safe", "warn", "blocked"].includes(risk) ? risk : "safe";
}

function riskText(risk) {
  const labels = state.panel?.riskLabels || {};
  if (labels[risk]) return labels[risk];
  return { safe: "低风险", warn: "注意", blocked: "已拦截" }[risk] || risk;
}

function toolText(tool) {
  return TOOL_LABELS[tool] || tool || "—";
}

function stageText(stage) {
  return STAGE_LABELS[stage] || stage || "—";
}

// ---- 取数 ----

async function fetchEvents() {
  if (state.loading) return;
  state.loading = true;
  try {
    const params = { limit: FETCH_LIMIT };
    if (state.level) params.level = state.level;
    if (state.kind) params.kind = state.kind;
    if (state.tool) params.tool = state.tool;

    const data = await bridge.apiGet("audit/events", params);
    state.events = Array.isArray(data?.events) ? data.events : [];
    state.stats = data?.stats || {};
    state.panel = data?.state || {};
    hideError();
    renderStats();
    renderMeta();
    renderList();
    el("updated").textContent = "更新于 " + new Date().toLocaleTimeString("zh-CN");
  } catch (error) {
    showError("读不到流水：" + (error?.message || error));
  } finally {
    state.loading = false;
  }
}

// ---- 渲染 ----

function renderStats() {
  const stats = state.stats || {};
  el("stat-total").textContent = stats.total ?? 0;
  el("stat-blocked").textContent = stats.blocked ?? 0;
  el("stat-warn").textContent = stats.warn ?? 0;
  el("stat-safe").textContent = stats.safe ?? 0;

  const guardCount = state.events.filter((item) => item.kind === "guard").length;
  el("stat-guard").textContent = guardCount;

  const panel = state.panel || {};
  const enabled = Boolean(panel.enabled);
  const guardBadge = el("guard-badge");
  guardBadge.textContent = enabled
    ? "管家在岗 · " + (panel.modeLabel || panel.mode || "")
    : "管家休息中（谁都能用工具）";
  guardBadge.className = "badge " + (enabled ? "badge-on" : "badge-off");

  const toolsBadge = el("tools-badge");
  const toolsOn = Boolean(panel.toolsEnabled) && enabled;
  toolsBadge.textContent = toolsOn ? "本机工具：可以用" : "本机工具：关着";
  toolsBadge.className = "badge " + (toolsOn ? "badge-on" : "badge-off");
}

function renderMeta() {
  const panel = state.panel || {};
  el("meta-mode").textContent = "范围：" + (panel.modeLabel || panel.mode || "—");

  const allowlist = Array.isArray(panel.allowlist) ? panel.allowlist : [];
  const names = allowlist.map((qq) =>
    String(qq) === String(panel.master) ? qq + "（老公）" : qq,
  );
  el("meta-allow").textContent =
    "名单（" + allowlist.length + "）：" + (names.length ? names.join("、") : "空");

  el("meta-run").textContent = panel.lastRunAt
    ? "最近一次本机动作：" +
      panel.lastRunAt +
      " 来自 " +
      (panel.lastRunWho || "—") +
      "：" +
      (panel.lastRunBrief || "")
    : "最近一次本机动作：还没有";

  el("meta-file").textContent = panel.auditFile
    ? "流水文件：" + panel.auditFile
    : "流水文件：只留内存";

  el("foot-note").textContent =
    "内存里留最近 " +
    (state.stats?.limit ?? panel.limits?.auditMaxEvents ?? "-") +
    " 条；落盘文件超过 4MB 会自动滚动。" +
    "危险规则 " +
    (Array.isArray(panel.dangerRules) ? panel.dangerRules.length : 0) +
    " 条命中即拒。";
}

function matchKeyword(event) {
  if (!state.keyword) return true;
  const needle = state.keyword.toLowerCase();
  return [event.action, event.detail, event.output, event.actor, event.tool]
    .filter(Boolean)
    .some((field) => String(field).toLowerCase().includes(needle));
}

function cardHtml(event) {
  const risk = riskOf(event);
  const who = String(event.actor || "") || "（读不到号）";
  const isMaster = who === String(state.panel?.master || "");
  const reasons = Array.isArray(event.reasons) ? event.reasons : [];

  const pills = [];
  pills.push(
    '<span class="pill ' +
      (event.stage === "blocked" || event.stage === "denied" ? "bad" : "") +
      '">' +
      escapeHtml(stageText(event.stage)) +
      "</span>",
  );
  if (event.ok === true) pills.push('<span class="pill ok">成功</span>');
  if (event.ok === false) pills.push('<span class="pill bad">未成功</span>');
  if (event.exitCode !== null && event.exitCode !== undefined) {
    pills.push('<span class="pill">退出码 ' + escapeHtml(event.exitCode) + "</span>");
  }
  if (typeof event.durationMs === "number") {
    pills.push('<span class="pill">耗时 ' + escapeHtml(event.durationMs) + " ms</span>");
  }
  reasons.forEach((reason) => {
    pills.push('<span class="pill warn">' + escapeHtml(reason) + "</span>");
  });

  const detail = event.detail
    ? '<div class="block"><span class="block-cap">原始动作</span><pre>' +
      escapeHtml(event.detail) +
      "</pre></div>"
    : "";
  const output = event.output
    ? '<div class="block"><span class="block-cap">结果输出</span><pre>' +
      escapeHtml(event.output) +
      "</pre></div>"
    : "";

  return (
    '<article class="card risk-' +
    risk +
    '">' +
    '<div class="bar"></div>' +
    '<div class="card-body">' +
    '<div class="line1">' +
    '<span class="tag tag-' +
    risk +
    '">' +
    escapeHtml(riskText(risk)) +
    "</span>" +
    '<span class="tool">' +
    escapeHtml(toolText(event.tool)) +
    "</span>" +
    '<span class="who">' +
    escapeHtml(who) +
    (isMaster ? '<em class="master">老公</em>' : "") +
    "</span>" +
    '<time class="at">' +
    escapeHtml(event.at || "") +
    "</time>" +
    "</div>" +
    '<p class="action">' +
    escapeHtml(event.action || "（没有说明）") +
    "</p>" +
    '<div class="line2">' +
    pills.join("") +
    "</div>" +
    (detail || output
      ? "<details><summary>看细节</summary>" + detail + output + "</details>"
      : "") +
    "</div></article>"
  );
}

function renderList() {
  const visible = state.events.filter(matchKeyword);
  const list = el("list");
  list.innerHTML = visible.map(cardHtml).join("");
  el("empty").hidden = visible.length > 0;
}

function showError(message) {
  const box = el("error");
  box.textContent = message;
  box.hidden = false;
}

function hideError() {
  el("error").hidden = true;
}

// ---- 交互 ----

function bindFilters() {
  el("level-chips").addEventListener("click", (event) => {
    const chip = event.target.closest(".chip");
    if (!chip) return;
    state.level = chip.dataset.level || "";
    state.kind = "";
    syncChips();
    fetchEvents();
  });

  document.querySelectorAll(".stat").forEach((card) => {
    card.addEventListener("click", () => {
      const value = card.dataset.level || "";
      if (value === "guard") {
        state.kind = "guard";
        state.level = "";
      } else {
        state.level = value;
        state.kind = "";
      }
      el("kind-filter").value = state.kind;
      syncChips();
      fetchEvents();
    });
  });

  el("kind-filter").addEventListener("change", (event) => {
    state.kind = event.target.value;
    fetchEvents();
  });

  el("tool-filter").addEventListener("change", (event) => {
    state.tool = event.target.value;
    fetchEvents();
  });

  el("search").addEventListener("input", (event) => {
    state.keyword = event.target.value.trim();
    renderList();
  });

  el("refresh").addEventListener("click", () => fetchEvents());

  el("auto").addEventListener("change", (event) => {
    state.auto = Boolean(event.target.checked);
  });

  el("clear").addEventListener("click", onClearClick);
}

function syncChips() {
  document.querySelectorAll(".chip").forEach((chip) => {
    chip.classList.toggle("on", (chip.dataset.level || "") === state.level);
  });
}

async function onClearClick() {
  const button = el("clear");
  if (!state.clearArmed) {
    state.clearArmed = true;
    button.textContent = "再点一次确认清空";
    setTimeout(() => {
      state.clearArmed = false;
      button.textContent = "清空流水";
    }, 4000);
    return;
  }

  state.clearArmed = false;
  button.textContent = "清空中…";
  try {
    const data = await bridge.apiPost("audit/clear", { confirm: true });
    el("updated").textContent = "已清空 " + (data?.cleared ?? 0) + " 条";
  } catch (error) {
    showError("清空失败：" + (error?.message || error));
  } finally {
    button.textContent = "清空流水";
    fetchEvents();
  }
}

// ---- 启动 ----

async function boot() {
  if (!bridge) {
    showError("没找到插件 Page 的 bridge —— 请在 AstrBot 的插件页面里打开这张 Page。");
    return;
  }

  try {
    await Promise.race([
      bridge.ready(),
      new Promise((resolve) => setTimeout(resolve, 3000)),
    ]);
  } catch (error) {
    showError("页面上下文没接上：" + (error?.message || error));
  }

  bindFilters();
  await fetchEvents();

  setInterval(() => {
    if (state.auto) fetchEvents();
  }, REFRESH_MS);
}

boot();
