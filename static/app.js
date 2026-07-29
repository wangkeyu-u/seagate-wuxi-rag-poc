const state = {
  meta: null,
  stats: null,
  current: null,
  accessToken: window.sessionStorage.getItem("rag_access_token") || "",
  identity: null,
  developmentAuth: window.sessionStorage.getItem("rag_development_auth") === "1",
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

const WORKFLOW_STATES = [
  { code: "TRIAGE", label: "初始分诊", gate: "调查所有者" },
  { code: "INVESTIGATING", label: "执行检查", gate: "调查所有者" },
  { code: "CHECKED", label: "检查完成", gate: "调查所有者" },
  { code: "ROOT_CAUSE_REVIEW", label: "根因复核", gate: "质量角色" },
  { code: "CLOSED", label: "审核关闭", gate: "质量角色" },
  { code: "PUBLISHED", label: "正式发布", gate: "质量角色" },
];
const QUALITY_ROLES = new Set(["QUALITY_ENGINEER", "ADMIN"]);
const CHECK_OUTCOME_LABELS = {
  PASS: "通过",
  FAIL: "未通过",
  INCONCLUSIVE: "证据不足",
  NOT_APPLICABLE: "不适用",
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
  if (state.developmentAuth) window.sessionStorage.setItem("rag_development_auth", "1");
}

async function ensureIdentity() {
  if (state.accessToken) {
    try {
      state.identity = await api("/api/whoami");
      return;
    } catch (_error) {
      state.accessToken = "";
      state.developmentAuth = false;
      window.sessionStorage.removeItem("rag_access_token");
      window.sessionStorage.removeItem("rag_development_auth");
    }
  }
  try {
    state.identity = await api("/api/whoami", { skipAuth: true });
    state.developmentAuth = false;
    return;
  } catch (_error) {
    // A same-origin enterprise identity proxy may inject the verified bearer
    // token upstream. If it is absent, fall through to the explicit local demo.
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
    const created = await api("/api/triage", { method: "POST", body: JSON.stringify(payload) });
    const record = await api(`/api/investigations/${encodeURIComponent(created.investigation_id)}`);
    renderInvestigation(record);
    await loadHistory();
    $("#result-section").scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (error) {
    toast(`调查失败：${error.message}`);
  } finally {
    button.disabled = false;
    button.querySelector("span").textContent = "开始调查";
  }
}

function renderInvestigation(record) {
  record.status = record.status || record.answer?.status || "TRIAGE";
  record.check_results = Array.isArray(record.check_results) ? record.check_results : [];
  record.reviews = Array.isArray(record.reviews) ? record.reviews : [];
  if (record.answer) record.answer.status = record.status;
  state.current = record;
  renderResult(record.answer);
  renderWorkflow(record);
  $("#result-section").classList.remove("hidden");
  $("#workflow-section").classList.remove("hidden");
}

function renderResult(answer) {
  const action = answer.decision.action;
  const banner = $("#decision-banner");
  banner.classList.toggle("warning", ["ASK_FOR_CONTEXT", "ESCALATE"].includes(action));
  banner.classList.toggle("danger", action.startsWith("REFUSE"));
  $("#decision-action").textContent = `${action.replaceAll("_", " / ")} · ${answer.status || "TRIAGE"}`;
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

  const generated = answer.generated_analysis;
  const modelCard = $("#model-analysis-card");
  if (generated?.hypotheses?.length) {
    modelCard.classList.remove("hidden");
    $("#model-summary").textContent = generated.summary;
    $("#hypothesis-list").innerHTML = generated.hypotheses.map((item, index) => {
      const supporting = item.supporting_evidence_ids.map((id) => {
        const citation = answer.citations.find((source) => source.citation_id === id);
        return citation
          ? `<button type="button" data-source-uri="${escapeHtml(citation.uri)}">${escapeHtml(id)}</button>`
          : `<span>${escapeHtml(id)}</span>`;
      }).join("");
      const contradicting = item.contradicting_evidence_ids.map((id) => `<span>${escapeHtml(id)}</span>`).join("");
      return `<article class="hypothesis-item">
        <div class="hypothesis-rank">H${String(index + 1).padStart(2, "0")}</div>
        <div><h4>${escapeHtml(item.label)}</h4><p>${escapeHtml(item.analysis)}</p>
          <div class="hypothesis-evidence"><small>SUPPORT</small>${supporting}</div>
          ${contradicting ? `<div class="hypothesis-evidence contradict"><small>CONTRADICT</small>${contradicting}</div>` : ""}
        </div>
      </article>`;
    }).join("");
    const modelMissing = $("#model-missing");
    if (generated.missing_information?.length) {
      modelMissing.classList.remove("hidden");
      modelMissing.innerHTML = `<strong>模型认为仍需补充：</strong>${generated.missing_information.map(escapeHtml).join("、")}`;
    } else {
      modelMissing.classList.add("hidden");
    }
  } else {
    modelCard.classList.add("hidden");
    $("#model-summary").textContent = "";
    $("#hypothesis-list").innerHTML = "";
    $("#model-missing").classList.add("hidden");
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

function workflowLabel(status) {
  return WORKFLOW_STATES.find((item) => item.code === status)?.label || status;
}

function renderWorkflow(record) {
  const status = record.status || "TRIAGE";
  const currentIndex = Math.max(0, WORKFLOW_STATES.findIndex((item) => item.code === status));
  const isOwner = state.identity?.subject === record.subject;
  const isQuality = QUALITY_ROLES.has(state.identity?.role);
  const checks = record.check_results || [];
  const reviews = record.reviews || [];

  $("#workflow-id").textContent = record.investigation_id;
  $("#workflow-track").innerHTML = WORKFLOW_STATES.map((item, index) => {
    const phase = index < currentIndex ? "complete" : index === currentIndex ? "active" : "pending";
    return `<li class="workflow-state ${phase}" ${phase === "active" ? 'aria-current="step"' : ""}>
      <span class="workflow-node">${phase === "complete" ? "✓" : String(index + 1).padStart(2, "0")}</span>
      <strong>${escapeHtml(item.label)}</strong>
      <small>${escapeHtml(item.gate)}</small>
    </li>`;
  }).join("");

  $("#check-count").textContent = checks.length;
  $("#review-count").textContent = reviews.length;
  renderWorkflowLedger(checks, reviews);

  const actor = isOwner ? `OWNER / ${state.identity?.role || "UNKNOWN"}` : isQuality ? `QUALITY GATE / ${state.identity?.role}` : "READ ONLY";
  $("#workflow-actor-boundary").textContent = actor;
  const action = workflowActionMarkup(record, { isOwner, isQuality });
  $("#workflow-action-title").textContent = action.title;
  $("#workflow-action-body").innerHTML = action.markup;
  bindWorkflowActions(record, { isOwner, isQuality });
}

function renderWorkflowLedger(checks, reviews) {
  const events = [
    ...checks.map((item) => ({ ...item, ledgerType: "CHECK" })),
    ...reviews.map((item) => ({ ...item, ledgerType: "REVIEW" })),
  ].sort((left, right) => String(left.created_at).localeCompare(String(right.created_at)));

  $("#workflow-ledger").innerHTML = events.length ? events.map((item) => {
    if (item.ledgerType === "CHECK") {
      const evidence = (item.evidence_ids || []).map((id) => `<span>${escapeHtml(id)}</span>`).join("");
      return `<article class="ledger-entry">
        <div class="ledger-rail"><i class="outcome-${escapeHtml(item.outcome.toLowerCase())}"></i></div>
        <div>
          <div class="ledger-entry-head"><b>CHECK ${String(item.step_sequence).padStart(2, "0")}</b><span>${escapeHtml(CHECK_OUTCOME_LABELS[item.outcome] || item.outcome)}</span></div>
          <p>${escapeHtml(item.notes || "未填写检查备注")}</p>
          <div class="ledger-evidence">${evidence || "<em>NO EVIDENCE ID</em>"}</div>
          <small>${escapeHtml(item.actor_subject)} · ${escapeHtml(new Date(item.created_at).toLocaleString("zh-CN", { hour12: false }))}</small>
        </div>
      </article>`;
    }
    return `<article class="ledger-entry review-entry">
      <div class="ledger-rail"><i></i></div>
      <div>
        <div class="ledger-entry-head"><b>QUALITY REVIEW</b><span>${escapeHtml(item.decision)}</span></div>
        <p>${escapeHtml(item.notes || "未填写审核意见")}</p>
        <small>${escapeHtml(item.reviewer_subject)} · ${escapeHtml(new Date(item.created_at).toLocaleString("zh-CN", { hour12: false }))}</small>
      </div>
    </article>`;
  }).join("") : `<div class="ledger-empty"><span>00</span><p>尚无执行记录。开始调查后，每次检查和审核都会在这里形成不可覆盖的证据轨迹。</p></div>`;
}

function waitingMarkup(code, title, copy) {
  return `<div class="workflow-waiting">
    <span>${escapeHtml(code)}</span>
    <div><h4>${escapeHtml(title)}</h4><p>${escapeHtml(copy)}</p></div>
  </div>`;
}

function workflowActionMarkup(record, permissions) {
  const { isOwner, isQuality } = permissions;
  const status = record.status;
  const checks = record.check_results || [];
  const steps = record.answer?.triage_steps || [];

  if (status === "TRIAGE") {
    return isOwner ? {
      title: "确认接手调查",
      markup: `<p class="workflow-lead">建议已经生成，但尚未构成执行记录。确认接手后，才可以填写现场检查结果。</p>
        <div class="workflow-boundary-note"><b>责任边界</b><span>系统提供证据路径；调查所有者对检查内容和升级决定负责。</span></div>
        <button class="workflow-primary" id="workflow-start" type="button"><span>开始执行检查</span><b>01 → 02</b></button>`,
    } : {
      title: "等待调查所有者接手",
      markup: waitingMarkup("OWNER GATE", "当前身份不能启动此调查", `调查归属 ${record.subject}，请由调查所有者确认接手。`),
    };
  }

  if (status === "INVESTIGATING") {
    if (!isOwner) return {
      title: "现场检查进行中",
      markup: waitingMarkup("OWNER GATE", "等待现场检查记录", `调查所有者 ${record.subject} 正在执行首轮检查。`),
    };
    const options = steps.map((step, index) => `<option value="${Number(step.sequence) || index + 1}">${String(Number(step.sequence) || index + 1).padStart(2, "0")} · ${escapeHtml(step.title)}</option>`).join("");
    return {
      title: "记录一项检查结果",
      markup: `<form class="workflow-form" id="check-form">
        <div class="workflow-form-grid">
          <label><span>检查步骤</span><select id="check-step" required>${options}</select></label>
          <label><span>检查结论</span><select id="check-outcome" required>
            <option value="PASS">通过 / PASS</option>
            <option value="FAIL">未通过 / FAIL</option>
            <option value="INCONCLUSIVE">证据不足 / INCONCLUSIVE</option>
            <option value="NOT_APPLICABLE">不适用 / N/A</option>
          </select></label>
        </div>
        <label class="workflow-notes"><span>现场备注</span><textarea id="check-notes" aria-label="现场备注" maxlength="4000" rows="4" placeholder="记录观察值、对比范围和未决问题。不要在这里写最终放行决定。"></textarea><small aria-hidden="true"><b id="check-note-count">0</b> / 4000</small></label>
        <fieldset class="evidence-selector"><legend>绑定步骤证据</legend><div id="workflow-evidence-options"></div></fieldset>
        <div class="workflow-button-row">
          <button class="workflow-secondary" id="record-check" type="submit">写入检查记录</button>
          <button class="workflow-primary" id="workflow-checked" type="button" ${checks.length ? "" : "disabled"}><span>完成首轮检查</span><b>02 → 03</b></button>
        </div>
        ${checks.length ? "" : '<p class="workflow-hint">至少写入一项检查记录后，才能完成首轮检查。</p>'}
      </form>`,
    };
  }

  if (status === "CHECKED") {
    return isOwner ? {
      title: "提交质量复核",
      markup: `<p class="workflow-lead">已记录 ${checks.length} 项检查。提交后，调查进入质量角色复核；如证据不足，审核人可以退回继续调查。</p>
        <div class="workflow-boundary-note"><b>提交前确认</b><span>检查结论和引用证据已完整，且没有把历史相似性写成已确认根因。</span></div>
        <button class="workflow-primary" id="workflow-submit-review" type="button"><span>提交根因复核</span><b>03 → 04</b></button>`,
    } : {
      title: "等待调查所有者提交",
      markup: waitingMarkup("OWNER GATE", "检查已经完成", "调查所有者尚未把记录提交到质量复核队列。"),
    };
  }

  if (status === "ROOT_CAUSE_REVIEW") {
    if (!isQuality) return {
      title: "质量复核进行中",
      markup: waitingMarkup("QUALITY GATE", "等待独立质量复核", "只有质量工程师或管理员可以批准或退回这项调查。"),
    };
    return {
      title: "审核证据闭环",
      markup: `<form class="workflow-form" id="review-form">
        <p class="workflow-lead">复核 ${checks.length} 项现场记录。批准会关闭调查；退回会重新进入检查阶段。</p>
        <label class="workflow-notes"><span>审核意见</span><textarea id="review-notes" aria-label="审核意见" maxlength="4000" rows="5" required placeholder="说明证据是否支持结论，以及批准或退回的具体理由。"></textarea><small aria-hidden="true"><b id="review-note-count">0</b> / 4000</small></label>
        <div class="workflow-button-row review-buttons">
          <button class="workflow-danger" id="workflow-reject" type="button">退回继续调查</button>
          <button class="workflow-primary" id="workflow-approve" type="submit"><span>批准并关闭</span><b>04 → 05</b></button>
        </div>
      </form>`,
    };
  }

  if (status === "CLOSED") {
    return isQuality ? {
      title: "发布已审核调查",
      markup: `<p class="workflow-lead">质量审核已经完成。发布会把当前记录标记为正式可复用状态，且不能再回退。</p>
        <div class="workflow-boundary-note"><b>不可逆边界</b><span>确认记录不含敏感原文，引用版本有效，审核意见足以支持后续复用。</span></div>
        <button class="workflow-primary publish-button" id="workflow-publish" type="button"><span>正式发布调查</span><b>05 → 06</b></button>`,
    } : {
      title: "等待质量角色发布",
      markup: waitingMarkup("QUALITY GATE", "调查已经审核关闭", "只有质量工程师或管理员可以发布为正式记录。"),
    };
  }

  return {
    title: "调查已正式发布",
    markup: `<div class="workflow-published"><span>✓</span><div><h4>证据闭环完成</h4><p>该记录已通过质量复核并发布。后续使用仍需核对适用条件，不能直接替代当前批次的工程判断。</p></div></div>`,
  };
}

function renderEvidenceOptions(record, sequence) {
  const step = (record.answer?.triage_steps || []).find((item) => Number(item.sequence) === Number(sequence));
  const evidence = step?.evidence_ids || [];
  $("#workflow-evidence-options").innerHTML = evidence.length
    ? evidence.map((id) => `<label><input type="checkbox" name="workflow-evidence" value="${escapeHtml(id)}" checked /><span>${escapeHtml(id)}</span></label>`).join("")
    : `<p>该步骤没有预绑定证据；可以先记录观察结果。</p>`;
}

function bindWorkflowActions(record, permissions) {
  const status = record.status;
  if (status === "TRIAGE" && permissions.isOwner) {
    $("#workflow-start").addEventListener("click", () => transitionWorkflow("INVESTIGATING", "调查已开始"));
  }
  if (status === "INVESTIGATING" && permissions.isOwner) {
    const step = $("#check-step");
    renderEvidenceOptions(record, step.value);
    step.addEventListener("change", () => renderEvidenceOptions(record, step.value));
    $("#check-notes").addEventListener("input", (event) => { $("#check-note-count").textContent = event.target.value.length; });
    $("#check-form").addEventListener("submit", recordWorkflowCheck);
    if ($("#workflow-checked")) $("#workflow-checked").addEventListener("click", () => transitionWorkflow("CHECKED", "首轮检查已完成"));
  }
  if (status === "CHECKED" && permissions.isOwner) {
    $("#workflow-submit-review").addEventListener("click", () => transitionWorkflow("ROOT_CAUSE_REVIEW", "已提交质量复核"));
  }
  if (status === "ROOT_CAUSE_REVIEW" && permissions.isQuality) {
    $("#review-notes").addEventListener("input", (event) => { $("#review-note-count").textContent = event.target.value.length; });
    $("#review-form").addEventListener("submit", (event) => { event.preventDefault(); submitWorkflowReview("APPROVE"); });
    $("#workflow-reject").addEventListener("click", () => submitWorkflowReview("REJECT"));
  }
  if (status === "CLOSED" && permissions.isQuality) {
    $("#workflow-publish").addEventListener("click", () => transitionWorkflow("PUBLISHED", "调查已正式发布"));
  }
}

async function workflowMutation(path, payload, successMessage) {
  const actionBody = $("#workflow-action-body");
  actionBody.setAttribute("aria-busy", "true");
  actionBody.querySelectorAll("button, select, textarea, input").forEach((element) => { element.disabled = true; });
  try {
    await api(path, { method: "POST", body: JSON.stringify(payload) });
    await refreshCurrentInvestigation();
    await loadHistory();
    toast(successMessage);
  } catch (error) {
    renderWorkflow(state.current);
    toast(`工作流操作失败：${error.message}`);
  }
}

async function transitionWorkflow(targetStatus, successMessage) {
  if (!state.current) return;
  await workflowMutation(
    `/api/investigations/${encodeURIComponent(state.current.investigation_id)}/status`,
    { status: targetStatus },
    successMessage,
  );
}

async function recordWorkflowCheck(event) {
  event.preventDefault();
  if (!state.current) return;
  const evidenceIds = $$('input[name="workflow-evidence"]:checked').map((input) => input.value);
  await workflowMutation(
    `/api/investigations/${encodeURIComponent(state.current.investigation_id)}/checks`,
    {
      step_sequence: Number($("#check-step").value),
      outcome: $("#check-outcome").value,
      notes: $("#check-notes").value.trim(),
      evidence_ids: evidenceIds,
    },
    "检查结果已写入证据账本",
  );
}

async function submitWorkflowReview(decision) {
  if (!state.current) return;
  const notes = $("#review-notes").value.trim();
  if (!notes) return toast("请填写审核意见，再做出质量决定");
  await workflowMutation(
    `/api/investigations/${encodeURIComponent(state.current.investigation_id)}/reviews`,
    { decision, notes },
    decision === "APPROVE" ? "质量审核已批准" : "调查已退回继续检查",
  );
}

async function refreshCurrentInvestigation() {
  if (!state.current) return;
  const record = await api(`/api/investigations/${encodeURIComponent(state.current.investigation_id)}`);
  renderInvestigation(record);
}

async function openInvestigation(investigationId, { scroll = true } = {}) {
  const record = await api(`/api/investigations/${encodeURIComponent(investigationId)}`);
  renderInvestigation(record);
  if (scroll) $("#workflow-section").scrollIntoView({ behavior: "smooth", block: "start" });
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
        <button class="recent-item ${state.current?.investigation_id === item.investigation_id ? "current" : ""}" type="button" data-investigation-id="${escapeHtml(item.investigation_id)}">
          <time>${escapeHtml(new Date(item.created_at).toLocaleString("zh-CN", { hour12: false }))}</time>
          <h4 title="${escapeHtml(item.query)}">${escapeHtml(item.query)}</h4>
          <span>${escapeHtml(workflowLabel(item.status || item.answer.status || item.answer.decision.action))}</span>
          <b>↗</b>
        </button>`).join("")
      : `<div class="empty-state">还没有调查记录</div>`;
    $$('[data-investigation-id]').forEach((button) => button.addEventListener("click", () => {
      openInvestigation(button.dataset.investigationId).catch((error) => toast(`无法打开调查：${error.message}`));
    }));
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
      if (state.current) {
        try {
          await refreshCurrentInvestigation();
        } catch (_error) {
          state.current = null;
          $("#result-section").classList.add("hidden");
          $("#workflow-section").classList.add("hidden");
        }
      }
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
    $("#engine-status").textContent = "ONLINE / VERIFIED";
    applyScenario("equipment");
    await loadHistory();
  } catch (error) {
    toast(`初始化失败：${error.message}`);
  }
}

bootstrap();
