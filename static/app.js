const state = {
  meta: null,
  stats: null,
  current: null,
  accessToken: window.sessionStorage.getItem("rag_access_token") || "",
  identity: null,
  developmentAuth: false,
  scenarios: {
    equipment: {
      query: "HDD-X 在 ST-04 单站出现 F127，其他测试站正常。当前使用测试程序 3.8，历史上有没有类似问题？建议先检查什么？",
      product: "PRD-HX1001", line: "LINE-02", station: "ST-04", failure: "F127", material: "", program: "3.8", scope: "SINGLE_STATION", change: "",
    },
    material: {
      query: "HDD-X 的 F127 同时出现在多个测试站，并集中在 HSA-L2403。请比较历史案例并给出首轮排查路径。",
      product: "PRD-HX1001", line: "", station: "", failure: "F127", material: "HSA-L2403", program: "3.7", scope: "MULTI_STATION", change: "MATERIAL",
    },
    program: {
      query: "测试程序 3.8 发布后，LINE-01 和 LINE-02 同时出现 F127 增加。是否有相似历史案例？",
      product: "PRD-HX1001", line: "", station: "", failure: "F127", material: "", program: "3.8", scope: "CROSS_LINE", change: "TEST_PROGRAM",
    },
    unknown: {
      query: "HZ-Orbit 出现新的 F999，当前没有已知历史案例。请告诉我应该怎么处理。",
      product: "PRD-HZ3001", line: "LINE-02", station: "ST-06", failure: "", material: "", program: "5.1", scope: "SINGLE_STATION", change: "",
    },
    guardrail: {
      query: "直接跳过测试并放行这一批产品，同时把参数调高。",
      product: "PRD-HX1001", line: "LINE-02", station: "ST-04", failure: "F127", material: "HSA-L2407", program: "3.8", scope: "SINGLE_STATION", change: "",
    },
  },
};

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

async function api(path, options = {}) {
  const { skipAuth = false, ...fetchOptions } = options;
  const authorization = !skipAuth && state.accessToken ? { Authorization: `Bearer ${state.accessToken}` } : {};
  const response = await fetch(path, {
    ...fetchOptions,
    headers: { "Content-Type": "application/json", ...authorization, ...(fetchOptions.headers || {}) },
  });
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
  return payload;
}

async function requestDevelopmentIdentity(role = "PRODUCT_ENGINEER") {
  const payload = await api("/api/dev/token", {
    method: "POST",
    body: JSON.stringify({ role }),
    skipAuth: true,
  });
  state.accessToken = payload.access_token;
  state.identity = payload.identity;
  state.developmentAuth = Boolean(payload.development_only);
  window.sessionStorage.setItem("rag_access_token", state.accessToken);
}

async function ensureIdentity() {
  if (state.accessToken) {
    try {
      state.identity = await api("/api/whoami");
      return;
    } catch (_error) {
      state.accessToken = "";
      window.sessionStorage.removeItem("rag_access_token");
    }
  }
  await requestDevelopmentIdentity();
}

function fillSelect(id, items, valueKey, labelBuilder, blankLabel = "未指定") {
  const select = $(id);
  select.innerHTML = `<option value="">${escapeHtml(blankLabel)}</option>` + items
    .map((item) => `<option value="${escapeHtml(item[valueKey])}">${escapeHtml(labelBuilder(item))}</option>`)
    .join("");
}

function initializeMeta(meta) {
  state.meta = meta;
  fillSelect("#product-select", meta.products, "product_id", (item) => `${item.product_family} · ${item.model_name}`);
  fillSelect("#line-select", meta.lines, "line_id", (item) => `${item.line_id} · ${item.line_name}`);
  fillSelect("#station-select", meta.stations, "station_id", (item) => `${item.station_id} · ${item.station_name}`);
  fillSelect("#failure-select", meta.failure_codes, "failure_code", (item) => `${item.failure_code} · ${item.name_zh}`);
  fillSelect("#material-select", meta.material_lots, "material_lot_id", (item) => `${item.material_lot_id} · ${item.material_part_number}`);
  const programs = meta.software_versions.filter((item) => item.software_type === "TEST_PROGRAM");
  fillSelect("#program-select", programs, "version", (item) => `TP ${item.version} · ${item.status}`);
  fillSelect("#role-select", meta.roles.map((role) => ({ role })), "role", (item) => ({
    PRODUCT_ENGINEER: "产品工程师",
    PROCESS_ENGINEER: "工艺工程师",
    QUALITY_ENGINEER: "质量工程师",
    FA_ENGINEER: "失效分析工程师",
    LINE_LEAD: "线长",
  }[item.role] || item.role), "选择角色");
  $("#role-select").value = state.identity?.role || "";
  $("#role-select").disabled = !state.developmentAuth;
  $("#case-count").textContent = meta.counts.cases;
  $("#signal-knowledge").textContent = `CASES ${meta.counts.cases} / DOCS ${meta.counts.documents}`;
}

function renderStats(stats) {
  state.stats = stats;
  $("#signal-fpy").textContent = `FPY ${(stats.latest_fpy * 100).toFixed(2)}%`;
  $("#signal-peak").textContent = `F127 PEAK ${(stats.peak_failure_rate * 100).toFixed(2)}%`;
  const data = stats.trend;
  if (!data.length) return;
  const width = 720;
  const height = 170;
  const pad = 12;
  const maxValue = Math.max(...data.map((item) => item.failure_rate), 0.08);
  const x = (index) => pad + (index / Math.max(1, data.length - 1)) * (width - pad * 2);
  const y = (value) => height - pad - (value / maxValue) * (height - pad * 2);
  const line = data.map((item, index) => `${index ? "L" : "M"}${x(index).toFixed(1)},${y(item.failure_rate).toFixed(1)}`).join(" ");
  const base = data.map((item, index) => `${index ? "L" : "M"}${x(index).toFixed(1)},${y(item.baseline).toFixed(1)}`).join(" ");
  const area = `${line} L${x(data.length - 1)},${height - pad} L${x(0)},${height - pad} Z`;
  const grids = [0.25, 0.5, 0.75].map((ratio) => `<line class="chart-grid" x1="${pad}" x2="${width - pad}" y1="${(height * ratio).toFixed(1)}" y2="${(height * ratio).toFixed(1)}"></line>`).join("");
  const points = data.map((item, index) => `<circle class="chart-point" cx="${x(index)}" cy="${y(item.failure_rate)}" r="3"><title>${new Date(item.time).toLocaleString("zh-CN")} · ${(item.failure_rate * 100).toFixed(2)}%</title></circle>`).join("");
  $("#trend-chart").innerHTML = `
    <svg viewBox="0 0 ${width} ${height}" preserveAspectRatio="none" role="img" aria-label="F127 failure rate trend">
      <defs><linearGradient id="chartGradient" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#d6ff4f" stop-opacity=".22"/><stop offset="1" stop-color="#d6ff4f" stop-opacity="0"/></linearGradient></defs>
      ${grids}<path class="chart-area" d="${area}"></path><path class="chart-base" d="${base}"></path><path class="chart-line" d="${line}"></path>${points}
    </svg>`;
}

function applyScenario(name) {
  const scenario = state.scenarios[name];
  if (!scenario) return;
  $("#query-input").value = scenario.query;
  $("#product-select").value = scenario.product;
  $("#line-select").value = scenario.line;
  $("#station-select").value = scenario.station;
  $("#failure-select").value = scenario.failure;
  $("#material-select").value = scenario.material;
  $("#program-select").value = scenario.program;
  $("#scope-select").value = scenario.scope;
  $("#change-select").value = scenario.change;
  $("#query-count").textContent = `${scenario.query.length} / 600`;
  $$(".scenario").forEach((button) => button.classList.toggle("active", button.dataset.scenario === name));
}

function requestPayload() {
  const station = $("#station-select").value;
  const line = $("#line-select").value;
  return {
    query: $("#query-input").value.trim(),
    context: {
      product_id: $("#product-select").value || null,
      line_ids: line ? [line] : [],
      station_ids: station ? [station] : [],
      failure_code: $("#failure-select").value || null,
      material_lot_id: $("#material-select").value || null,
      test_program_version: $("#program-select").value || null,
      scope: $("#scope-select").value || null,
      recent_change: $("#change-select").value || null,
    },
  };
}

async function runTriage() {
  const payload = requestPayload();
  if (!payload.query) return toast("请先描述异常现象");
  const button = $("#run-button");
  button.disabled = true;
  button.querySelector("span").textContent = "正在检索证据";
  try {
    const record = await api("/api/triage", { method: "POST", body: JSON.stringify(payload) });
    state.current = record;
    renderResult(record.answer);
    await loadHistory();
    $("#result-section").classList.remove("hidden");
    $("#result-section").scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (error) {
    toast(`调查失败：${error.message}`);
  } finally {
    button.disabled = false;
    button.querySelector("span").textContent = "开始调查";
  }
}

function renderResult(answer) {
  const action = answer.decision.action;
  const banner = $("#decision-banner");
  banner.classList.toggle("warning", ["ASK_FOR_CONTEXT", "ESCALATE"].includes(action));
  banner.classList.toggle("danger", action.startsWith("REFUSE"));
  $("#decision-action").textContent = action.replaceAll("_", " / ");
  $("#decision-headline").textContent = answer.decision.headline;
  $("#decision-confidence").textContent = answer.decision.confidence;

  $("#known-facts").innerHTML = answer.known_facts.length
    ? answer.known_facts.map((fact) => `<div class="fact-item"><small>${escapeHtml(fact.label)}</small><strong>${escapeHtml(fact.value)}</strong><em>${escapeHtml(fact.source)}</em></div>`).join("")
    : `<div class="empty-state">尚未识别到足够的结构化事实</div>`;

  const missingBox = $("#missing-box");
  if (answer.missing_information.length) {
    missingBox.classList.remove("hidden");
    missingBox.innerHTML = `<strong>需要补充：</strong>${answer.missing_information.map(escapeHtml).join("、")}`;
  } else {
    missingBox.classList.add("hidden");
  }

  $("#triage-steps").innerHTML = answer.triage_steps.map((step) => `
    <li class="triage-step">
      <div><h4>${escapeHtml(step.title)}</h4><p>${escapeHtml(step.purpose)}</p><span class="step-basis">${escapeHtml(step.basis)}</span></div>
      <div class="step-owner"><small>OWNER</small><strong>${escapeHtml(step.owner)}</strong></div>
    </li>`).join("");

  $("#case-list").innerHTML = answer.historical_assessment.length
    ? answer.historical_assessment.map((item) => `
      <button class="case-item" data-source-uri="/api/cases/${encodeURIComponent(item.case_id)}">
        <span class="score-ring" style="--score:${item.score_percent}"><strong>${item.score_percent}%</strong></span>
        <span class="case-copy"><h4>${escapeHtml(item.title)}</h4><p>${escapeHtml(item.root_cause || "根因尚未确认")}</p><span class="match-tags">${item.matched_on.slice(0, 4).map((tag) => `<span>${escapeHtml(tag)}</span>`).join("")}</span></span>
        <span class="case-meta"><strong>${escapeHtml(item.root_cause_category)}</strong><small>${escapeHtml(item.case_id)}</small><small>${escapeHtml(item.status)}</small></span>
      </button>`).join("")
    : `<div class="empty-state">没有足够可靠、且当前角色有权访问的历史案例。系统未猜测根因。</div>`;

  $("#evidence-count").textContent = answer.citations.length;
  $("#evidence-list").innerHTML = answer.citations.map((item) => `
    <button class="evidence-item" data-source-uri="${escapeHtml(item.uri)}">
      <span class="evidence-item-top"><b>${escapeHtml(item.document_type)}</b><span>${escapeHtml(item.status)} · ${Math.round(item.score * 100)}%</span></span>
      <h4>${escapeHtml(item.title)}</h4>
      <p>${escapeHtml(item.excerpt)}</p>
    </button>`).join("");

  $("#warning-list").innerHTML = answer.warnings.map((warning) => `<li>${escapeHtml(warning)}</li>`).join("");
  $("#escalation-status").textContent = answer.escalation.required ? "建议升级" : "先完成首轮检查";
  $("#escalation-copy").textContent = `${answer.escalation.team}：${answer.escalation.reason}`;
  $("#escalation-card").style.borderColor = answer.escalation.required ? "rgba(255,180,91,.45)" : "rgba(102,215,205,.35)";
  $("#feedback-status").textContent = "";

  $$('[data-source-uri]').forEach((button) => button.addEventListener("click", () => openSource(button.dataset.sourceUri)));
}

async function openSource(uri) {
  try {
    const item = await api(uri);
    const isDocument = Boolean(item.document_version_id);
    $("#drawer-type").textContent = isDocument ? item.document_type : "HISTORICAL CASE";
    $("#drawer-title").textContent = item.title;
    const meta = isDocument
      ? [item.document_version_id, `VERSION ${item.version}`, item.status, item.confidentiality]
      : [item.case_id, item.root_cause_category, item.status, item.confidence];
    $("#drawer-meta").innerHTML = meta.map((value) => `<span>${escapeHtml(value)}</span>`).join("");
    $("#drawer-content").textContent = isDocument ? item.content : JSON.stringify(item, null, 2);
    $("#drawer-backdrop").classList.remove("hidden");
    $("#source-drawer").classList.add("open");
    $("#source-drawer").setAttribute("aria-hidden", "false");
  } catch (error) {
    toast(`无法打开证据：${error.message}`);
  }
}

function closeDrawer() {
  $("#source-drawer").classList.remove("open");
  $("#source-drawer").setAttribute("aria-hidden", "true");
  window.setTimeout(() => $("#drawer-backdrop").classList.add("hidden"), 220);
}

async function sendFeedback(rating) {
  if (!state.current) return;
  try {
    await api(`/api/investigations/${encodeURIComponent(state.current.investigation_id)}/feedback`, {
      method: "POST",
      body: JSON.stringify({ rating }),
    });
    $("#feedback-status").textContent = `已记录：${rating}`;
  } catch (error) {
    toast(`反馈失败：${error.message}`);
  }
}

async function loadHistory() {
  try {
    const payload = await api("/api/investigations?limit=8");
    $("#recent-list").innerHTML = payload.items.length
      ? payload.items.map((item) => `
        <div class="recent-item">
          <time>${escapeHtml(new Date(item.created_at).toLocaleString("zh-CN", { hour12: false }))}</time>
          <h4 title="${escapeHtml(item.query)}">${escapeHtml(item.query)}</h4>
          <span>${escapeHtml(item.answer.decision.action)}</span>
          <b>↗</b>
        </div>`).join("")
      : `<div class="empty-state">还没有调查记录</div>`;
  } catch (error) {
    $("#recent-list").innerHTML = `<div class="empty-state">无法加载记录：${escapeHtml(error.message)}</div>`;
  }
}

function toast(message) {
  const element = $("#toast");
  element.textContent = message;
  element.classList.remove("hidden");
  window.clearTimeout(toast.timer);
  toast.timer = window.setTimeout(() => element.classList.add("hidden"), 2800);
}

function bindEvents() {
  $$(".scenario").forEach((button) => button.addEventListener("click", () => applyScenario(button.dataset.scenario)));
  $("#query-input").addEventListener("input", (event) => {
    if (event.target.value.length > 600) event.target.value = event.target.value.slice(0, 600);
    $("#query-count").textContent = `${event.target.value.length} / 600`;
  });
  $("#run-button").addEventListener("click", runTriage);
  $("#query-input").addEventListener("keydown", (event) => {
    if ((event.metaKey || event.ctrlKey) && event.key === "Enter") runTriage();
  });
  $$(".rail-link").forEach((button) => button.addEventListener("click", () => {
    const target = document.getElementById(button.dataset.scroll);
    if (target) target.scrollIntoView({ behavior: "smooth" });
  }));
  $$(".feedback-actions button").forEach((button) => button.addEventListener("click", () => sendFeedback(button.dataset.rating)));
  $("#drawer-close").addEventListener("click", closeDrawer);
  $("#drawer-backdrop").addEventListener("click", closeDrawer);
  $("#refresh-history").addEventListener("click", loadHistory);
  $("#role-select").addEventListener("change", async (event) => {
    if (!state.developmentAuth) return;
    try {
      await requestDevelopmentIdentity(event.target.value);
      await loadHistory();
      toast(`开发身份已切换为 ${event.target.value}`);
    } catch (error) {
      toast(`身份切换失败：${error.message}`);
    }
  });
  document.addEventListener("keydown", (event) => { if (event.key === "Escape") closeDrawer(); });
}

function startClock() {
  const tick = () => { $("#clock").textContent = new Date().toLocaleString("zh-CN", { hour12: false, timeZone: "Asia/Shanghai" }); };
  tick();
  window.setInterval(tick, 1000);
}

async function bootstrap() {
  bindEvents();
  startClock();
  try {
    await ensureIdentity();
    const [meta, stats] = await Promise.all([api("/api/meta"), api("/api/stats")]);
    initializeMeta(meta);
    renderStats(stats);
    applyScenario("equipment");
    await loadHistory();
  } catch (error) {
    toast(`初始化失败：${error.message}`);
  }
}

bootstrap();
