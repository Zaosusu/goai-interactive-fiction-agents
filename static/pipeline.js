const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => Array.from(document.querySelectorAll(selector));

const STORAGE_KEY = "npc-agent-pipeline-workbench-v1";
const CONFIG_KEY = "npc-agent-pipeline-config-v1";
const CONFIG_MIGRATED_KEY = "npc-agent-pipeline-config-migrated-v1";
const EYE_ICON = `
  <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
    <path d="M2.1 12s3.6-6.5 9.9-6.5S21.9 12 21.9 12s-3.6 6.5-9.9 6.5S2.1 12 2.1 12Z"></path>
    <circle cx="12" cy="12" r="2.7"></circle>
  </svg>
`;
const EYE_OFF_ICON = `
  <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
    <path d="M3 3l18 18"></path>
    <path d="M10.6 5.7A10.3 10.3 0 0 1 12 5.5c6.3 0 9.9 6.5 9.9 6.5a17.3 17.3 0 0 1-2.9 3.7"></path>
    <path d="M14.2 14.2A3 3 0 0 1 9.8 9.8"></path>
    <path d="M6.7 6.7C3.7 8.6 2.1 12 2.1 12s3.6 6.5 9.9 6.5c1.8 0 3.4-.5 4.8-1.2"></path>
  </svg>
`;

const tabs = [
  { id: "script", title: "剧本输入", agent: "ScriptDecompositionAgent" },
  { id: "decomposition", title: "剧本理解", agent: "ScriptDecompositionAgent" },
  { id: "graph", title: "故事图谱", agent: "ScriptGraphCompiler / ScriptGraphStore" },
  { id: "prompts", title: "视觉提示词", agent: "VisualPromptComposerAgent" },
  { id: "images", title: "图片生成", agent: "VisualAssetGenerationAgent" },
  { id: "world", title: "世界生成", agent: "WorldBuilderAgent / World API" },
  { id: "lorebook", title: "世界书", agent: "NpcLorebookCreationAgent / NpcLorebookRuntime" },
  { id: "npc", title: "NPC 对话", agent: "NPC Runtime API" },
  { id: "playtest", title: "试玩验证", agent: "Game Runtime / World Action API" },
];

const stageDefaults = Object.fromEntries(tabs.map((tab) => [tab.id, "idle"]));

const state = {
  activeTab: "script",
  stages: { ...stageDefaults },
  sourceText: "",
  decompositionResponse: null,
  decomposition: null,
  scriptGraph: null,
  graphViewport: { scale: 1, x: 0, y: 0 },
  graphNodeOverrides: {},
  report: null,
  world: null,
  visualPlan: null,
  imageResult: null,
  selectedImageAssetId: "",
  playtestSnapshot: null,
  playtestLog: [],
  npcLog: [],
  lastNpcResponseJson: null,
  npcRuntimeSnapshot: null,
  selectedNpcRuntimeId: "",
  runLog: [],
  worlds: [],
  decompositionArtifacts: [],
  decompositionArtifactId: "",
  scriptGraphArtifacts: [],
  scriptGraphArtifactId: "",
  visualAssetArtifacts: [],
  visualAssetArtifactId: "",
  visualAssetRuns: [],
  selectedVisualAssetRunId: "",
  selectedLorebookVersionId: "",
  effectiveConfig: null,
  currentJobId: null,
  currentJobKind: "",
  cancelRequested: false,
  groupChatRunning: false,
};

const hotLoadTokens = {
  decomposition: 0,
  graph: 0,
};

let graphSuppressClickUntil = 0;

const defaultConfig = {
  defaultLlmBaseUrl: "",
  defaultLlmApiKey: "",
  defaultLlmModel: "",
  defaultImageProvider: "stepfun",
  defaultImageBaseUrl: "https://api.stepfun.com/step_plan/v1",
  defaultImageApiKey: "",
  defaultImageModel: "step-image-edit-2",
  defaultImageSize: "1024x1024",
  defaultImageRetry: 3,
  defaultImageSeed: "",
  defaultImageSteps: 8,
  defaultImageCfgScale: 1,
  defaultImageTextMode: false,
  scriptUseDefaultLlm: true,
  scriptBaseUrl: "",
  scriptApiKey: "",
  scriptModel: "",
  worldUseDefaultLlm: true,
  worldBaseUrl: "",
  worldApiKey: "",
  worldModel: "",
  visualUseDefaultLlm: true,
  visualPromptBaseUrl: "",
  visualPromptApiKey: "",
  visualPromptModel: "",
  imageUseDefaultImage: true,
  imageProvider: "stepfun",
  imageBaseUrl: "https://api.stepfun.com/step_plan/v1",
  imageApiKey: "",
  imageModel: "step-image-edit-2",
  imageSize: "1024x1024",
  imageRetry: 3,
  imageSeed: "",
  imageSteps: 8,
  imageCfgScale: 1,
  imageTextMode: false,
  npcUseDefaultLlm: true,
  npcBaseUrl: "",
  npcApiKey: "",
  npcModel: "",
};

let config = { ...defaultConfig };
let activeAbortController = null;
let activeAbortLabel = "";
let configSaveTimer = null;
let groupChatAbortController = null;
let groupChatRunId = 0;

const stepfunImageModels = ["step-image-edit-2"];
// StepFun size values are kept as provider input values; labels describe observed output orientation.
const stepfunImageSizes = [
  { value: "1024x1024", label: "正方形 1024宽 x 1024高" },
  { value: "1360x768", label: "纵向 768宽 x 1360高" },
  { value: "1184x896", label: "纵向 896宽 x 1184高" },
  { value: "768x1360", label: "横向 1360宽 x 768高" },
  { value: "896x1184", label: "横向 1184宽 x 896高" },
];

async function init() {
  restoreConfig();
  restoreState();
  renderSecretButtons();
  renderTabs();
  renderStages();
  bindEvents();
  fillConfigForm();
  await migrateLocalConfigToBackend();
  await loadEffectiveConfig();
  loadWorlds();
  loadDecompositionArtifacts();
  loadScriptGraphArtifacts();
  loadVisualAssetArtifacts();
  renderAll();
  updateCancelButton();
}

function bindEvents() {
  $("#reset-state").addEventListener("click", resetState);
  $("#clear-run-log").addEventListener("click", clearRunLog);
  $("#cancel-current-job").addEventListener("click", cancelCurrentJob);
  $("#load-effective-config").addEventListener("click", () => loadEffectiveConfig(true));
  $("#save-config").addEventListener("click", saveConfigFromForm);
  bindConfigAutosave();
  $("#default-image-provider").addEventListener("change", () =>
    syncImageProviderControls({
      providerSelector: "#default-image-provider",
      modelSelector: "#default-image-model",
      sizeSelector: "#default-image-size",
    }),
  );
  $("#image-provider").addEventListener("change", () => syncImageProviderControls());
  $$("[id$='use-default-llm'], #image-use-default-image").forEach((input) => input.addEventListener("change", syncAgentConfigVisibility));
  $$(".reveal-secret").forEach((button) => button.addEventListener("click", toggleSecretVisibility));
  $("#load-file").addEventListener("click", loadSelectedFile);
  $("#import-document").addEventListener("click", importDocument);
  $("#run-decomposition").addEventListener("click", runDecomposition);
  $("#use-current-world").addEventListener("click", loadPickedWorld);
  $("#format-decomposition").addEventListener("click", formatDecompositionJson);
  $("#apply-decomposition").addEventListener("click", applyDecompositionEdit);
  $("#refresh-decompositions").addEventListener("click", () => loadDecompositionArtifacts(true));
  $("#load-decomposition-artifact").addEventListener("click", loadPickedDecompositionArtifact);
  $("#decomposition-artifact-picker").addEventListener("change", hotLoadPickedDecompositionArtifact);
  $("#rebuild-world").addEventListener("click", rebuildWorldFromCurrentJson);
  $("#compile-script-graph").addEventListener("click", compileScriptGraph);
  $("#refresh-script-graphs").addEventListener("click", () => loadScriptGraphArtifacts(true));
  $("#load-script-graph-artifact").addEventListener("click", loadPickedScriptGraphArtifact);
  $("#script-graph-artifact-picker").addEventListener("change", hotLoadPickedScriptGraphArtifact);
  $("#refresh-visual-script-graphs").addEventListener("click", () => loadScriptGraphArtifacts(true));
  $("#load-visual-script-graph").addEventListener("click", loadPickedVisualScriptGraphArtifact);
  $("#visual-script-graph-picker").addEventListener("change", hotLoadPickedVisualScriptGraphArtifact);
  $("#refresh-world-script-graphs").addEventListener("click", () => loadScriptGraphArtifacts(true));
  $("#load-world-script-graph").addEventListener("click", loadPickedWorldScriptGraphArtifact);
  $("#world-script-graph-picker").addEventListener("change", hotLoadPickedWorldScriptGraphArtifact);
  $("#refresh-world-visual-asset-runs").addEventListener("click", () => loadWorldVisualAssetRuns(true));
  $("#load-world-visual-asset-run").addEventListener("click", loadPickedWorldVisualAssetRun);
  $("#world-visual-asset-run-picker").addEventListener("change", hotLoadPickedWorldVisualAssetRun);
  $("#plan-visuals").addEventListener("click", planVisuals);
  $("#apply-plan-edits").addEventListener("click", applyVisualPlanEdit);
  $("#refresh-visual-assets").addEventListener("click", () => loadVisualAssetArtifacts(true));
  $("#load-visual-asset").addEventListener("click", loadPickedVisualAssetArtifact);
  $("#refresh-visual-asset-runs").addEventListener("click", () => loadVisualAssetRuns(true));
  $("#load-visual-asset-run").addEventListener("click", loadPickedVisualAssetRun);
  $("#delete-visual-asset-run").addEventListener("click", deletePickedVisualAssetRun);
  $("#apply-image-asset-edit").addEventListener("click", applyImageAssetEdit);
  $("#generate-images").addEventListener("click", generateImages);
  $("#generate-world-from-decomposition").addEventListener("click", generateWorldFromDecomposition);
  $("#save-world").addEventListener("click", saveWorldJson);
  $("#refresh-lorebook-worlds").addEventListener("click", () => loadWorlds(true));
  $("#load-lorebook-world").addEventListener("click", loadPickedLorebookWorld);
  $("#generate-lorebook").addEventListener("click", generateLorebookForCurrentWorld);
  $("#lorebook-world-picker").addEventListener("change", hotLoadPickedLorebookWorld);
  $("#lorebook-version-picker").addEventListener("change", previewPickedLorebookVersion);
  $("#select-lorebook-version").addEventListener("click", selectPickedLorebookVersion);
  $("#playtest-world-picker").addEventListener("change", hotLoadPickedPlaytestWorld);
  $("#playtest-start").addEventListener("click", startPlaytest);
  $("#playtest-refresh").addEventListener("click", refreshPlaytest);
  $("#playtest-move").addEventListener("click", playtestMove);
  $("#playtest-inspect").addEventListener("click", playtestInspect);
  $("#playtest-run-action").addEventListener("click", playtestRunConfiguredAction);
  $("#playtest-run-custom").addEventListener("click", playtestRunCustomAction);
  $("#playtest-send-chat").addEventListener("click", playtestSendChat);
  $("#playtest-chat-message").addEventListener("keydown", (event) => {
    if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
      event.preventDefault();
      playtestSendChat();
    }
  });
  $("#refresh-npc-worlds").addEventListener("click", () => loadWorlds(true));
  $("#load-npc-world").addEventListener("click", loadPickedNpcWorld);
  $("#npc-world-picker").addEventListener("change", hotLoadPickedNpcWorld);
  $("#npc-runtime-state-picker").addEventListener("change", selectNpcRuntimeContext);
  $("#refresh-npc-runtime-context").addEventListener("click", () => loadNpcRuntimeContext(true));
  $("#target-npc").addEventListener("change", syncNpcRuntimeContextFromTarget);
  $("#chat-location").addEventListener("change", renderNpcControls);
  $("#npc-lorebook-version-picker").addEventListener("change", previewPickedNpcLorebookVersion);
  $("#select-npc-lorebook-version").addEventListener("click", selectPickedNpcLorebookVersion);
  $("#start-world").addEventListener("click", startWorld);
  $("#send-chat").addEventListener("click", sendChat);
  $("#start-group-chat").addEventListener("click", startContinuousGroupChat);
  $("#stop-group-chat").addEventListener("click", stopContinuousGroupChat);
  $("#clear-npc-log").addEventListener("click", clearNpcLog);
  $("#tick-agent").addEventListener("click", tickAgent);

  $("#source-text").addEventListener("input", () => {
    state.sourceText = $("#source-text").value;
    setStage("script", "dirty");
    persistState();
  });
  $("#script-file").addEventListener("change", updateSelectedFilesStatus);
  $("#script-folder").addEventListener("change", updateSelectedFilesStatus);
  $("#decomposition-json").addEventListener("input", () => setStage("decomposition", "dirty"));
  $("#script-graph-json").addEventListener("input", () => setStage("graph", "dirty"));
  $("#visual-plan-json").addEventListener("input", () => setStage("prompts", "dirty"));
  $("#world-json").addEventListener("input", () => {
    setStage("world", "dirty");
    setDownstreamDirty(["lorebook", "npc", "playtest"]);
  });
}

function renderTabs() {
  const tabbar = $("#tabbar");
  tabbar.innerHTML = "";
  for (const tab of tabs) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "tab-button";
    button.dataset.tab = tab.id;
    button.textContent = tab.title;
    button.addEventListener("click", () => switchTab(tab.id));
    tabbar.appendChild(button);
  }
}

function switchTab(tabId) {
  state.activeTab = tabId;
  $$(".tab-button").forEach((button) => button.classList.toggle("active", button.dataset.tab === tabId));
  $$(".tab-page").forEach((page) => page.classList.toggle("active", page.dataset.page === tabId));
  const tab = tabs.find((item) => item.id === tabId);
  $("#active-title").textContent = tab.title;
  $("#active-agent").textContent = tab.agent;
  persistState();
}

function renderStages() {
  const container = $("#stage-list");
  container.innerHTML = "";
  for (const tab of tabs) {
    const row = document.createElement("button");
    row.type = "button";
    row.className = "stage-row";
    row.addEventListener("click", () => switchTab(tab.id));
    row.innerHTML = `
      <span class="stage-copy">
        <span class="stage-heading">
          <strong>${escapeHtml(tab.title)}</strong>
          <em class="badge ${state.stages[tab.id] || "idle"}">${stageLabel(state.stages[tab.id])}</em>
        </span>
        <span class="stage-agent" title="${escapeAttribute(tab.agent)}">${escapeHtml(tab.agent)}</span>
      </span>
    `;
    container.appendChild(row);
  }
  switchTab(state.activeTab);
}

function stageLabel(status) {
  return {
    idle: "待处理",
    dirty: "已修改",
    running: "运行中",
    done: "完成",
    error: "错误",
  }[status || "idle"];
}

function setStage(stage, status) {
  state.stages[stage] = status;
  renderStages();
  persistState();
}

function renderAll() {
  $("#source-text").value = state.sourceText || "";
  renderEffectiveConfig();
  renderRunMeta();
  renderReport();
  renderDecompositionEditor();
  renderDecompositionInspector();
  renderScriptGraph();
  renderVisualPlan();
  renderVisualAssetRunPicker();
  renderImages();
  renderWorld();
  renderLorebook();
  renderPlaytest();
  renderNpcControls();
  renderGroupChatControls();
  renderNpcLog();
  renderNpcResponseJson();
  renderRunLog();
  renderStages();
}

function renderRunMeta() {
  const title = state.decomposition?.title || state.world?.name || $("#script-title").value || "未命名";
  $("#run-title").textContent = title;
  $("#run-world").textContent = state.world?.world_id || "未创建";
  $("#run-assets").textContent = String(state.visualPlan?.assets?.length || state.imageResult?.plan?.assets?.length || 0);
  const creatorLink = $("#open-creator");
  const worldId = String(state.world?.world_id || "").trim();
  creatorLink.href = worldId ? `/creator?world=${encodeURIComponent(worldId)}` : "/creator";
  creatorLink.title = worldId ? `在 Creator 中打开 ${worldId}` : "进入 Creator 创作平台";
}

function renderReport() {
  const report = state.report || state.decomposition?.report || {};
  $("#summary-passed").textContent = typeof report.passed === "boolean" ? (report.passed ? "是" : "否") : "-";
  $("#summary-nodes").textContent = String(report.node_count ?? state.decomposition?.story_graph?.entities?.length ?? 0);
  $("#summary-edges").textContent = String(report.edge_count ?? state.decomposition?.story_graph?.relations?.length ?? 0);
  $("#summary-evidence").textContent = String(report.evidence_count ?? countStoryGraphEvidence(state.decomposition?.story_graph));
  $("#decomposition-report").textContent = state.report ? pretty(state.report) : "暂无拆解报告";
}

function renderDecompositionEditor() {
  $("#decomposition-json").value = state.decomposition ? pretty(state.decomposition) : "";
}

function renderDecompositionInspector() {
  const container = $("#decomposition-inspector");
  const decomposition = state.decomposition;
  if (!decomposition) {
    container.innerHTML = `<div class="inspector-item"><strong>暂无数据</strong><small>先运行拆解，或载入已有世界。</small></div>`;
    return;
  }
  const report = state.report || decomposition.report || {};
  const storyGraph = decomposition.story_graph || {};
  const items = [
    ["标题", decomposition.title || "-"],
    ["拆解模式", decomposition.metadata?.decomposition_mode || "rules"],
    ["图节点", String(report.node_count ?? storyGraph.entities?.length ?? 0)],
    ["图关系", String(report.edge_count ?? storyGraph.relations?.length ?? 0)],
    ["证据片段", String(report.evidence_count ?? countStoryGraphEvidence(storyGraph))],
    ["实体类型", summarizeCounts(report.entity_counts) || "-"],
    ["关系类型", summarizeCounts(report.relation_counts) || "-"],
    ["悬空引用", (report.unresolved_references || []).join("、") || "无"],
    ["孤立节点", (report.isolated_nodes || []).join("、") || "无"],
    ["错误", (report.errors || []).join("\n") || "无"],
    ["警告", [...(report.warnings || []), ...(report.ontology_warnings || [])].join("\n") || "无"],
  ];
  container.innerHTML = items
    .map(([title, value]) => `<div class="inspector-item"><strong>${escapeHtml(title)}</strong><small>${escapeHtml(value)}</small></div>`)
    .join("");
}

function renderScriptGraph() {
  const editor = $("#script-graph-json");
  if (editor) editor.value = state.scriptGraph ? pretty(state.scriptGraph) : "";
  const container = $("#script-graph-inspector");
  const graphTargets = {
    map: $("#script-graph-map"),
    nodeList: $("#script-graph-node-list"),
    edgeList: $("#script-graph-edge-list"),
    nodeCount: $("#script-graph-node-count"),
    edgeCount: $("#script-graph-edge-count"),
  };
  if (!container) return;
  const graph = state.scriptGraph;
  if (!graph) {
    container.innerHTML = `<div class="inspector-item"><strong>暂无图谱</strong><small>先完成剧本理解，再手动编译故事图谱。</small></div>`;
    renderScriptGraphBrowser(null, graphTargets);
    return;
  }
  const nodeCounts = graph.indexes?.node_counts || {};
  const edgeCounts = graph.indexes?.edge_counts || {};
  const items = [
    ["图谱 ID", graph.graph_id || "-"],
    ["标题", graph.title || "-"],
    ["节点数", String(graph.nodes?.length || 0)],
    ["边数", String(graph.edges?.length || 0)],
    ["节点类型", Object.entries(nodeCounts).map(([key, value]) => `${key}: ${value}`).join("\n") || "-"],
    ["关系类型", Object.entries(edgeCounts).map(([key, value]) => `${key}: ${value}`).join("\n") || "-"],
    ["存储目标", (graph.ontology?.storage_targets || []).join("、") || "json_artifact"],
  ];
  container.innerHTML = items
    .map(([title, value]) => `<div class="inspector-item"><strong>${escapeHtml(title)}</strong><small>${escapeHtml(value)}</small></div>`)
    .join("");
  renderScriptGraphBrowser(graph, graphTargets);
  bindGraphCanvasInteractions();
}

function renderScriptGraphBrowser(graph, targets = {}) {
  const nodes = Array.isArray(graph?.nodes) ? graph.nodes : [];
  const edges = Array.isArray(graph?.edges) ? graph.edges : [];
  if (targets.nodeCount) targets.nodeCount.textContent = String(nodes.length);
  if (targets.edgeCount) targets.edgeCount.textContent = String(edges.length);
  if (targets.map) targets.map.innerHTML = renderScriptGraphMap(nodes, edges);
  if (targets.nodeList) {
    targets.nodeList.innerHTML = nodes.length
      ? nodes
          .slice(0, 80)
          .map((node) => {
            const summary = node.properties?.description || node.properties?.content || node.properties?.text || node.id;
            return `<article class="graph-list-item"><span class="graph-kind">${escapeHtml(node.kind || "node")}</span><div><strong>${escapeHtml(node.label || node.id)}</strong><small>${escapeHtml(summary || "")}</small></div></article>`;
          })
          .join("")
      : `<div class="graph-empty">暂无节点</div>`;
  }
  if (targets.edgeList) {
    const nodeLabels = new Map(nodes.map((node) => [node.id, node.label || node.id]));
    targets.edgeList.innerHTML = edges.length
      ? edges
          .slice(0, 100)
          .map((edge) => {
            const source = nodeLabels.get(edge.source) || edge.source;
            const target = nodeLabels.get(edge.target) || edge.target;
            return `<article class="graph-list-item edge"><span class="graph-kind">${escapeHtml(edge.type || "EDGE")}</span><div><strong>${escapeHtml(source)} -> ${escapeHtml(target)}</strong><small>${escapeHtml(edge.properties?.description || edge.id || "")}</small></div></article>`;
          })
          .join("")
      : `<div class="graph-empty">暂无关系</div>`;
  }
}

function renderScriptGraphMap(nodes, edges) {
  if (!nodes.length) {
    return `<div class="graph-empty">暂无可见图节点</div>`;
  }
  const visibleNodes = nodes.slice(0, 80);
  const visibleIds = new Set(visibleNodes.map((node) => node.id));
  const visibleEdges = edges.filter((edge) => visibleIds.has(edge.source) && visibleIds.has(edge.target)).slice(0, 160);
  const layout = layoutGraphNodes(visibleNodes, visibleEdges, edges);
  const lines = visibleEdges
    .map((edge) => {
      const source = layout.positions.get(edge.source);
      const target = layout.positions.get(edge.target);
      if (!source || !target) return "";
      return `<path class="graph-edge ${isMainlineEdge(edge) ? "graph-edge-mainline" : "graph-edge-branch"}" fill="none" d="${graphEdgePath(source, target)}" />`;
    })
    .join("");
  const nodeMarkup = visibleNodes
    .map((node) => {
      const position = layout.positions.get(node.id);
      const title = escapeHtml(node.label || node.id);
      const kind = escapeHtml(node.kind || "node");
      const degree = layout.degree.get(node.id) || 0;
      const size = graphNodeSize(degree);
      const style = `left:${position.x}%;top:${position.y}%;--node-scale:${size.scale};--node-opacity:${size.opacity}`;
      return `<button class="graph-node kind-${cssSafeKind(node.kind)} graph-node-${size.level}" data-node-id="${escapeAttribute(node.id)}" type="button" title="${kind}: ${title} · ${degree} 条线" style="${style}"><span>${title}</span><em>${kind} · ${degree}</em></button>`;
    })
    .join("");
  const hidden = nodes.length > visibleNodes.length ? `<span class="graph-hidden-count">+${nodes.length - visibleNodes.length} nodes</span>` : "";
  const viewport = state.graphViewport || { scale: 1, x: 0, y: 0 };
  const viewportStyle = `transform:translate(${viewport.x}px, ${viewport.y}px) scale(${viewport.scale});`;
  return `<div class="graph-canvas" style="${viewportStyle}"><svg class="graph-edges" viewBox="0 0 100 100" preserveAspectRatio="none" aria-hidden="true">${lines}</svg>${nodeMarkup}</div>${hidden}`;
}

function scoreGraphNodes(nodes, edges) {
  const degree = new Map(nodes.map((node) => [node.id, 0]));
  const mainlineDegree = new Map(nodes.map((node) => [node.id, 0]));
  for (const edge of edges) {
    degree.set(edge.source, (degree.get(edge.source) || 0) + 1);
    degree.set(edge.target, (degree.get(edge.target) || 0) + 1);
    if (isMainlineEdge(edge)) {
      mainlineDegree.set(edge.source, (mainlineDegree.get(edge.source) || 0) + 1);
      mainlineDegree.set(edge.target, (mainlineDegree.get(edge.target) || 0) + 1);
    }
  }
  return new Map(
    nodes.map((node) => [
      node.id,
      graphNodeBaseWeight(node) + (degree.get(node.id) || 0) * 1.4 + (mainlineDegree.get(node.id) || 0) * 2.6,
    ]),
  );
}

function graphNodeBaseWeight(node) {
  return {
    script: 18,
    chapter: 15,
    event: 11,
    timeline_event: 11,
    task: 10,
    secret: 9,
    character: 9,
    organization: 7,
    clue: 6,
    item: 5,
    location: 4,
    constraint: 3,
  }[node.kind || ""] || 4;
}

function isMainlineEdge(edge) {
  return new Set([
    "NEXT_EVENT",
    "CAUSES",
    "DEPENDS_ON",
    "REVEALS",
    "HAS_EVENT",
    "HAS_TIMELINE_EVENT",
    "HAS_TASK",
    "HAS_TRUTH",
    "HAS_SECRET",
  ]).has(edge.type || "");
}

function graphNodeSize(degree) {
  if (degree >= 10) return { level: "hero", scale: 1.62, opacity: 1 };
  if (degree >= 7) return { level: "hero", scale: 1.42, opacity: 1 };
  if (degree >= 5) return { level: "major", scale: 1.24, opacity: 0.98 };
  if (degree >= 3) return { level: "normal", scale: 1.04, opacity: 0.94 };
  return { level: "minor", scale: 0.86, opacity: 0.82 };
}

function pickVisibleGraphNodes(nodes, edges, weights, limit) {
  const degree = new Map(nodes.map((node) => [node.id, 0]));
  for (const edge of edges) {
    degree.set(edge.source, (degree.get(edge.source) || 0) + 1);
    degree.set(edge.target, (degree.get(edge.target) || 0) + 1);
  }
  return [...nodes]
    .sort((a, b) => {
      if ((a.kind || "") === "script") return -1;
      if ((b.kind || "") === "script") return 1;
      return (weights.get(b.id) || 0) - (weights.get(a.id) || 0) || (degree.get(b.id) || 0) - (degree.get(a.id) || 0);
    })
    .slice(0, limit);
}

function layoutGraphNodes(nodes, edges, allEdges = edges) {
  const degree = graphDegree(nodes, allEdges);
  const positions = new Map();
  const byDegree = (a, b) => (degree.get(b.id) || 0) - (degree.get(a.id) || 0) || String(a.label || a.id).localeCompare(String(b.label || b.id));
  const scriptNodes = nodes.filter((node) => (node.kind || "") === "script");
  const spineNodes = nodes
    .filter((node) => ["chapter", "event", "timeline_event", "task", "secret", "truth", "ending", "rule"].includes(node.kind || ""))
    .sort((a, b) => graphNodeOrder(a) - graphNodeOrder(b) || byDegree(a, b));
  const leftNodes = nodes
    .filter((node) => ["character", "organization"].includes(node.kind || ""))
    .sort(byDegree);
  const rightNodes = nodes
    .filter((node) => !scriptNodes.includes(node) && !spineNodes.includes(node) && !leftNodes.includes(node))
    .sort(byDegree);

  placeGraphColumn(positions, scriptNodes, 50, 10, 10, 0);
  placeGraphColumn(positions, spineNodes, 50, 24, 88, 0);
  placeGraphColumn(positions, leftNodes, 24, 18, 84, -1);
  placeGraphColumn(positions, rightNodes, 76, 18, 84, 1);
  resolveGraphOverlaps(positions, nodes);
  applyGraphNodeOverrides(positions);
  return { positions, degree };
}

function graphNodeOrder(node) {
  const explicit = Number(node.properties?.order);
  if (Number.isFinite(explicit) && explicit > 0) return explicit;
  const match = String(node.id || "").match(/(?:event|task|chapter|secret|rule)[:_ -]*(\d+)/i);
  return match ? Number(match[1]) : 999;
}

function placeGraphColumn(positions, nodes, x, yMin, yMax, side = 0) {
  if (!nodes.length) return;
  const columns = Math.ceil(nodes.length / 7);
  const rows = Math.ceil(nodes.length / columns);
  nodes.forEach((node, index) => {
    const column = Math.floor(index / rows);
    const row = index % rows;
    const y = rows === 1 ? (yMin + yMax) / 2 : yMin + row * ((yMax - yMin) / (rows - 1));
    const xOffset = side ? side * column * 11 : (column - (columns - 1) / 2) * 12;
    const stagger = row % 2 ? side * 3 : 0;
    positions.set(node.id, {
      x: Math.max(7, Math.min(93, x + xOffset + stagger)),
      y: Math.max(7, Math.min(93, y)),
    });
  });
}

function resolveGraphOverlaps(positions, nodes) {
  const ids = nodes.map((node) => node.id);
  for (let pass = 0; pass < 7; pass += 1) {
    for (let i = 0; i < ids.length; i += 1) {
      for (let j = i + 1; j < ids.length; j += 1) {
        const a = positions.get(ids[i]);
        const b = positions.get(ids[j]);
        if (!a || !b) continue;
        const dx = b.x - a.x;
        const dy = b.y - a.y;
        if (Math.abs(dx) >= 9 || Math.abs(dy) >= 8) continue;
        const pushX = dx >= 0 ? 2.4 : -2.4;
        const pushY = dy >= 0 ? 2 : -2;
        b.x = Math.max(7, Math.min(93, b.x + pushX));
        b.y = Math.max(7, Math.min(93, b.y + pushY));
      }
    }
  }
}

function applyGraphNodeOverrides(positions) {
  const overrides = state.graphNodeOverrides || {};
  for (const [nodeId, position] of Object.entries(overrides)) {
    if (positions.has(nodeId)) {
      positions.set(nodeId, {
        x: Math.max(-20, Math.min(120, Number(position.x) || 50)),
        y: Math.max(-20, Math.min(120, Number(position.y) || 50)),
      });
    }
  }
}

function graphDegree(nodes, edges) {
  const degree = new Map(nodes.map((node) => [node.id, 0]));
  for (const edge of edges) {
    degree.set(edge.source, (degree.get(edge.source) || 0) + 1);
    degree.set(edge.target, (degree.get(edge.target) || 0) + 1);
  }
  return degree;
}

function graphEdgePath(source, target) {
  const midY = (source.y + target.y) / 2;
  return `M ${source.x} ${source.y} C ${source.x} ${midY}, ${target.x} ${midY}, ${target.x} ${target.y}`;
}

function bindGraphCanvasInteractions() {
  const map = $("#script-graph-map");
  if (!map || map.dataset.bound === "1") return;
  map.dataset.bound = "1";
  map.addEventListener("click", (event) => {
    if (Date.now() < graphSuppressClickUntil) return;
    if (event.target.closest(".graph-node")) return;
    zoomGraphViewport(1.18, event.clientX, event.clientY);
  });
  map.addEventListener(
    "wheel",
    (event) => {
      event.preventDefault();
      zoomGraphViewport(event.deltaY < 0 ? 1.12 : 0.9, event.clientX, event.clientY);
    },
    { passive: false },
  );
  map.addEventListener("pointerdown", startGraphPointerInteraction);
}

function startGraphPointerInteraction(event) {
  const map = $("#script-graph-map");
  const canvas = map?.querySelector(".graph-canvas");
  if (!map || !canvas || event.button !== 0) return;
  const node = event.target.closest(".graph-node");
  event.preventDefault();
  map.setPointerCapture(event.pointerId);
  const start = {
    x: event.clientX,
    y: event.clientY,
    viewport: { ...(state.graphViewport || { scale: 1, x: 0, y: 0 }) },
    nodeId: node?.dataset?.nodeId || "",
    nodePosition: node ? graphNodePositionFromElement(node) : null,
  };
  const move = (moveEvent) => {
    if (Math.abs(moveEvent.clientX - start.x) + Math.abs(moveEvent.clientY - start.y) > 4) {
      graphSuppressClickUntil = Date.now() + 250;
    }
    if (start.nodeId) {
      const next = clientPointToGraphPercent(map, moveEvent.clientX, moveEvent.clientY);
      const activeNode = map.querySelector(`.graph-node[data-node-id="${cssEscape(start.nodeId)}"]`);
      if (activeNode) {
        activeNode.style.left = `${next.x}%`;
        activeNode.style.top = `${next.y}%`;
      }
      start.nodePosition = next;
      return;
    }
    state.graphViewport = {
      ...start.viewport,
      x: start.viewport.x + moveEvent.clientX - start.x,
      y: start.viewport.y + moveEvent.clientY - start.y,
    };
    applyGraphViewport();
  };
  const up = () => {
    if (start.nodeId && start.nodePosition) {
      state.graphNodeOverrides = {
        ...(state.graphNodeOverrides || {}),
        [start.nodeId]: start.nodePosition,
      };
      renderScriptGraph();
    }
    map.releasePointerCapture(event.pointerId);
    map.removeEventListener("pointermove", move);
    map.removeEventListener("pointerup", up);
    map.removeEventListener("pointercancel", up);
    persistState();
  };
  map.addEventListener("pointermove", move);
  map.addEventListener("pointerup", up);
  map.addEventListener("pointercancel", up);
}

function cssEscape(value) {
  if (window.CSS?.escape) return window.CSS.escape(value);
  return String(value).replace(/["\\]/g, "\\$&");
}

function zoomGraphViewport(factor, clientX, clientY) {
  const map = $("#script-graph-map");
  if (!map) return;
  const viewport = state.graphViewport || { scale: 1, x: 0, y: 0 };
  const rect = map.getBoundingClientRect();
  const nextScale = Math.max(0.55, Math.min(2.8, viewport.scale * factor));
  const focusX = clientX - rect.left;
  const focusY = clientY - rect.top;
  const graphX = (focusX - viewport.x) / viewport.scale;
  const graphY = (focusY - viewport.y) / viewport.scale;
  state.graphViewport = {
    scale: nextScale,
    x: focusX - graphX * nextScale,
    y: focusY - graphY * nextScale,
  };
  applyGraphViewport();
  persistState();
}

function applyGraphViewport() {
  const canvas = $("#script-graph-map")?.querySelector(".graph-canvas");
  if (!canvas) return;
  const viewport = state.graphViewport || { scale: 1, x: 0, y: 0 };
  canvas.style.transform = `translate(${viewport.x}px, ${viewport.y}px) scale(${viewport.scale})`;
}

function resetGraphViewport() {
  state.graphViewport = { scale: 1, x: 0, y: 0 };
  state.graphNodeOverrides = {};
}

function clientPointToGraphPercent(map, clientX, clientY) {
  const rect = map.getBoundingClientRect();
  const viewport = state.graphViewport || { scale: 1, x: 0, y: 0 };
  return {
    x: Math.max(-20, Math.min(120, ((clientX - rect.left - viewport.x) / viewport.scale / rect.width) * 100)),
    y: Math.max(-20, Math.min(120, ((clientY - rect.top - viewport.y) / viewport.scale / rect.height) * 100)),
  };
}

function graphNodePositionFromElement(node) {
  return {
    x: parseFloat(node.style.left) || 50,
    y: parseFloat(node.style.top) || 50,
  };
}

function cssSafeKind(value) {
  return String(value || "node").toLowerCase().replace(/[^a-z0-9_-]+/g, "-");
}

function renderVisualPlan() {
  $("#visual-plan-json").value = state.visualPlan ? pretty(state.visualPlan) : "";
  const container = $("#asset-list");
  const assets = state.visualPlan?.assets || [];
  if (!assets.length) {
    container.innerHTML = `<div class="inspector-item"><strong>暂无资产</strong><small>先生成视觉提示词计划。</small></div>`;
    return;
  }
  container.innerHTML = assets
    .map(
      (asset) => `
        <article class="asset-row">
          <span class="asset-kind">${escapeHtml(asset.kind || "-")}</span>
          <div>
            <strong>${escapeHtml(asset.display_name || asset.id)}</strong>
            <small>${escapeHtml(asset.prompt || "")}</small>
          </div>
        </article>
      `,
    )
    .join("");
}

function renderImages() {
  const result = state.imageResult;
  const resultAssets = [...(result?.generated || []), ...(result?.failed || [])];
  const plannedAssets = state.visualPlan?.assets || [];
  const assets = resultAssets.length ? resultAssets : plannedAssets;
  if (assets.length && !assets.some((asset) => asset.id === state.selectedImageAssetId)) {
    state.selectedImageAssetId = assets[0].id || "";
  }
  $("#image-log").textContent = result
    ? pretty(result)
    : state.visualPlan
      ? pretty(buildImageGenerationInputPreview(plannedAssets))
      : "暂无图片产物";
  const grid = $("#image-grid");
  if (!assets.length) {
    grid.innerHTML = `<div class="inspector-item"><strong>暂无可消费资产</strong><small>先在「视觉提示词」生成或载入视觉计划。</small></div>`;
    return;
  }
  grid.innerHTML = assets
    .map((asset) => {
      const url = outputPathToUrl(asset.output_path);
      const image =
        asset.status === "generated"
          ? `<span class="image-preview"><img src="${escapeAttribute(url)}" alt="${escapeAttribute(asset.display_name || asset.id)}" loading="lazy" /></span>`
          : "";
      const statusLabel = imageAssetStatusLabel(asset.status);
      const placeholder = image ? "" : `<div class="image-placeholder">${escapeHtml(statusLabel)}</div>`;
      const selected = asset.id === state.selectedImageAssetId ? " selected" : "";
      return `
        <button class="image-card${selected}" type="button" data-asset-id="${escapeAttribute(asset.id || "")}" data-image-url="${escapeAttribute(asset.status === "generated" ? url : "")}">
          ${image || placeholder}
          <div>
            <strong>${escapeHtml(asset.display_name || asset.id)}</strong>
            <small>${escapeHtml(statusLabel)} · ${escapeHtml(imageAssetKindLabel(asset.kind))}</small>
            <small>${escapeHtml(asset.output_path || "")}</small>
            <small>${escapeHtml(asset.prompt || "")}</small>
          </div>
        </button>
      `;
    })
    .join("");
  $$("#image-grid .image-card").forEach((card) => {
    card.addEventListener("click", () => {
      state.selectedImageAssetId = card.dataset.assetId || "";
      if (card.dataset.imageUrl) {
        openImageLightbox(card.dataset.imageUrl, assets.find((asset) => asset.id === state.selectedImageAssetId));
      }
      renderImages();
      persistState();
    });
  });
  updateImageCardRatios();
  renderImageEditor(assets);
}

function updateImageCardRatios(rootSelector = "#image-grid") {
  $$(`${rootSelector} .image-card img`).forEach((img) => {
    const applyRatio = () => {
      if (!img.naturalWidth || !img.naturalHeight) return;
      const preview = img.closest(".image-preview");
      if (preview) preview.style.setProperty("--image-aspect", `${img.naturalWidth} / ${img.naturalHeight}`);
    };
    if (img.complete) applyRatio();
    else img.addEventListener("load", applyRatio, { once: true });
  });
}

function openImageLightbox(url, asset = {}) {
  closeImageLightbox();
  const overlay = document.createElement("div");
  overlay.className = "image-lightbox";
  overlay.innerHTML = `
    <button class="image-lightbox-close" type="button" aria-label="关闭大图">×</button>
    <figure>
      <img src="${escapeAttribute(url)}" alt="${escapeAttribute(asset?.display_name || asset?.id || "generated image")}" />
      <figcaption>
        <strong>${escapeHtml(asset?.display_name || asset?.id || "")}</strong>
        <span>${escapeHtml(asset?.output_path || "")}</span>
      </figcaption>
    </figure>
  `;
  overlay.addEventListener("click", (event) => {
    if (event.target === overlay || event.target.closest(".image-lightbox-close")) closeImageLightbox();
  });
  document.body.appendChild(overlay);
  document.addEventListener("keydown", handleImageLightboxKeydown);
}

function closeImageLightbox() {
  const overlay = document.querySelector(".image-lightbox");
  if (overlay) overlay.remove();
  document.removeEventListener("keydown", handleImageLightboxKeydown);
}

function handleImageLightboxKeydown(event) {
  if (event.key === "Escape") closeImageLightbox();
}

function buildImageGenerationInputPreview(plannedAssets) {
  return {
    status: "ready_for_image_generation",
    status_label: "视觉计划已载入，等待图片生成",
    pipeline_contract: "script_graph -> visual_plan -> image_generation",
    provider: {
      provider: $("#image-provider").value || "stepfun",
      model: $("#image-model").value || "",
      size: $("#image-size").value || "",
    },
    visual_plan: state.visualPlan,
    upstream_context: state.visualPlan?.metadata?.upstream_context || {
      source_json: state.visualPlan?.metadata?.style_guide?.source_json_excerpt || null,
      story_graph_context: state.visualPlan?.metadata?.style_guide?.graph_visual_context || "",
    },
    asset_count: plannedAssets.length,
  };
}

function renderImageEditor(assets) {
  const asset = assets.find((item) => item.id === state.selectedImageAssetId);
  const empty = $("#image-editor-empty");
  const form = $("#image-editor-form");
  if (!asset) {
    empty.hidden = false;
    form.hidden = true;
    return;
  }
  empty.hidden = true;
  form.hidden = false;
  $("#edit-image-display-name").value = asset.display_name || "";
  $("#edit-image-kind").value = ["character", "scene", "item", "other"].includes(asset.kind) ? asset.kind : "other";
  $("#edit-image-prompt").value = asset.metadata?.manual_prompt || (asset.prompt?.includes("LOCKED BATCH STYLE") ? "" : asset.prompt || "");
  $("#edit-image-negative-prompt").value = asset.negative_prompt || "";
  $("#edit-image-output-path").value = asset.output_path || "";
}

function imageAssetStatusLabel(status) {
  return {
    planned: "待生成",
    generated: "已生成",
    failed: "失败",
  }[status || "planned"] || status || "待生成";
}

function imageAssetKindLabel(kind) {
  return {
    character: "人物",
    scene: "场景",
    item: "道具",
    other: "其他",
  }[kind || ""] || kind || "资产";
}

function applyImageAssetEdit() {
  if (!state.visualPlan?.assets?.length || !state.selectedImageAssetId) {
    setStatus("请先选择一个可编辑的视觉资产", true);
    return;
  }
  let updated = false;
  const assets = state.visualPlan.assets.map((asset) => {
    if (asset.id !== state.selectedImageAssetId) return asset;
    updated = true;
    const manualPrompt = $("#edit-image-prompt").value.trim();
    return {
      ...asset,
      display_name: $("#edit-image-display-name").value.trim() || asset.display_name,
      kind: $("#edit-image-kind").value || asset.kind,
      negative_prompt: $("#edit-image-negative-prompt").value.trim(),
      output_path: $("#edit-image-output-path").value.trim() || asset.output_path,
      status: asset.status === "generated" ? "planned" : asset.status,
      metadata: {
        ...(asset.metadata || {}),
        manual_prompt: manualPrompt,
      },
    };
  });
  if (!updated) {
    setStatus("未找到选中的视觉资产", true);
    return;
  }
  state.visualPlan = { ...state.visualPlan, assets };
  state.imageResult = null;
  setStage("images", "dirty");
  persistState();
  renderAll();
  setStatus("已应用修改；可继续编辑或开始生成图片");
  appendRunLog("done", "图片资产编辑已应用", `资产：${state.selectedImageAssetId}`);
}

function renderWorld() {
  const worldEditor = $("#world-json");
  worldEditor.value = state.world ? pretty(state.world) : "";
  worldEditor.hidden = !state.world;
  renderWorldSourceSummary();
  renderWorldAssetRunSummary();
  renderWorldAssetGrid();
  const container = $("#world-inspector");
  const source = resolveWorldGenerationSource();
  if (!state.world && source) {
    const visualContext = worldVisualContext(source);
    const pendingItems = [
      ["待消费 JSON", source.label],
      ["标题", source.title || "-"],
      ["节点", String(source.nodeCount ?? "-")],
      ["关系", String(source.edgeCount ?? "-")],
      ["图片资产", visualContext.label],
      ["来源", source.reason || "-"],
    ];
    container.innerHTML = pendingItems
      .map(([title, value]) => `<div class="inspector-item"><strong>${escapeHtml(title)}</strong><small>${escapeHtml(value)}</small></div>`)
      .join("");
    return;
  }
  if (!state.world) {
    container.innerHTML = `<div class="inspector-item"><strong>暂无世界</strong><small>拆解成功后会得到可运行世界，也可以用世界 API 重新生成。</small></div>`;
    return;
  }
  const world = state.world;
  const items = [
    ["世界 ID", world.world_id || "-"],
    ["名称", world.name || "-"],
    ["NPC", summarizeNames(world.npcs, "name") || "0"],
    ["任务", summarizeNames(world.tasks, "title") || "0"],
    ["动作", summarizeNames(world.actions, "label") || "0"],
  ];
  container.innerHTML = items
    .map(([title, value]) => `<div class="inspector-item"><strong>${escapeHtml(title)}</strong><small>${escapeHtml(value)}</small></div>`)
    .join("");
}

function renderWorldSourceSummary() {
  const container = $("#world-source-summary");
  if (!container) return;
  const source = resolveWorldGenerationSource();
  if (!source) {
    container.innerHTML = `<div class="source-summary-empty">未选择可消费 JSON。请选择故事图谱来源。</div>`;
    return;
  }
  const visualContext = worldVisualContext(source);
  const sourceTime = worldSourceTimeLabel(source);
  container.innerHTML = `
    <div class="source-summary-item source-summary-main">
      <small>将消费的故事图谱</small>
      <strong>${escapeHtml(source.title || "未命名图谱")}</strong>
      <span>${escapeHtml(source.nodeCount)} 节点 · ${escapeHtml(source.edgeCount)} 关系 · ${escapeHtml(source.label)}${sourceTime ? ` · ${escapeHtml(sourceTime)}` : ""}</span>
    </div>
    <div class="source-summary-item ${visualContext.hasVisual ? "source-summary-ready" : "source-summary-muted"}">
      <small>图片带入状态</small>
      <strong>${escapeHtml(visualContext.title)}</strong>
      <span>${escapeHtml(visualContext.detail)}</span>
    </div>
  `;
}

function renderWorldVisualAssetRunPicker() {
  const picker = $("#world-visual-asset-run-picker");
  if (!picker) return;
  const runs = state.visualAssetRuns || [];
  const options = runs
    .map((run) => {
      const label = `${formatFullDateTime(run.updated_at || run.created_at) || run.run_id} · ${run.asset_count || 0} 张 · ${run.run_id}`;
      return `<option value="${escapeAttribute(run.run_id)}">${escapeHtml(label)}</option>`;
    })
    .join("");
  const previous = picker.value || state.selectedVisualAssetRunId || state.imageResult?.metadata?.generation_run_id || runs[0]?.run_id || "";
  picker.innerHTML = `<option value="">不指定图片批次</option>${options}`;
  if (previous) picker.value = previous;
  renderWorldAssetRunSummary();
  renderWorldAssetGrid();
  renderWorldSourceSummary();
}

function renderWorldAssetRunSummary() {
  const container = $("#world-asset-run-summary");
  if (!container) return;
  const selectedRunId = $("#world-visual-asset-run-picker")?.value || "";
  const selectedRun = selectedRunId ? (state.visualAssetRuns || []).find((run) => run.run_id === selectedRunId) : null;
  const loadedRunId = state.imageResult?.metadata?.generation_run_id || "";
  const isLoaded = Boolean(selectedRunId && loadedRunId === selectedRunId);
  if (!selectedRunId) {
    container.innerHTML = `<div class="asset-run-summary-empty">未指定图片资产批次；世界生成只会带入视觉计划，不会绑定某次已生成图片。</div>`;
    return;
  }
  if (!selectedRun) {
    container.innerHTML = `<div class="asset-run-summary-empty">当前选择的图片批次不在列表中，请刷新批次列表。</div>`;
    return;
  }
  const assets = isLoaded ? state.imageResult?.generated || [] : selectedRun.assets || [];
  const kinds = summarizeAssetKinds(assets);
  const preview = assets
    .slice(0, 6)
    .map((asset) => asset.display_name || asset.source_name || asset.id || String(asset).split(/[\\/]/).pop())
    .filter(Boolean)
    .join("、");
  container.innerHTML = `
    <div class="asset-run-summary-card ${isLoaded ? "is-loaded" : ""}">
      <div>
        <small>${isLoaded ? "已载入图片资产批次" : "已选择，生成时会自动载入"}</small>
        <strong>${escapeHtml(formatShortDateTime(selectedRun.updated_at || selectedRun.created_at) || selectedRun.run_id)}</strong>
        <span>${escapeHtml(selectedRun.asset_count || 0)} 张 · ${escapeHtml(kinds || "类型待载入")} · ${escapeHtml(selectedRun.run_id)}</span>
      </div>
      <p>${escapeHtml(selectedRun.path || "")}</p>
      ${preview ? `<p>预览：${escapeHtml(preview)}</p>` : ""}
    </div>
  `;
}

function renderWorldAssetGrid() {
  const grid = $("#world-asset-grid");
  if (!grid) return;
  const preview = resolveWorldAssetPreview();
  if (!preview.assets.length) {
    grid.innerHTML = `<div class="inspector-item world-asset-empty"><strong>暂无图片资产</strong><small>选择图片资产批次后，这里会直接显示世界生成将带入的图片。</small></div>`;
    return;
  }
  grid.innerHTML = preview.assets
    .map((asset) => {
      const url = outputPathToUrl(asset.output_path);
      const image = url
        ? `<span class="image-preview"><img src="${escapeAttribute(url)}" alt="${escapeAttribute(asset.display_name || asset.id || "image asset")}" loading="lazy" /></span>`
        : "";
      const statusLabel = imageAssetStatusLabel(asset.status || "generated");
      const selected = asset.id === state.selectedImageAssetId ? " selected" : "";
      return `
        <button class="image-card${selected}" type="button" data-asset-id="${escapeAttribute(asset.id || "")}" data-image-url="${escapeAttribute(url)}">
          ${image || `<div class="image-placeholder">${escapeHtml(statusLabel)}</div>`}
          <div>
            <strong>${escapeHtml(asset.display_name || asset.id || "图片资产")}</strong>
            <small>${escapeHtml(statusLabel)} · ${escapeHtml(imageAssetKindLabel(asset.kind))}</small>
            <small>${escapeHtml(asset.output_path || "")}</small>
          </div>
        </button>
      `;
    })
    .join("");
  $$("#world-asset-grid .image-card").forEach((card) => {
    card.addEventListener("click", () => {
      state.selectedImageAssetId = card.dataset.assetId || "";
      if (card.dataset.imageUrl) {
        openImageLightbox(card.dataset.imageUrl, preview.assets.find((asset) => asset.id === state.selectedImageAssetId));
      }
      renderWorldAssetGrid();
      persistState();
    });
  });
  updateImageCardRatios("#world-asset-grid");
}

function resolveWorldAssetPreview() {
  const selectedRunId = $("#world-visual-asset-run-picker")?.value || "";
  const loadedRunId = state.imageResult?.metadata?.generation_run_id || "";
  if (selectedRunId && loadedRunId === selectedRunId) {
    return { runId: selectedRunId, assets: normalizeWorldPreviewAssets(state.imageResult?.generated || []) };
  }
  const selectedRun = selectedRunId ? (state.visualAssetRuns || []).find((run) => run.run_id === selectedRunId) : null;
  if (selectedRun?.assets?.length) {
    return { runId: selectedRunId, assets: normalizeWorldPreviewAssets(selectedRun.assets) };
  }
  const plan = currentWorldVisualPlan();
  if (plan?.assets?.length) {
    return { runId: "", assets: normalizeWorldPreviewAssets(plan.assets, { planned: true }) };
  }
  return { runId: "", assets: [] };
}

function normalizeWorldPreviewAssets(assets = [], options = {}) {
  return assets
    .map((asset, index) => {
      if (typeof asset === "string") return assetFromOutputPath(asset, index);
      const outputPath = asset.output_path || asset.path || "";
      return {
        ...asset,
        id: asset.id || asset.asset_id || asset.source_id || pathStem(outputPath) || `asset-${index + 1}`,
        display_name: asset.display_name || asset.name || asset.source_name || pathStem(outputPath) || `图片资产 ${index + 1}`,
        kind: asset.kind || pathParentName(outputPath) || "asset",
        output_path: outputPath,
        status: outputPath ? "generated" : asset.status || "planned",
      };
    })
    .filter((asset) => asset.id || asset.output_path);
}

function assetFromOutputPath(outputPath, index = 0) {
  const name = pathStem(outputPath) || `图片资产 ${index + 1}`;
  return {
    id: `${name}-${index}`,
    display_name: name,
    kind: pathParentName(outputPath) || "asset",
    output_path: outputPath,
    status: "generated",
  };
}

function pathStem(path) {
  const filename = String(path || "").split(/[\\/]/).pop() || "";
  return filename.replace(/\.[^.]+$/, "");
}

function pathParentName(path) {
  const parts = String(path || "").split(/[\\/]/).filter(Boolean);
  return parts.length > 1 ? parts[parts.length - 2] : "";
}

function summarizeAssetKinds(assets = []) {
  const counts = {};
  for (const asset of assets) {
    const kind = typeof asset === "string" ? asset.split(/[\\/]/).slice(-2, -1)[0] || "asset" : asset.kind || "asset";
    counts[kind] = (counts[kind] || 0) + 1;
  }
  return Object.entries(counts)
    .map(([kind, count]) => `${kind} ${count}`)
    .join(" · ");
}

function renderLorebook() {
  const editor = $("#lorebook-json");
  const inspector = $("#lorebook-inspector");
  if (!editor || !inspector) return;
  const world = state.world;
  const hasVersionChoices = renderLorebookVersionPicker(world);
  const previewVersion = getSelectedLorebookVersion(world);
  const activeVersion = getActiveLorebookVersion(world);
  const lorebook = previewVersion?.artifact || getWorldLorebook(world);
  const isPreviewActive = !previewVersion || Boolean(previewVersion.is_active) || previewVersion.version_id === activeVersion?.version_id;
  editor.value = lorebook ? pretty(lorebook) : "";
  renderLorebookSourceSummary(world, lorebook, previewVersion, activeVersion);
  const selectButton = $("#select-lorebook-version");
  if (selectButton) {
    selectButton.disabled = !previewVersion || isPreviewActive;
    selectButton.textContent = isPreviewActive ? "当前已使用" : "使用此版本";
  }
  if (!world) {
    inspector.innerHTML = `<div class="inspector-item"><strong>暂无世界</strong><small>先完成「世界生成」；世界书由 NpcLorebookCreationAgent 在该阶段产出。</small></div>`;
    return;
  }
  if (!lorebook) {
    inspector.innerHTML = `<div class="inspector-item"><strong>未找到世界书</strong><small>当前世界 JSON 没有 metadata.npc_lorebook。请重新运行世界生成；正式流程不应靠 fallback 隐式补齐。</small></div>`;
    return;
  }
  const entries = Array.isArray(lorebook.entries) ? lorebook.entries : [];
  const generation = world.metadata?.npc_lorebook_generation || {};
  const review = lorebook.metadata?.review || generation.review || {};
  const generatedAt = formatFullDateTime(previewVersion?.created_at || lorebook?.metadata?.created_at || lorebookGeneratedTime(world));
  const items = [
    ["Artifact", lorebook.artifact_id || "-"],
    ...(hasVersionChoices ? [["Version", previewVersion?.version_id || "active"], ["当前消费", isPreviewActive ? "是" : "否，当前仅预览"]] : []),
    ["World", lorebook.world_id || world.world_id || "-"],
    ["创建者", lorebook.metadata?.created_by || generation.agent || "-"],
    ["生成时间", generatedAt || "-"],
    ["条目总数", String(entries.length)],
    ["条目类型", summarizeLorebookEntryTypes(entries) || "-"],
    ["校验", typeof review.passed === "boolean" ? (review.passed ? "通过" : "未通过") : "-"],
    ["错误", summarizeReviewMessages(review.errors || review.issues, "error") || "无"],
    ["警告", summarizeReviewMessages(review.warnings || review.issues, "warning") || "无"],
  ];
  inspector.innerHTML = items
    .map(([title, value]) => `<div class="inspector-item"><strong>${escapeHtml(title)}</strong><small>${escapeHtml(value)}</small></div>`)
    .join("");
}

function renderLorebookSourceSummary(world, lorebook, previewVersion = null, activeVersion = null) {
  const container = $("#lorebook-source-summary");
  if (!container) return;
  if (!world) {
    container.innerHTML = `<div class="source-summary-empty">未选择世界。世界书在世界生成完成后出现，并被 NPC 对话与试玩运行时消费。</div>`;
    return;
  }
  const generation = world.metadata?.npc_lorebook_generation || {};
  const entries = Array.isArray(lorebook?.entries) ? lorebook.entries : [];
  const generatedAt = formatFullDateTime(previewVersion?.created_at || lorebook?.metadata?.created_at || lorebookGeneratedTime(world));
  const versions = getWorldLorebookVersions(world);
  const versionText = previewVersion
    ? ` · ${previewVersion.version_id}${previewVersion.is_active || previewVersion.version_id === activeVersion?.version_id ? " · 当前消费" : " · 仅预览"}`
    : "";
  const versionSummary = versions.length
    ? `
    <div class="source-summary-item source-summary-ready">
      <small>可选版本</small>
      <strong>${escapeHtml(versions.length)} 个世界书版本</strong>
      <span>每次生成都会保留为一个可消费 artifact；点击“使用此版本”后才切换 NPC Runtime 当前消费版本。</span>
    </div>`
    : "";
  container.innerHTML = `
    <div class="source-summary-item ${lorebook ? "source-summary-ready" : "source-summary-muted"}">
      <small>NpcLorebookCreationAgent 输出</small>
      <strong>${escapeHtml(lorebook ? lorebook.title || lorebook.artifact_id || "世界书已生成" : "缺少世界书 artifact")}</strong>
      <span>${escapeHtml(entries.length)} 条目 · ${escapeHtml(generation.agent || lorebook?.metadata?.created_by || "-")}${generatedAt ? ` · ${escapeHtml(generatedAt)}` : ""}${escapeHtml(versionText)}</span>
    </div>
    ${versionSummary}
  `;
}

function renderLorebookVersionPicker(world = state.world) {
  const picker = $("#lorebook-version-picker");
  const controls = $("#lorebook-version-controls");
  if (!picker) return;
  const versions = getWorldLorebookVersions(world);
  if (!world) {
    if (controls) controls.hidden = true;
    picker.innerHTML = `<option value="">未选择世界</option>`;
    picker.disabled = true;
    state.selectedLorebookVersionId = "";
    return false;
  }
  if (!versions.length) {
    if (controls) controls.hidden = true;
    picker.innerHTML = `<option value="">暂无世界书版本</option>`;
    picker.disabled = true;
    state.selectedLorebookVersionId = "";
    return false;
  }
  if (controls) controls.hidden = false;
  const active = getActiveLorebookVersion(world);
  const selected =
    state.selectedLorebookVersionId && versions.some((version) => version.version_id === state.selectedLorebookVersionId)
      ? state.selectedLorebookVersionId
      : active?.version_id || versions[0]?.version_id || "";
  picker.disabled = false;
  picker.innerHTML = versions
    .map((version, index) => {
      const createdAt = formatFullDateTime(version.created_at) || `版本 ${index + 1}`;
      const status = version.is_active || version.version_id === active?.version_id ? "当前" : "历史";
      const title = version.title || version.artifact_id || version.version_id;
      const count = version.entry_count ?? version.artifact?.entries?.length ?? 0;
      const label = `${status} · ${createdAt} · ${count} 条 · ${title}`;
      return `<option value="${escapeAttribute(version.version_id)}">${escapeHtml(label)}</option>`;
    })
    .join("");
  picker.value = selected;
  state.selectedLorebookVersionId = selected;
  return true;
}

function getWorldLorebook(world = state.world) {
  const raw = world?.metadata?.npc_lorebook;
  return raw && typeof raw === "object" ? raw : null;
}

function getWorldLorebookVersions(world = state.world) {
  const versions = world?.metadata?.npc_lorebook_versions;
  return Array.isArray(versions)
    ? versions.filter((version) => version && typeof version === "object" && version.artifact && version.version_id)
    : [];
}

function getActiveLorebookVersion(world = state.world) {
  const versions = getWorldLorebookVersions(world);
  return versions.find((version) => version.is_active) || null;
}

function getSelectedLorebookVersion(world = state.world) {
  const versions = getWorldLorebookVersions(world);
  if (!versions.length) return null;
  return versions.find((version) => version.version_id === state.selectedLorebookVersionId) || getActiveLorebookVersion(world) || versions[0] || null;
}

function hasWorldLorebook(world = state.world) {
  return Boolean(getWorldLorebook(world));
}

function setLorebookStageForWorld(world = state.world) {
  setStage("lorebook", hasWorldLorebook(world) ? "done" : "error");
}

function lorebookGeneratedTime(world = state.world) {
  return world?.metadata?.npc_lorebook_generation?.created_at || world?.metadata?.npc_lorebook?.metadata?.created_at || "";
}

function lorebookRuntimeSummary(world = state.world) {
  const lorebook = getWorldLorebook(world);
  const entries = Array.isArray(lorebook?.entries) ? lorebook.entries : [];
  const generation = world?.metadata?.npc_lorebook_generation || {};
  const generatedAt = formatFullDateTime(lorebookGeneratedTime(world));
  return {
    lorebook,
    entries,
    generatedAt,
    createdBy: generation.created_by || lorebook?.metadata?.created_by || "",
    fallbackUsed: Boolean(generation.fallback_used || lorebook?.metadata?.creation_agent_failed),
  };
}

function formatLorebookRuntimeStatus(world = state.world) {
  const summary = lorebookRuntimeSummary(world);
  if (!summary.lorebook) {
    return "世界书：未生成；NPC 仍可读取世界 JSON，运行时会使用基础 fallback。";
  }
  const parts = [`世界书：已接入 ${summary.entries.length} 条`];
  if (summary.generatedAt) parts.push(`生成时间：${summary.generatedAt}`);
  if (summary.createdBy) parts.push(`来源：${summary.createdBy}`);
  if (summary.fallbackUsed) parts.push("fallback");
  return parts.join("\n");
}

function summarizeActiveLorebookEntries(data) {
  const active = data?.debug_trace?.lorebook?.active_entries;
  if (!Array.isArray(active) || !active.length) return "本轮未激活世界书条目";
  return active
    .slice(0, 8)
    .map((entry) => entry?.title || entry?.id || "")
    .filter(Boolean)
    .join("、") || "本轮未激活世界书条目";
}

function summarizeLorebookEntryTypes(entries = []) {
  return summarizeCounts(
    entries.reduce((counts, entry) => {
      const type = entry?.entry_type || "other";
      counts[type] = (counts[type] || 0) + 1;
      return counts;
    }, {}),
  );
}

function summarizeReviewMessages(value, severity = "") {
  if (!Array.isArray(value)) return "";
  return value
    .filter((item) => !severity || !item?.severity || item.severity === severity)
    .map((item) => item?.message || item?.detail || item?.path || String(item || ""))
    .filter(Boolean)
    .join("\n");
}

function resolveWorldGenerationSource() {
  const editedGraph = parseOptionalJsonEditor("#script-graph-json");
  if (isScriptGraphDocument(editedGraph)) {
    return worldSourceFromGraph(editedGraph, "当前故事图谱编辑器", "script_graph_editor");
  }
  if (isScriptGraphDocument(state.scriptGraph)) {
    return worldSourceFromGraph(state.scriptGraph, "当前故事图谱", "state.scriptGraph");
  }

  const visualSource =
    state.visualPlan?.metadata?.upstream_context?.source_json || state.visualPlan?.metadata?.style_guide?.source_json_excerpt;
  const visualGraph = extractScriptGraphFromSourceJson(visualSource);
  if (isScriptGraphDocument(visualGraph)) {
    return worldSourceFromGraph(visualGraph, "视觉计划上游 JSON", "visual_plan.upstream_context.source_json");
  }

  const editedDecomposition = parseOptionalJsonEditor("#decomposition-json");
  if (isScriptDecompositionDocument(editedDecomposition)) {
    return worldSourceFromDecomposition(editedDecomposition, "当前剧本理解编辑器", "decomposition_editor");
  }
  if (isScriptDecompositionDocument(state.decomposition)) {
    return worldSourceFromDecomposition(state.decomposition, "当前剧本理解", "state.decomposition");
  }
  if (isScriptDecompositionDocument(visualSource)) {
    return worldSourceFromDecomposition(visualSource, "视觉计划上游 JSON", "visual_plan.upstream_context.source_json");
  }
  return null;
}

function findVisualPlanArtifactForSource(source) {
  if (!source) return null;
  const title = source.title || "";
  const graphId = source.graph?.graph_id || "";
  const candidates = state.visualAssetArtifacts || [];
  return (
    candidates.find((artifact) => artifact.title && artifact.title === title) ||
    candidates.find((artifact) => artifact.world_id && artifact.world_id === graphId) ||
    candidates.find((artifact) => artifact.plan_id && graphId && String(artifact.plan_id).includes(graphId.replace(":", "_"))) ||
    null
  );
}

async function ensureVisualPlanForWorldSource(source) {
  if (!source || source.kind !== "script_graph") return null;
  if (state.visualPlan && visualPlanMatchesWorldSource(state.visualPlan, source)) return state.visualPlan;
  if (!Array.isArray(state.visualAssetArtifacts) || state.visualAssetArtifacts.length === 0) {
    await loadVisualAssetArtifacts();
  }
  const artifact = findVisualPlanArtifactForSource(source);
  if (!artifact?.artifact_id) return null;
  const data = await requestJson(`/api/worlds/visual-assets/${encodeURIComponent(artifact.artifact_id)}`);
  state.visualAssetArtifactId = artifact.artifact_id;
  state.visualPlan = data.plan || null;
  if (state.visualPlan) {
    setStage("prompts", "done");
    await loadVisualAssetRuns();
    renderWorldVisualAssetRunPicker();
  }
  return state.visualPlan;
}

async function ensureVisualResultForWorldSource(source, visualPlan) {
  if (!visualPlan) return null;
  const selectedRunId = $("#world-visual-asset-run-picker")?.value || "";
  if (!selectedRunId) {
    return visualPlan && state.imageResult?.plan?.plan_id === visualPlan.plan_id ? state.imageResult : null;
  }
  if (
    state.imageResult?.metadata?.generation_run_id === selectedRunId &&
    state.imageResult?.plan?.plan_id === visualPlan.plan_id
  ) {
    return state.imageResult;
  }
  const run = await loadVisualAssetRunById(selectedRunId);
  return run ? imageResultFromVisualAssetRun(run) : null;
}

function visualPlanMatchesWorldSource(plan, source) {
  if (!plan || !source) return false;
  if (plan.title && source.title && plan.title === source.title) return true;
  const sourceGraphId = source.graph?.graph_id || "";
  const planGraph = extractScriptGraphFromSourceJson(plan.metadata?.upstream_context?.source_json || plan.metadata?.style_guide?.source_json_excerpt);
  return Boolean(sourceGraphId && planGraph?.graph_id === sourceGraphId);
}

function currentWorldVisualPlan(source = resolveWorldGenerationSource()) {
  return visualPlanMatchesWorldSource(state.visualPlan, source) ? state.visualPlan : null;
}

function worldVisualContext(source = resolveWorldGenerationSource()) {
  const plan = currentWorldVisualPlan(source);
  const result = plan && state.imageResult?.plan?.plan_id === plan.plan_id ? state.imageResult : null;
  const selectedRunId = $("#world-visual-asset-run-picker")?.value || "";
  const selectedRun = selectedRunId ? (state.visualAssetRuns || []).find((run) => run.run_id === selectedRunId) : null;
  if (plan) {
    const count = plan.assets?.length || 0;
    const generatedCount = result?.generated?.length || 0;
    const runId = result?.metadata?.generation_run_id || "";
    if (selectedRun && !result) {
      return {
        hasVisual: true,
        title: `将带入 ${selectedRun.asset_count || 0} 张已生成图片`,
        detail: `${selectedRun.run_id} · 点击生成时会载入该批次`,
        label: `${selectedRun.asset_count || 0} 张 · ${selectedRun.run_id}`,
      };
    }
    return {
      hasVisual: true,
      title: result ? `将带入 ${generatedCount} 张已生成图片` : `将带入 ${count} 个计划资产`,
      detail: result ? `${runId || "已选图片批次"} · ${plan.plan_id || plan.title || "当前视觉计划"}` : `${plan.plan_id || plan.title || "当前视觉计划"} · 可在下方选择具体图片批次`,
      label: result ? `${generatedCount} 张 · ${runId || "已选图片批次"}` : `${count} 个计划资产 · 未指定图片批次`,
    };
  }
  const artifact = findVisualPlanArtifactForSource(source);
  if (artifact) {
    const count = artifact.asset_count || 0;
    const name = artifact.plan_id || artifact.title || artifact.artifact_id || "已保存视觉计划";
    return {
      hasVisual: true,
      title: `将自动载入 ${count} 个图片资产`,
      detail: `${name} · 生成时会随故事图谱一起发送`,
      label: `${count} 个 · ${name}`,
    };
  }
  return {
    hasVisual: false,
    title: "不带图片",
    detail: "未发现同标题或同 world_id 的视觉计划；本次只消费故事图谱",
    label: "未匹配；只消费故事图谱",
  };
}

function worldSourceFromGraph(graph, label, reason) {
  return {
    kind: "script_graph",
    label,
    reason,
    title: graph.title || graph.graph_id || "",
    nodeCount: Array.isArray(graph.nodes) ? graph.nodes.length : 0,
    edgeCount: Array.isArray(graph.edges) ? graph.edges.length : 0,
    graph,
  };
}

function worldSourceFromDecomposition(decomposition, label, reason) {
  const storyGraph = decomposition.story_graph || {};
  return {
    kind: "script_decomposition",
    label,
    reason,
    title: decomposition.title || decomposition.script_id || "",
    nodeCount: Array.isArray(storyGraph.entities) ? storyGraph.entities.length : decomposition.characters?.length || 0,
    edgeCount: Array.isArray(storyGraph.relations) ? storyGraph.relations.length : 0,
    decomposition,
  };
}

function extractScriptGraphFromSourceJson(sourceJson) {
  if (!sourceJson || typeof sourceJson !== "object") return null;
  if (isScriptGraphDocument(sourceJson)) return sourceJson;
  const nestedGraph = sourceJson.script_graph;
  if (isScriptGraphDocument(nestedGraph)) return nestedGraph;
  if (isScriptGraphDocument(nestedGraph?.artifact)) return nestedGraph.artifact;
  return null;
}

function isScriptGraphDocument(value) {
  return Boolean(value && typeof value === "object" && Array.isArray(value.nodes) && Array.isArray(value.edges));
}

function isScriptDecompositionDocument(value) {
  return Boolean(
    value &&
      typeof value === "object" &&
      (Array.isArray(value.characters) || Array.isArray(value.locations) || value.story_graph || value.core_plot || value.public_background),
  );
}

function parseOptionalJsonEditor(selector) {
  const editor = $(selector);
  const raw = editor?.value?.trim() || "";
  if (!raw) return null;
  try {
    return JSON.parse(raw);
  } catch {
    return null;
  }
}

function renderPlaytest() {
  renderPlaytestControls();
  renderPlaytestStatus();
  renderPlaytestLog();
  $("#playtest-session-json").value = state.playtestSnapshot ? pretty(state.playtestSnapshot) : "";
}

function renderPlaytestControls() {
  const locations = collectWorldLocations();
  $("#playtest-location").innerHTML = locations.length
    ? locations.map((location) => `<option value="${escapeAttribute(location)}">${escapeHtml(location)}</option>`).join("")
    : `<option value="">无地点</option>`;
  const actions = state.world?.actions || [];
  $("#playtest-action").innerHTML = actions.length
    ? actions.map((action) => `<option value="${escapeAttribute(action.id)}">${escapeHtml(action.label || action.id)}</option>`).join("")
    : `<option value="">无配置动作</option>`;
  renderPlaytestNpcPicker();
  renderPlaytestPlayerNameHint();
}

function renderPlaytestNpcPicker() {
  const picker = $("#playtest-target-npc");
  if (!picker) return;
  const previous = picker.value;
  const options = collectPlaytestNpcOptions();
  const autoLabel = state.playtestSnapshot?.nearby_npcs?.length ? "自动选择附近 NPC" : "自动选择 NPC";
  picker.innerHTML = `<option value="">${escapeHtml(autoLabel)}</option>${options
    .map((npc) => {
      const tag = npc.source === "nearby" ? "附近" : "世界";
      const location = npc.location ? ` · ${npc.location}` : "";
      return `<option value="${escapeAttribute(npc.id)}">${escapeHtml(`${npc.name || npc.id} · ${tag}${location}`)}</option>`;
    })
    .join("")}`;
  const hasPrevious = previous && options.some((npc) => npc.id === previous);
  const firstNearby = options.find((npc) => npc.source === "nearby")?.id || "";
  picker.value = hasPrevious ? previous : firstNearby;
}

function renderPlaytestPlayerNameHint() {
  const input = $("#playtest-player-name");
  if (!input) return;
  const name = state.playtestSnapshot?.player?.name || state.world?.player?.name || $("#script-player")?.value || state.decomposition?.player_name || "玩家";
  input.placeholder = `默认：${name}`;
}

function renderPlaytestStatus() {
  const container = $("#playtest-status");
  const snapshot = state.playtestSnapshot;
  const world = state.world;
  if (!world && !snapshot) {
    container.innerHTML = `<div class="inspector-item"><strong>暂无世界</strong><small>先在「世界生成」保存或载入世界，再启动试玩。</small></div>`;
    return;
  }
  const sessionState = snapshot?.state || {};
  const player = snapshot?.player || world?.player || {};
  const tasks = sessionState.tasks || world?.tasks || [];
  const done = tasks.filter((task) => task.status === "done").length;
  const total = tasks.length;
  const currentLocation = player.location || "-";
  const nearby = snapshot?.nearby_npcs || [];
  const items = [
    ["闭环进度", total ? `${done}/${total} 任务完成` : "无任务"],
    ["当前位置", currentLocation],
    ["附近 NPC", summarizeNames(nearby, "name") || "无"],
    ["玩家状态", pretty(player)],
    ["任务列表", tasks.map((task) => `${task.status || "pending"} · ${task.title || task.id}`).join("\n") || "无"],
    ["建议动作", (snapshot?.suggested_actions || []).join("\n") || "无"],
  ];
  container.innerHTML = items
    .map(([title, value]) => `<div class="inspector-item"><strong>${escapeHtml(title)}</strong><small>${escapeHtml(value)}</small></div>`)
    .join("");
}

function renderPlaytestLog() {
  const log = $("#playtest-log");
  log.innerHTML = (state.playtestLog || [])
    .map((item) => `<article class="message"><strong>${escapeHtml(item.title)}</strong><p>${escapeHtml(item.text)}</p></article>`)
    .join("");
  log.scrollTop = log.scrollHeight;
}

function renderNpcControls() {
  renderNpcLorebookVersionPicker();
  renderNpcSourceSummary();
  renderNpcLocationPicker();
  const targetPicker = $("#target-npc");
  const previousTarget = targetPicker?.value || "";
  const location = $("#chat-location")?.value || state.world?.player?.location || "";
  const npcs = (state.world?.npcs || []).filter((npc) => location && npcMatchesLocation(npc, location));
  targetPicker.innerHTML = `<option value="">自动群聊（一轮）</option>${npcs
    .map((npc) => {
      const label = npcLocationLabel(npc);
      const text = label ? `${npc.name || npc.id} · ${label}` : npc.name || npc.id;
      return `<option value="${escapeAttribute(npc.id || "")}">${escapeHtml(text)}</option>`;
    })
    .join("")}`;
  targetPicker.value = npcs.some((npc) => String(npc.id || "") === previousTarget) ? previousTarget : "";
  renderNpcRuntimeContext();
}

function syncNpcRuntimeContextFromTarget() {
  const target = $("#target-npc")?.value || "";
  if (target) state.selectedNpcRuntimeId = target;
  renderNpcRuntimeContext();
  persistState();
}

function renderNpcRuntimeContext() {
  const picker = $("#npc-runtime-state-picker");
  const container = $("#npc-runtime-context");
  if (!picker || !container) return;
  const sessions = state.npcRuntimeSnapshot?.state?.npc_sessions || {};
  const npcs = state.world?.npcs || [];
  const options = npcs
    .map((npc) => ({ id: String(npc.id || ""), name: npc.name || npc.id || "NPC" }))
    .filter((npc) => npc.id);
  const preferred = state.selectedNpcRuntimeId || $("#target-npc")?.value || options[0]?.id || "";
  const selected = options.some((npc) => npc.id === preferred) ? preferred : options[0]?.id || "";
  picker.innerHTML = options.length
    ? options.map((npc) => `<option value="${escapeAttribute(npc.id)}">${escapeHtml(npc.name)}</option>`).join("")
    : `<option value="">暂无 NPC</option>`;
  picker.value = selected;
  picker.disabled = !options.length;
  state.selectedNpcRuntimeId = selected;
  const session = sessions[selected];
  if (!state.world) {
    container.innerHTML = `<div class="source-summary-empty">先载入世界，再查看角色运行上下文。</div>`;
    return;
  }
  if (!session) {
    container.innerHTML = `<div class="source-summary-empty">点击“启动世界”或“刷新上下文”后查看该 NPC 实际消费的 Director、记忆和 Review。</div>`;
    return;
  }
  const director = session.turn_plan || {};
  const review = session.conversation_review || {};
  const working = session.working_memory || {};
  const capsule = Array.isArray(session.memory_capsule) ? session.memory_capsule : [];
  const summaries = Array.isArray(session.memory_summaries) ? session.memory_summaries : [];
  const issues = Array.isArray(review.issues) ? review.issues.map((item) => item.message || item.code).filter(Boolean) : [];
  const metrics = review.metrics || {};
  container.innerHTML = `
    <div class="source-summary-item ${director.mode ? "source-summary-ready" : "source-summary-muted"}">
      <small>NpcTurnDirector · 本轮已消费</small>
      <strong>${escapeHtml(director.mode || "尚未规划")} · ${escapeHtml(director.emotion || "-")} · ${escapeHtml(director.relationship_stage || session.relationship_stage || "-")}</strong>
      <span>${escapeHtml(director.current_topic || "发送消息后生成本轮导演计划")} · 问题 ${escapeHtml(director.question_budget ?? "-")} / 动作 ${escapeHtml(director.action_budget ?? "-")}</span>
    </div>
    <div class="source-summary-item ${capsule.length ? "source-summary-ready" : "source-summary-muted"}">
      <small>Memory Capsule · 常驻记忆</small>
      <strong>${escapeHtml(capsule.length)} 条</strong>
      <span>${escapeHtml(capsule.join("；") || "玩家明确要求记住的称呼、身份和偏好会进入这里")}</span>
    </div>
    <div class="source-summary-item ${Object.keys(working).length ? "source-summary-ready" : "source-summary-muted"}">
      <small>Working Memory · 动态连续性</small>
      <strong>${escapeHtml(working.current_topic || "暂无当前话题")}</strong>
      <span>${escapeHtml(working.open_loop || working.last_npc_reply || "暂无未完成话题")} · 压缩摘要 ${escapeHtml(summaries.length)} 条</span>
    </div>
    <div class="source-summary-item ${review.passed ? "source-summary-ready" : review.reviewer ? "source-summary-error" : "source-summary-muted"}">
      <small>NpcConversationReview · 输出复核</small>
      <strong>${escapeHtml(review.reviewer ? (review.passed ? "通过" : "未通过") : "尚未复核")}</strong>
      <span>${escapeHtml(issues.join("；") || (review.metrics ? `问题 ${metrics.question_count || 0}/${metrics.question_budget ?? "-"} · 动作 ${metrics.action_count || 0}/${metrics.action_budget ?? "-"}` : "发送消息后显示复核结果"))}</span>
    </div>
  `;
}

function selectNpcRuntimeContext() {
  state.selectedNpcRuntimeId = $("#npc-runtime-state-picker")?.value || "";
  renderNpcRuntimeContext();
  persistState();
}

async function loadNpcRuntimeContext(showStatus = false) {
  const worldId = state.world?.world_id;
  if (!worldId) {
    if (showStatus) setStatus("请先载入世界", true);
    return null;
  }
  try {
    state.npcRuntimeSnapshot = await requestJson(`/api/worlds/${encodeURIComponent(worldId)}/session`);
    renderNpcRuntimeContext();
    persistState();
    if (showStatus) setStatus("角色运行上下文已刷新");
    return state.npcRuntimeSnapshot;
  } catch (error) {
    if (showStatus) setStatus(`角色运行上下文刷新失败：${error.message}`, true);
    return null;
  }
}

function renderNpcLorebookVersionPicker(world = state.world) {
  const controls = $("#npc-lorebook-version-controls");
  const picker = $("#npc-lorebook-version-picker");
  const button = $("#select-npc-lorebook-version");
  if (!controls || !picker || !button) return false;
  const versions = getWorldLorebookVersions(world);
  if (!world || !versions.length) {
    controls.hidden = true;
    picker.innerHTML = "";
    picker.disabled = true;
    button.disabled = true;
    return false;
  }
  const active = getActiveLorebookVersion(world);
  const selected =
    state.selectedLorebookVersionId && versions.some((version) => version.version_id === state.selectedLorebookVersionId)
      ? state.selectedLorebookVersionId
      : active?.version_id || versions[0]?.version_id || "";
  controls.hidden = false;
  picker.disabled = false;
  picker.innerHTML = versions
    .map((version, index) => {
      const createdAt = formatFullDateTime(version.created_at) || `版本 ${index + 1}`;
      const status = version.is_active || version.version_id === active?.version_id ? "当前消费" : "可切换";
      const title = version.title || version.artifact_id || version.version_id;
      const count = version.entry_count ?? version.artifact?.entries?.length ?? 0;
      const label = `${status} · ${createdAt} · ${count} 条 · ${title}`;
      return `<option value="${escapeAttribute(version.version_id)}">${escapeHtml(label)}</option>`;
    })
    .join("");
  picker.value = selected;
  state.selectedLorebookVersionId = selected;
  const isActive = selected && (selected === active?.version_id || versions.find((version) => version.version_id === selected)?.is_active);
  button.disabled = !selected || Boolean(isActive);
  button.textContent = isActive ? "当前已使用" : "使用此版本";
  return true;
}

function renderNpcLocationPicker() {
  const picker = $("#chat-location");
  if (!picker) return;
  const current = firstLocationValue(picker.value) || firstLocationValue(state.world?.player?.location) || "";
  const locations = collectWorldLocations();
  if (current && !locations.includes(current)) locations.unshift(current);
  picker.innerHTML = locations.length
    ? locations.map((location) => `<option value="${escapeAttribute(location)}">${escapeHtml(location)}</option>`).join("")
    : `<option value="">无地点</option>`;
  picker.value = current && locations.includes(current) ? current : locations[0] || "";
}

function renderNpcSourceSummary() {
  const container = $("#npc-source-summary");
  if (!container) return;
  const world = state.world;
  if (!world) {
    container.innerHTML = `<div class="source-summary-empty">未选择可消费 JSON。请选择已保存世界。</div>`;
    return;
  }
  const runtimeState = state.stages.npc === "done" ? "已启动或已返回" : "待启动";
  const lorebookSummary = lorebookRuntimeSummary(world);
  const activeLorebookVersion = getActiveLorebookVersion(world);
  const lorebookStateClass = lorebookSummary.lorebook ? "source-summary-ready" : "source-summary-muted";
  const lorebookTitle = lorebookSummary.lorebook ? "世界书已接入 NPC Runtime" : "未生成独立世界书";
  const lorebookDetail = lorebookSummary.lorebook
    ? `${lorebookSummary.entries.length} 条目${lorebookSummary.generatedAt ? ` · ${lorebookSummary.generatedAt}` : ""}${activeLorebookVersion ? ` · ${activeLorebookVersion.version_id}` : ""}${lorebookSummary.fallbackUsed ? " · fallback" : ""}`
    : "NPC 仍可工作；会读取世界 JSON，并由运行时临时 fallback 基础条目。";
  container.innerHTML = `
    <div class="source-summary-item source-summary-main">
      <small>将消费的世界 JSON</small>
      <strong>${escapeHtml(world.name || world.world_id || "未命名世界")}</strong>
      <span>${escapeHtml(world.world_id || "-")} · ${escapeHtml((world.npcs || []).length)} NPC · ${escapeHtml((world.tasks || []).length)} 任务</span>
    </div>
    <div class="source-summary-item ${state.stages.npc === "done" ? "source-summary-ready" : "source-summary-muted"}">
      <small>NPC Runtime 状态</small>
      <strong>${escapeHtml(runtimeState)}</strong>
      <span>${escapeHtml(world.player?.location || "未设置当前位置")}</span>
    </div>
    <div class="source-summary-item ${lorebookStateClass}">
      <small>世界书增强</small>
      <strong>${escapeHtml(lorebookTitle)}</strong>
      <span>${escapeHtml(lorebookDetail)}</span>
    </div>
  `;
}

async function requestJson(url, options = {}) {
  const response = await fetch(url, {
    headers: { "Content-Type": "application/json; charset=utf-8", ...(options.headers || {}) },
    ...options,
  });
  if (!response.ok) {
    throw new Error(formatApiError(await response.text()));
  }
  return response.json();
}

async function loadWorlds(showStatus = false) {
  try {
    state.worlds = await requestJson("/api/worlds");
    renderWorldPickers();
    if (showStatus) {
      setStatus(`已刷新世界列表：${state.worlds.length} 个`);
    }
  } catch (error) {
    setStatus(`加载世界列表失败：${error.message}`, true);
  }
}

function renderWorldPickers() {
  const options = `<option value="">选择已保存世界</option>${state.worlds
    .map((world) => {
      const time = formatFullDateTime(world.updated_at || world.created_at);
      const name = world.name && world.name !== world.world_id ? `${world.name} · ${world.world_id}` : world.name || world.world_id;
      const label = time ? `${name} · ${time}` : name;
      return `<option value="${escapeAttribute(world.world_id)}">${escapeHtml(label)}</option>`;
    })
    .join("")}`;
  for (const selector of ["#world-picker", "#lorebook-world-picker", "#playtest-world-picker", "#npc-world-picker"]) {
    const picker = $(selector);
    if (!picker) continue;
    const previous = picker.value || state.world?.world_id || "";
    picker.innerHTML = options;
    if (previous) picker.value = previous;
  }
}

function syncWorldPickers() {
  if (!state.world?.world_id) return;
  for (const selector of ["#world-picker", "#lorebook-world-picker", "#playtest-world-picker", "#npc-world-picker"]) {
    const picker = $(selector);
    if (picker) picker.value = state.world.world_id;
  }
}

async function loadDecompositionArtifacts(showStatus = false) {
  try {
    state.decompositionArtifacts = await requestJson("/api/worlds/script-decompositions");
    renderDecompositionArtifactPicker();
    if (showStatus) {
      setStatus(`已刷新剧本理解列表：${state.decompositionArtifacts.length} 个`);
    }
  } catch (error) {
    setStatus(`加载剧本理解列表失败：${error.message}`, true);
  }
}

async function loadScriptGraphArtifacts(showStatus = false) {
  try {
    state.scriptGraphArtifacts = await requestJson("/api/worlds/script-graphs");
    renderScriptGraphArtifactPicker();
    await ensureDefaultWorldScriptGraphSelection();
    if (showStatus) {
      setStatus(`已刷新故事图谱列表：${state.scriptGraphArtifacts.length} 个`);
    }
  } catch (error) {
    setStatus(`加载故事图谱列表失败：${error.message}`, true);
  }
}

async function loadVisualAssetArtifacts(showStatus = false) {
  try {
    state.visualAssetArtifacts = await requestJson("/api/worlds/visual-assets");
    renderVisualAssetArtifactPicker();
    renderWorld();
    if (showStatus) {
      setStatus(`已刷新视觉计划列表：${state.visualAssetArtifacts.length} 个`);
    }
  } catch (error) {
    setStatus(`加载视觉计划列表失败：${error.message}`, true);
  }
}

async function loadVisualAssetRuns(showStatus = false) {
  if (!state.visualPlan) {
    state.visualAssetRuns = [];
    renderVisualAssetRunPicker();
    if (showStatus) setStatus("需要先载入视觉计划，才能查看生成批次", true);
    return;
  }
  try {
    state.visualAssetRuns = await requestJson(`/api/worlds/visual-assets/runs?${visualAssetRunQuery()}`);
    renderVisualAssetRunPicker();
    renderWorldVisualAssetRunPicker();
    if (showStatus) {
      setStatus(`已刷新生成批次：${state.visualAssetRuns.length} 个`);
    }
  } catch (error) {
    setStatus(`加载生成批次失败：${error.message}`, true);
  }
}

function renderDecompositionArtifactPicker() {
  const picker = $("#decomposition-artifact-picker");
  if (!picker) return;
  const previous = picker.value || state.decompositionArtifactId || "";
  picker.innerHTML = `<option value="">选择已保存剧本理解</option>${(state.decompositionArtifacts || [])
    .map((artifact) => {
      const time = artifactTimeLabel(artifact);
      const label = `${artifact.title || artifact.artifact_id} · ${artifact.node_count || 0} 节点 · ${artifact.edge_count || 0} 关系${time ? ` · ${time}` : ""}`;
      return `<option value="${escapeAttribute(artifact.artifact_id)}">${escapeHtml(label)}</option>`;
    })
    .join("")}`;
  if (previous) picker.value = previous;
}

function renderScriptGraphArtifactPicker() {
  const options = `<option value="">选择已保存故事图谱</option>${(state.scriptGraphArtifacts || [])
    .map((artifact) => {
      const time = artifactTimeLabel(artifact);
      const label = `${artifact.title || artifact.artifact_id} · ${artifact.node_count || 0} 节点 · ${artifact.edge_count || 0} 关系${time ? ` · ${time}` : ""}`;
      return `<option value="${escapeAttribute(artifact.artifact_id)}">${escapeHtml(label)}</option>`;
    })
    .join("")}`;
  for (const selector of ["#script-graph-artifact-picker", "#visual-script-graph-picker", "#world-script-graph-picker"]) {
    const picker = $(selector);
    if (!picker) continue;
    const previous = picker.value || state.scriptGraphArtifactId || "";
    picker.innerHTML = options;
    if (previous) picker.value = previous;
  }
}

function syncScriptGraphPickers() {
  for (const selector of ["#script-graph-artifact-picker", "#visual-script-graph-picker", "#world-script-graph-picker"]) {
    const picker = $(selector);
    if (picker && state.scriptGraphArtifactId) picker.value = state.scriptGraphArtifactId;
  }
}

function renderVisualAssetArtifactPicker() {
  const picker = $("#visual-asset-picker");
  if (!picker) return;
  const currentTitle = state.visualPlan?.title || state.scriptGraph?.title || state.decomposition?.title || "未命名视觉计划";
  const currentOption = state.visualPlan
    ? `<option value="__current_visual_plan">${escapeHtml(currentTitle)} · 当前视觉计划 · ${(state.visualPlan.assets || []).length} 资产</option>`
    : "";
  const options = (state.visualAssetArtifacts || [])
    .map((artifact) => {
      const kind = "已保存计划";
      const generated = artifact.generated_count ? ` · 已生成 ${artifact.generated_count}` : "";
      const updated = artifactTimeLabel(artifact);
      const updatedText = updated ? ` · ${updated}` : "";
      const label = `${artifact.title || artifact.artifact_id} · ${kind} · ${artifact.asset_count || 0} 资产${generated}${updatedText}`;
      return `<option value="${escapeAttribute(artifact.artifact_id)}">${escapeHtml(label)}</option>`;
    })
    .join("");
  const previous = picker.value || state.visualAssetArtifactId || "";
  picker.innerHTML = `<option value="">选择已保存视觉计划</option>${currentOption}${options}`;
  if (previous) picker.value = previous;
}

function renderVisualAssetRunPicker() {
  const picker = $("#visual-asset-run-picker");
  if (!picker) return;
  const options = (state.visualAssetRuns || [])
    .map((run) => {
      const label = `${formatFullDateTime(run.updated_at || run.created_at) || run.run_id} · ${run.asset_count || 0} 张 · ${run.run_id}`;
      return `<option value="${escapeAttribute(run.run_id)}">${escapeHtml(label)}</option>`;
    })
    .join("");
  const previous = picker.value || state.selectedVisualAssetRunId || state.imageResult?.metadata?.generation_run_id || "";
  picker.innerHTML = `<option value="">选择已生成批次</option>${options}`;
  if (previous) picker.value = previous;
  renderWorldVisualAssetRunPicker();
}

function formatShortDateTime(value) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return date.toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false });
}

function formatFullDateTime(value) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return date.toLocaleString("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  });
}

function artifactTimeLabel(artifact = {}) {
  return formatFullDateTime(artifact.updated_at || artifact.created_at || artifact.saved_at);
}

function worldSourceTimeLabel(source) {
  if (!source) return "";
  if (source.reason === "state.scriptGraph" || source.reason === "script_graph_editor") {
    const artifact = (state.scriptGraphArtifacts || []).find((item) => item.artifact_id === state.scriptGraphArtifactId);
    return artifactTimeLabel(artifact);
  }
  if (source.reason === "state.decomposition" || source.reason === "decomposition_editor") {
    const artifact = (state.decompositionArtifacts || []).find((item) => item.artifact_id === state.decompositionArtifactId);
    return artifactTimeLabel(artifact);
  }
  return "";
}

async function loadEffectiveConfig(forceApply = false) {
  try {
    const data = await requestJson("/api/config/effective?include_secrets=true");
    state.effectiveConfig = data;
    applyEffectiveConfig(data);
    renderEffectiveConfig(data);
    persistState();
    if (forceApply) {
      setStatus("已读取后端配置");
    }
  } catch (error) {
    $("#effective-config-summary").textContent = `读取后端配置失败：${error.message}`;
  }
}

function applyEffectiveConfig(data) {
  const defaults = data.defaults || {};
  const agents = data.agents || {};
  const defaultLlm = defaults.llm || {};
  const defaultImage = defaults.image || {};
  const script = agents.script_decomposition || { use_default_llm: true, llm: data.script_decomposition_api || data.world_api || {} };
  const world = agents.world_builder || { use_default_llm: false, llm: data.world_api || {} };
  const visual = agents.visual_prompt_composer || { use_default_llm: false, llm: data.visual_prompt_api || {} };
  const image = agents.visual_asset_generation || { use_default_image: false, image: data.image_api || {} };
  const npc = agents.npc_runtime || { use_default_llm: false, llm: data.npc_api || {} };

  setConfigInput("#default-llm-base-url", defaultLlm.base_url);
  setConfigInput("#default-llm-api-key", defaultLlm.api_key);
  setConfigInput("#default-llm-model", defaultLlm.model);
  if (defaultImage.provider) $("#default-image-provider").value = defaultImage.provider;
  setConfigInput("#default-image-base-url", defaultImage.api_base_url);
  setConfigInput("#default-image-api-key", defaultImage.api_key);
  syncImageProviderControls({
    providerSelector: "#default-image-provider",
    modelSelector: "#default-image-model",
    preferredModel: defaultImage.model || "step-image-edit-2",
    preferredSize: defaultImage.size || "1024x1024",
  });
  setConfigInput("#default-image-retry", defaultImage.retry_count);
  setConfigInput("#default-image-seed", defaultImage.seed);
  setConfigInput("#default-image-steps", defaultImage.steps);
  setConfigInput("#default-image-cfg-scale", defaultImage.cfg_scale);
  $("#default-image-text-mode").checked = Boolean(defaultImage.text_mode);

  $("#script-use-default-llm").checked = script.use_default_llm !== false;
  setConfigInput("#script-base-url", script.llm?.base_url);
  setConfigInput("#script-api-key", script.llm?.api_key);
  setConfigInput("#script-model", script.llm?.model);
  $("#world-use-default-llm").checked = world.use_default_llm !== false;
  setConfigInput("#world-base-url", world.llm?.base_url);
  setConfigInput("#world-api-key", world.llm?.api_key);
  setConfigInput("#world-model", world.llm?.model);
  $("#visual-use-default-llm").checked = visual.use_default_llm !== false;
  setConfigInput("#visual-prompt-base-url", visual.llm?.base_url);
  setConfigInput("#visual-prompt-api-key", visual.llm?.api_key);
  setConfigInput("#visual-prompt-model", visual.llm?.model);
  $("#npc-use-default-llm").checked = npc.use_default_llm !== false;
  setConfigInput("#npc-base-url", npc.llm?.base_url);
  setConfigInput("#npc-api-key", npc.llm?.api_key);
  setConfigInput("#npc-model", npc.llm?.model);

  $("#image-use-default-image").checked = image.use_default_image !== false;
  const imageConfig = image.image || data.image_api || {};
  setConfigInput("#image-base-url", imageConfig.api_base_url);
  setConfigInput("#image-api-key", imageConfig.api_key);
  if (imageConfig.provider) $("#image-provider").value = imageConfig.provider;
  syncImageProviderControls(imageConfig.model || defaultImage.model || "step-image-edit-2", imageConfig.size || defaultImage.size || "1024x1024");
  setConfigInput("#image-retry", imageConfig.retry_count);
  setConfigInput("#image-seed", imageConfig.seed);
  setConfigInput("#image-steps", imageConfig.steps);
  setConfigInput("#image-cfg-scale", imageConfig.cfg_scale);
  $("#image-text-mode").checked = Boolean(imageConfig.text_mode);
  syncAgentConfigVisibility();
  config = configFromForm();
}

function setConfigInput(selector, value) {
  if (value === undefined || value === null) return;
  const input = $(selector);
  if (!input) return;
  input.value = String(value);
}

function renderEffectiveConfig(data = state.effectiveConfig) {
  const container = $("#effective-config-summary");
  if (!data) {
    container.textContent = "未读取后端配置";
    return;
  }
  const agents = data.agents || {};
  const rows = [
    ["ScriptDecompositionAgent", agents.script_decomposition?.effective_llm?.base_url || data.script_decomposition_api?.base_url, agents.script_decomposition?.effective_llm?.model || data.script_decomposition_api?.model, agents.script_decomposition?.effective_llm?.has_api_key ?? data.script_decomposition_api?.has_api_key],
    ["WorldBuilderAgent", agents.world_builder?.effective_llm?.base_url || data.world_api?.base_url, agents.world_builder?.effective_llm?.model || data.world_api?.model, agents.world_builder?.effective_llm?.has_api_key ?? data.world_api?.has_api_key],
    ["VisualPromptComposerAgent", agents.visual_prompt_composer?.effective_llm?.base_url || data.visual_prompt_api?.base_url, agents.visual_prompt_composer?.effective_llm?.model || data.visual_prompt_api?.model, agents.visual_prompt_composer?.effective_llm?.has_api_key ?? data.visual_prompt_api?.has_api_key],
    ["VisualAssetGenerationAgent", agents.visual_asset_generation?.effective_image?.api_base_url || data.image_api?.api_base_url, agents.visual_asset_generation?.effective_image?.model || data.image_api?.model, agents.visual_asset_generation?.effective_image?.has_api_key ?? data.image_api?.has_api_key],
    ["NPCRuntimeAgent", agents.npc_runtime?.effective_llm?.base_url || data.npc_api?.base_url, agents.npc_runtime?.effective_llm?.model || data.npc_api?.model, agents.npc_runtime?.effective_llm?.has_api_key ?? data.npc_api?.has_api_key],
  ];
  container.innerHTML = rows
    .map(([label, baseUrl, model, hasKey]) => {
      const keyClass = hasKey ? "ok" : "missing";
      const keyText = hasKey ? "key 已配置" : "key 未配置";
      return `<div><strong>${escapeHtml(label)}</strong> ${escapeHtml(model || "-")} · ${escapeHtml(baseUrl || "-")} · <span class="${keyClass}">${keyText}</span></div>`;
    })
    .join("");
}

async function loadPickedWorld() {
  const worldId = $("#world-picker").value;
  if (!worldId) return;
  await loadWorldById(worldId);
}

async function loadPickedNpcWorld() {
  const worldId = $("#npc-world-picker").value;
  if (!worldId) {
    setStatus("请选择用于 NPC 对话的世界 JSON", true);
    return;
  }
  await loadWorldById(worldId);
  appendRunLog("done", "NPC 对话世界已载入", formatLorebookRuntimeStatus(state.world));
}

async function loadPickedLorebookWorld() {
  const worldId = $("#lorebook-world-picker").value;
  if (!worldId) {
    setStatus("请选择要查看世界书的世界 JSON", true);
    return;
  }
  await loadWorldById(worldId);
}

async function hotLoadPickedLorebookWorld() {
  const worldId = $("#lorebook-world-picker").value;
  if (!worldId) return;
  await loadWorldById(worldId, { hot: true });
}

async function hotLoadPickedPlaytestWorld() {
  const worldId = $("#playtest-world-picker").value;
  if (!worldId) return;
  await loadWorldById(worldId, { hot: true });
  appendRunLog("done", "试玩世界已载入", `world_id：${worldId}`);
}

async function generateLorebookForCurrentWorld() {
  const selectedWorldId = $("#lorebook-world-picker")?.value || "";
  const worldId = state.world?.world_id || selectedWorldId;
  if (!worldId) {
    setStatus("请先载入一个世界，再生成世界书", true);
    return;
  }
  await runTask("lorebook", async () => {
    setRunningDetail("NpcLorebookCreationAgent 正在生成世界书", "正在把当前世界 JSON 转成可按关键词激活的 Lorebook 条目...");
    appendRunLog("running", "生成世界书", `world_id：${worldId}`);
    const world = await requestJson(`/api/worlds/${encodeURIComponent(worldId)}/lorebook/generate`, { method: "POST" });
    state.world = world;
    state.selectedLorebookVersionId = getActiveLorebookVersion(world)?.version_id || "";
    setStage("world", "done");
    setLorebookStageForWorld(world);
    setStage("npc", "dirty");
    setStage("playtest", "dirty");
    syncWorldPickers();
    await loadWorlds();
    const lorebook = getWorldLorebook(world);
    const generation = world.metadata?.npc_lorebook_generation || {};
    const time = formatFullDateTime(generation.created_at || lorebookGeneratedTime(world));
    setStatus(`世界书已生成：${lorebook?.entries?.length || 0} 条${time ? ` · ${time}` : ""}`);
    appendRunLog(
      "done",
      "世界书生成完成",
      `world_id：${world.world_id}\n条目：${lorebook?.entries?.length || 0}\n生成时间：${time || "-"}\n创建者：${generation.created_by || lorebook?.metadata?.created_by || "-"}`,
    );
    renderAll();
  });
}

function previewPickedLorebookVersion() {
  state.selectedLorebookVersionId = $("#lorebook-version-picker")?.value || "";
  renderLorebook();
  renderNpcControls();
  persistState();
}

async function selectPickedLorebookVersion() {
  const versionId = $("#lorebook-version-picker")?.value || state.selectedLorebookVersionId || "";
  await selectLorebookVersionForCurrentWorld(versionId);
}

function previewPickedNpcLorebookVersion() {
  state.selectedLorebookVersionId = $("#npc-lorebook-version-picker")?.value || "";
  renderNpcControls();
  renderLorebook();
  persistState();
}

async function selectPickedNpcLorebookVersion() {
  const versionId = $("#npc-lorebook-version-picker")?.value || state.selectedLorebookVersionId || "";
  await selectLorebookVersionForCurrentWorld(versionId);
}

async function selectLorebookVersionForCurrentWorld(versionId) {
  const worldId = state.world?.world_id || $("#lorebook-world-picker")?.value || "";
  if (!worldId || !versionId) {
    setStatus("请先选择世界和世界书版本", true);
    return;
  }
  await runTask("lorebook", async () => {
    appendRunLog("running", "切换世界书版本", `world_id：${worldId}\nversion_id：${versionId}`);
    const world = await requestJson(`/api/worlds/${encodeURIComponent(worldId)}/lorebook/select/${encodeURIComponent(versionId)}`, { method: "POST" });
    state.world = world;
    state.selectedLorebookVersionId = versionId;
    setLorebookStageForWorld(world);
    setStage("npc", "dirty");
    setStage("playtest", "dirty");
    syncWorldPickers();
    await loadWorlds();
    const version = getSelectedLorebookVersion(world);
    const lorebook = version?.artifact || getWorldLorebook(world);
    setStatus(`已切换当前消费世界书：${lorebook?.title || versionId}`);
    appendRunLog(
      "done",
      "世界书版本已切换",
      `world_id：${world.world_id}\nversion_id：${versionId}\n条目：${lorebook?.entries?.length || 0}`,
    );
    renderAll();
  });
}

async function hotLoadPickedNpcWorld() {
  const worldId = $("#npc-world-picker").value;
  if (!worldId) return;
  await loadWorldById(worldId, { hot: true });
}

function syncImageProviderControls(optionsOrPreferredModel = "", preferredSize = "") {
  const options =
    typeof optionsOrPreferredModel === "object"
      ? optionsOrPreferredModel
      : { preferredModel: optionsOrPreferredModel, preferredSize };
  const providerSelector = options.providerSelector || "#image-provider";
  const modelSelector = options.modelSelector || "#image-model";
  const sizeSelector = options.sizeSelector || "#image-size";
  const syncSize = options.syncSize !== false;
  const provider = $(providerSelector).value || "stepfun";
  if (provider === "stepfun") {
    setSelectOptions(
      modelSelector,
      stepfunImageModels.map((model) => ({ value: model, label: model })),
      options.preferredModel || $(modelSelector).value || "step-image-edit-2",
    );
    if (syncSize) setSelectOptions(sizeSelector, stepfunImageSizes, options.preferredSize || $(sizeSelector).value || "1024x1024");
    if (!stepfunImageModels.includes($(modelSelector).value)) $(modelSelector).value = "step-image-edit-2";
    if (syncSize && !stepfunImageSizes.some((item) => item.value === $(sizeSelector).value)) $(sizeSelector).value = "1024x1024";
  } else {
    const model = options.preferredModel || $(modelSelector).value || config.imageModel || "";
    setSelectOptions(modelSelector, [{ value: model, label: model || "后端默认模型" }], model);
    if (syncSize) {
      const size = options.preferredSize || $(sizeSelector).value || config.imageSize || "1024x1024";
      setSelectOptions(sizeSelector, [{ value: size, label: size || "后端默认尺寸" }], size);
    }
  }
}

function setSelectOptions(selector, options, selectedValue = "") {
  const select = $(selector);
  const normalized = options.filter((item) => item.value !== undefined);
  select.innerHTML = normalized
    .map((item) => `<option value="${escapeAttribute(item.value)}">${escapeHtml(item.label || item.value)}</option>`)
    .join("");
  if (selectedValue && normalized.some((item) => item.value === selectedValue)) {
    select.value = selectedValue;
  }
}

async function loadPickedDecompositionArtifact() {
  const artifactId = $("#decomposition-artifact-picker").value;
  if (!artifactId) {
    setStatus("请选择已保存剧本理解", true);
    return;
  }
  await loadDecompositionArtifact(artifactId);
}

async function hotLoadPickedDecompositionArtifact() {
  const artifactId = $("#decomposition-artifact-picker").value;
  if (!artifactId) return;
  await loadDecompositionArtifact(artifactId, { hot: true });
}

async function loadDecompositionArtifact(artifactId, options = {}) {
  const token = ++hotLoadTokens.decomposition;
  await runTask("decomposition", async () => {
    appendRunLog("running", options.hot ? "热载入剧本理解" : "加载剧本理解", `artifact_id：${artifactId}`);
    const data = await requestJson(`/api/worlds/script-decompositions/${encodeURIComponent(artifactId)}`);
    if (token !== hotLoadTokens.decomposition) return;
    state.decompositionArtifactId = artifactId;
    ingestDecompositionArtifact(data);
    setStatus(`已载入剧本理解：${data.artifact?.title || artifactId}`);
    appendRunLog(
      "done",
      options.hot ? "剧本理解已热载入" : "剧本理解已载入",
      `标题：${state.decomposition?.title || "-"}\n图节点：${state.decomposition?.story_graph?.entities?.length || 0}\n图关系：${state.decomposition?.story_graph?.relations?.length || 0}`,
    );
    renderAll();
  });
}

async function loadPickedScriptGraphArtifact() {
  const artifactId = $("#script-graph-artifact-picker").value;
  if (!artifactId) {
    setStatus("请选择已保存故事图谱", true);
    return;
  }
  await loadScriptGraphArtifact(artifactId);
}

async function hotLoadPickedScriptGraphArtifact() {
  const artifactId = $("#script-graph-artifact-picker").value;
  if (!artifactId) return;
  await loadScriptGraphArtifact(artifactId, { hot: true });
}

async function loadPickedVisualScriptGraphArtifact() {
  const artifactId = $("#visual-script-graph-picker").value;
  if (!artifactId) {
    setStatus("请选择用于视觉提示词的故事图谱", true);
    return;
  }
  await loadScriptGraphArtifact(artifactId);
}

async function hotLoadPickedVisualScriptGraphArtifact() {
  const artifactId = $("#visual-script-graph-picker").value;
  if (!artifactId) return;
  await loadScriptGraphArtifact(artifactId, { hot: true });
}

async function loadPickedWorldScriptGraphArtifact() {
  const artifactId = $("#world-script-graph-picker").value;
  if (!artifactId) {
    setStatus("请选择用于世界生成的故事图谱", true);
    return;
  }
  await loadScriptGraphArtifact(artifactId);
  await loadWorldVisualAssetRuns();
}

async function hotLoadPickedWorldScriptGraphArtifact() {
  const artifactId = $("#world-script-graph-picker").value;
  if (!artifactId) return;
  await loadScriptGraphArtifact(artifactId, { hot: true });
  await loadWorldVisualAssetRuns();
}

async function ensureDefaultWorldScriptGraphSelection() {
  const picker = $("#world-script-graph-picker");
  if (!picker || picker.value || state.scriptGraphArtifactId || !state.scriptGraphArtifacts?.length) {
    renderWorld();
    return;
  }
  const firstArtifactId = state.scriptGraphArtifacts[0]?.artifact_id;
  if (!firstArtifactId) {
    renderWorld();
    return;
  }
  picker.value = firstArtifactId;
  await preloadWorldScriptGraphArtifact(firstArtifactId);
}

async function loadPickedVisualAssetArtifact() {
  const artifactId = $("#visual-asset-picker").value;
  if (!artifactId) {
    setStatus("请选择已保存视觉计划", true);
    return;
  }
  if (artifactId === "__current_visual_plan") {
    state.visualAssetArtifactId = artifactId;
    state.imageResult = null;
    state.selectedVisualAssetRunId = "";
    setStage("images", "dirty");
    await loadVisualAssetRuns();
    renderAll();
    const title = state.visualPlan?.title || "未命名视觉计划";
    setStatus(`已载入 ${title}：${state.visualPlan?.assets?.length || 0} 个待生成资产，可调用图片 API`);
    appendRunLog("done", "视觉计划已载入", `标题：${title}\n资产：${state.visualPlan?.assets?.length || 0}`);
    return;
  }
  await runTask("images", async () => {
    appendRunLog("running", "加载视觉计划", `artifact_id：${artifactId}`);
    const data = await requestJson(`/api/worlds/visual-assets/${encodeURIComponent(artifactId)}`);
    state.visualAssetArtifactId = artifactId;
    state.visualPlan = data.plan || null;
    state.imageResult = data.result || null;
    state.selectedVisualAssetRunId = state.imageResult?.metadata?.generation_run_id || "";
    setStage("prompts", state.visualPlan ? "done" : state.stages.prompts);
    setStage("images", state.imageResult ? "done" : "dirty");
    await loadVisualAssetRuns();
    setStatus(`已载入视觉计划：${data.artifact?.title || artifactId}`);
    appendRunLog(
      "done",
      "视觉计划已载入",
      `资产：${state.visualPlan?.assets?.length || 0}\n类型：${data.artifact?.kind || "visual_plan"}`,
    );
    renderAll();
  });
}

async function loadPickedVisualAssetRun() {
  const runId = $("#visual-asset-run-picker")?.value || "";
  if (!runId) {
    setStatus("请选择要查看的生成批次", true);
    return;
  }
  await runTask("images", async () => {
    appendRunLog("running", "加载生成批次", `run_id：${runId}`);
    const run = await loadVisualAssetRunById(runId);
    setStage("images", "done");
    setStatus(`已切换到生成批次：${formatShortDateTime(run.updated_at || run.created_at) || run.run_id}`);
    appendRunLog("done", "生成批次已载入", `图片：${run.asset_count || 0}\n路径：${run.path || ""}`);
    renderAll();
  });
}

async function loadWorldVisualAssetRuns(showStatus = false) {
  const source = resolveWorldGenerationSource();
  const visualPlan = await ensureVisualPlanForWorldSource(source);
  if (!visualPlan) {
    state.visualAssetRuns = [];
    renderWorldVisualAssetRunPicker();
    setStatus("当前故事图谱还没有匹配到视觉计划，无法选择图片资产批次", true);
    return;
  }
  await loadVisualAssetRuns(showStatus);
}

async function loadPickedWorldVisualAssetRun() {
  const runId = $("#world-visual-asset-run-picker")?.value || "";
  if (!runId) {
    state.imageResult = null;
    state.selectedVisualAssetRunId = "";
    renderWorld();
    setStatus("世界生成将不指定图片批次；如有视觉计划则只带计划资产", true);
    return;
  }
  await runTask("images", async () => {
    appendRunLog("running", "世界生成载入图片资产批次", `run_id：${runId}`);
    const run = await loadVisualAssetRunById(runId);
    setStage("images", "done");
    setStatus(`世界生成已选择图片资产批次：${formatShortDateTime(run.updated_at || run.created_at) || run.run_id}`);
    appendRunLog("done", "图片资产批次已用于世界生成", `图片：${run.asset_count || 0}\n路径：${run.path || ""}`);
    renderAll();
  });
}

async function hotLoadPickedWorldVisualAssetRun() {
  const runId = $("#world-visual-asset-run-picker")?.value || "";
  if (!runId) {
    state.imageResult = null;
    state.selectedVisualAssetRunId = "";
    renderWorld();
    return;
  }
  renderWorldAssetRunSummary();
  const run = await loadVisualAssetRunById(runId);
  if (run) renderAll();
}

async function loadVisualAssetRunById(runId) {
  const run = await requestJson(`/api/worlds/visual-assets/runs/${encodeURIComponent(runId)}?${visualAssetRunQuery()}`);
  state.selectedVisualAssetRunId = run.run_id;
  if (run.visual_plan) {
    state.visualPlan = run.visual_plan;
    state.visualAssetArtifactId = run.visual_plan_artifact?.artifact_id || state.visualAssetArtifactId;
    setStage("prompts", "done");
  }
  state.imageResult = imageResultFromVisualAssetRun(run);
  return run;
}

function imageResultFromVisualAssetRun(run) {
  return {
    plan: state.visualPlan,
    generated: mergeRunAssetsWithCurrentPlan(run.assets || []),
    failed: [],
    metadata: { generation_run_id: run.run_id, generation_run_path: run.path },
  };
}

async function deletePickedVisualAssetRun() {
  const runId = $("#visual-asset-run-picker")?.value || state.selectedVisualAssetRunId || "";
  if (!runId) {
    setStatus("请选择要删除的生成批次", true);
    return;
  }
  if (!window.confirm(`删除这个生成批次？\n${runId}\n\n该批次目录下的图片会从磁盘删除。`)) return;
  await runTask("images", async () => {
    const data = await requestJson(`/api/worlds/visual-assets/runs/${encodeURIComponent(runId)}?${visualAssetRunQuery()}`, {
      method: "DELETE",
    });
    if (state.selectedVisualAssetRunId === runId || state.imageResult?.metadata?.generation_run_id === runId) {
      state.imageResult = null;
      state.selectedImageAssetId = "";
      state.selectedVisualAssetRunId = "";
    }
    await loadVisualAssetRuns();
    setStage("images", state.visualPlan ? "dirty" : "idle");
    setStatus(`已删除生成批次：${data.run_id || runId}`);
    appendRunLog("done", "生成批次已删除", data.path || runId);
    renderAll();
  });
}

function visualAssetRunQuery() {
  const params = new URLSearchParams();
  params.set("world_id", state.visualPlan?.world_id || "");
  params.set("title", state.visualPlan?.title || "");
  params.set("output_root", "output/visual_assets");
  return params.toString();
}

function mergeRunAssetsWithCurrentPlan(runAssets) {
  const planAssets = state.visualPlan?.assets || [];
  return runAssets.map((asset, index) => {
    const planned =
      planAssets.find((item) => item.output_path && asset.output_path && item.output_path.split(/[\\/]/).pop() === asset.output_path.split(/[\\/]/).pop()) ||
      planAssets[index] ||
      {};
    return {
      ...planned,
      ...asset,
      id: planned.id || asset.id || `run_asset_${index + 1}`,
      kind: planned.kind || asset.kind || "other",
      display_name: planned.display_name || asset.display_name || asset.id,
      prompt: planned.prompt || asset.prompt || "",
      negative_prompt: planned.negative_prompt || asset.negative_prompt || "",
      metadata: { ...(planned.metadata || {}), ...(asset.metadata || {}) },
    };
  });
}

async function loadScriptGraphArtifact(artifactId, options = {}) {
  const token = ++hotLoadTokens.graph;
  await runTask("graph", async () => {
    if (!options.quiet) {
      appendRunLog("running", options.hot ? "热载入故事图谱" : "加载故事图谱", `artifact_id：${artifactId}`);
    }
    const data = await requestJson(`/api/worlds/script-graphs/${encodeURIComponent(artifactId)}`);
    if (token !== hotLoadTokens.graph) return;
    ingestScriptGraphArtifact(data, artifactId);
    if (!options.quiet) {
      setStatus(`已载入故事图谱：${state.scriptGraph?.title || artifactId}`);
      appendRunLog(
        "done",
        options.hot ? "故事图谱已热载入" : "故事图谱已载入",
        `节点：${state.scriptGraph?.nodes?.length || 0}\n关系：${state.scriptGraph?.edges?.length || 0}`,
      );
    }
    renderAll();
  });
}

async function preloadWorldScriptGraphArtifact(artifactId) {
  const token = ++hotLoadTokens.graph;
  try {
    const data = await requestJson(`/api/worlds/script-graphs/${encodeURIComponent(artifactId)}`);
    if (token !== hotLoadTokens.graph) return;
    ingestScriptGraphArtifact(data, artifactId);
    persistState();
    renderAll();
  } catch (error) {
    setStatus(`预载入世界生成图谱失败：${error.message}`, true);
  }
}

function ingestScriptGraphArtifact(data, artifactId) {
  state.scriptGraph = data.graph_id ? data : data.graph;
  state.scriptGraphArtifactId = artifactId;
  resetGraphViewport();
  setStage("graph", "done");
  setDownstreamDirty(["prompts", "images", "world", "lorebook", "npc", "playtest"]);
  syncScriptGraphPickers();
}

async function loadWorldById(worldId, options = {}) {
  return runTask("world", async () => {
    appendRunLog("running", options.hot ? "热载入世界 JSON" : "加载已有世界", `world_id：${worldId}`);
    const world = await requestJson(`/api/worlds/${encodeURIComponent(worldId)}`);
    ingestWorld(world);
    setStatus(`已载入世界：${world.name || world.world_id}`);
    appendRunLog(
      "done",
      options.hot ? "世界 JSON 已热载入" : "已有世界已载入",
      `名称：${world.name || "-"}\nNPC：${world.npcs?.length || 0}\n任务：${world.tasks?.length || 0}`,
    );
    renderAll();
  });
}

function ingestWorld(world) {
  state.world = world;
  state.npcRuntimeSnapshot = null;
  state.selectedNpcRuntimeId = "";
  const versions = getWorldLorebookVersions(world);
  state.selectedLorebookVersionId = versions.some((version) => version.version_id === state.selectedLorebookVersionId)
    ? state.selectedLorebookVersionId
    : getActiveLorebookVersion(world)?.version_id || versions[0]?.version_id || "";
  state.decomposition = world?.metadata?.script_decomposition || state.decomposition;
  state.report = state.decomposition?.report || world?.metadata?.script_case?.report || state.report;
  setStage("world", "done");
  setLorebookStageForWorld(world);
  if (state.decomposition) setStage("decomposition", "done");
  syncWorldPickers();
}

function ingestDecompositionArtifact(data) {
  const response = data.response && Object.keys(data.response).length ? data.response : null;
  state.decompositionResponse = response;
  state.decomposition = data.decomposition;
  state.report = (data.report && Object.keys(data.report).length ? data.report : response?.report) || null;
  state.world = response?.world || null;
  state.scriptGraph = null;
  state.scriptGraphArtifactId = "";
  resetGraphViewport();
  state.visualPlan = null;
  state.imageResult = null;
  setStage("script", "done");
  setStage("decomposition", "done");
  setStage("graph", "dirty");
  setStage("prompts", "dirty");
  setStage("images", "dirty");
  setStage("world", state.world ? "done" : "dirty");
  setStage("lorebook", state.world ? (hasWorldLorebook(state.world) ? "done" : "error") : "dirty");
  setStage("npc", "dirty");
  setStage("playtest", "dirty");
}

async function loadSelectedFile() {
  const files = selectedScriptFiles();
  if (!files.length) {
    setStatus("请选择文件", true);
    return;
  }
  if (files.some((file) => ["docx", "pdf"].includes(fileExtension(file)))) {
    setStatus("包含 docx/pdf，请使用“后端导入”读取");
    return;
  }
  const parts = [];
  for (const [index, file] of files.entries()) {
    const text = await file.text();
    parts.push(`\n\n## Source File ${index + 1}: ${file.webkitRelativePath || file.name}\n\n${text}`);
  }
  const text = parts.join("\n").trim();
  state.sourceText = text;
  $("#source-text").value = text;
  if (!$("#script-title").value) $("#script-title").value = inferTitleFromFiles(files);
  setStage("script", "done");
  appendRunLog("done", "本地文件读取完成", `文件数：${files.length}\n文本长度：${text.length} 字符`);
  persistState();
  setStatus(`已读取 ${files.length} 个文件`);
}

async function importDocument() {
  const files = selectedScriptFiles();
  if (!files.length) {
    setStatus("请选择要导入的文档", true);
    return;
  }
  await runTask("script", async () => {
    const mode = $("#decomposition-mode").value || "llm";
    const llmConfig = mode === "llm" ? buildLlmConfig("script") : null;
    setRunningDetail(
      mode === "llm" ? "LLM Agent 正在拆解多文件剧本" : "规则解析正在读取多文件剧本",
      `正在处理 ${files.length} 个文档，请稍等...`,
    );
    appendRunLog(
      "running",
      "准备导入文档",
      `模式：${mode}\n文件数：${files.length}\n${summarizeLlmConfig(llmConfig, "script")}`,
    );
    const body = new FormData();
    for (const file of files) {
      body.append("files", file, file.webkitRelativePath || file.name);
    }
    body.append("player_name", $("#script-player").value || "主角");
    body.append("world_name", $("#script-title").value || inferTitleFromFiles(files));
    body.append("decomposition_mode", mode);
    if (mode === "llm" && llmConfig) {
      body.append("decomposition_llm", JSON.stringify(llmConfig));
    }
    appendRunLog("running", "创建后端拆解任务", "长文档导入将通过 job 轮询显示实时事件。");
    const startResponse = await fetch("/api/worlds/script-decomposition/import/jobs", { method: "POST", body });
    if (!startResponse.ok) throw new Error(formatApiError(await startResponse.text()));
    const started = await startResponse.json();
    state.currentJobId = started.job_id;
    state.currentJobKind = "script_decomposition";
    state.cancelRequested = false;
    updateCancelButton();
    persistState();
    appendRunLog("running", "后端任务已创建", `job_id：${started.job_id}`);
    const data = await pollScriptDecompositionJob(started.job_id);
    state.currentJobId = null;
    state.currentJobKind = "";
    state.cancelRequested = false;
    updateCancelButton();
    ingestDecompositionResponse(data);
    setStage("script", "done");
    setStage("decomposition", data.report?.passed ? "done" : "error");
    setStage("world", data.report?.passed ? "dirty" : "idle");
    setStatus(
      data.report?.passed
        ? `LLM Agent 已拆解 ${files.length} 个文件：${data.decomposition?.title || data.world?.name || ""}`
        : "拆解未通过，请查看缺失字段和错误",
      !data.report?.passed,
    );
    appendDecompositionLog(data);
    await loadWorlds();
    renderAll();
  });
}

async function pollScriptDecompositionJob(jobId) {
  const seen = new Set();
  while (true) {
    const job = await requestJson(`/api/worlds/script-decomposition/import/jobs/${encodeURIComponent(jobId)}`);
    renderJobEvents(job, seen);
    if (job.status === "done") {
      if (!job.result) throw new Error("ScriptDecompositionJob completed without result.");
      return job.result;
    }
    if (job.status === "cancelled") {
      state.currentJobId = null;
      state.currentJobKind = "";
      state.cancelRequested = false;
      updateCancelButton();
      throw new Error("ScriptDecompositionJob cancelled by user.");
    }
    if (job.status === "cancelling") {
      setRunningDetail("正在终止 ScriptDecompositionAgent", "后端已收到终止请求，正在停止当前任务...");
    }
    if (job.status === "error") {
      const error = job.error || {};
      state.currentJobId = null;
      state.currentJobKind = "";
      state.cancelRequested = false;
      updateCancelButton();
      throw new Error(`${error.type || "ScriptDecompositionJobError"}: ${error.message || "unknown error"}`);
    }
    await sleep(1000);
  }
}

async function runDecomposition() {
  if (selectedScriptFiles().length) {
    await importDocument();
    return;
  }
  if (!$("#source-text").value.trim()) {
    setStatus("请先粘贴原始文本，或选择文件/文件夹后再运行拆解", true);
    return;
  }
  await runTask("decomposition", async () => {
    const mode = $("#decomposition-mode").value || "llm";
    const llmConfig = mode === "llm" ? buildLlmConfig("script") : null;
    setRunningDetail(
      mode === "llm" ? "LLM Agent 正在拆解文本" : "规则解析正在拆解文本",
      "正在提取人物、地点、线索、剧情主线和约束...",
    );
    appendRunLog("running", "准备拆解文本", `模式：${mode}\n文本长度：${$("#source-text").value.length} 字符\n${summarizeLlmConfig(llmConfig, "script")}`);
    const payload = {
      title: $("#script-title").value.trim(),
      player_name: $("#script-player").value.trim() || "主角",
      source_text: $("#source-text").value,
      decomposition_mode: mode,
      decomposition_llm: llmConfig,
    };
    appendRunLog("running", "等待 ScriptDecompositionAgent", "请求已发送到后端；收到模型结果后会继续校验故事图谱。");
    const data = await runCancellableRequest("ScriptDecompositionAgent", (signal) =>
      requestJson("/api/worlds/script-decomposition", {
        method: "POST",
        body: JSON.stringify(payload),
        signal,
      }),
    );
    ingestDecompositionResponse(data);
    setStage("script", "done");
    setStage("decomposition", data.report?.passed ? "done" : "error");
    setStage("world", data.report?.passed ? "dirty" : "idle");
    setStage("lorebook", data.report?.passed ? "dirty" : "idle");
    setStage("npc", data.report?.passed ? "dirty" : "idle");
    setStage("playtest", data.report?.passed ? "dirty" : "idle");
    setStatus(data.report?.passed ? "已生成剧本理解；世界生成已拆成独立阶段。" : "剧本理解未通过，请检查报告。", !data.report?.passed);
    appendDecompositionLog(data);
    await loadWorlds();
    renderAll();
  });
}

function ingestDecompositionResponse(data) {
  state.decompositionResponse = data;
  state.decomposition = data.decomposition;
  state.report = data.report;
  state.world = data.world;
  state.scriptGraph = null;
  state.scriptGraphArtifactId = "";
  setStage("graph", state.decomposition ? "dirty" : "idle");
  if (state.world) {
    setStage("world", "done");
    setLorebookStageForWorld(state.world);
  }
}

function formatDecompositionJson() {
  const value = parseJsonEditor("#decomposition-json");
  if (!value) return;
  $("#decomposition-json").value = pretty(value);
}

function applyDecompositionEdit() {
  const value = parseJsonEditor("#decomposition-json");
  if (!value) return;
  state.decomposition = value;
  state.report = value.report || state.report;
  state.scriptGraph = null;
  state.scriptGraphArtifactId = "";
  setStage("decomposition", "done");
  setDownstreamDirty(["graph", "prompts", "images", "world", "lorebook", "npc", "playtest"]);
  persistState();
  renderAll();
  setStatus("已应用剧本理解修改");
}

async function compileScriptGraph() {
  applyOptionalDecompositionEdit();
  if (!state.decomposition) {
    setStatus("需要先有剧本理解，才能编译故事图谱", true);
    return;
  }
  await runTask("graph", async () => {
    setRunningDetail("ScriptGraphCompiler 正在编译故事图谱", "正在把剧本理解转成节点、关系和索引...");
    appendRunLog("running", "ScriptGraphCompiler", "读取当前剧本理解；只做确定性关系编译，不重新解释原文。");
    const data = await requestJson("/api/worlds/script-graph/compile", {
      method: "POST",
      body: JSON.stringify({
        decomposition: state.decomposition,
        title: state.decomposition?.title || $("#script-title").value || "",
        save: true,
      }),
    });
    state.scriptGraph = data.graph;
    state.scriptGraphArtifactId = data.artifact?.artifact_id || "";
    resetGraphViewport();
    setStage("graph", "done");
    setDownstreamDirty(["prompts", "images", "world", "lorebook", "npc", "playtest"]);
    syncScriptGraphPickers();
    setStatus(`故事图谱已编译：${data.graph.nodes?.length || 0} 节点，${data.graph.edges?.length || 0} 关系`);
    appendRunLog(
      "done",
      "故事图谱编译完成",
      `节点：${data.graph.nodes?.length || 0}\n关系：${data.graph.edges?.length || 0}\n落盘：${data.artifact?.graph_path || "未保存"}`,
    );
    await loadScriptGraphArtifacts();
    renderAll();
  });
}

async function rebuildWorldFromCurrentJson() {
  applyDecompositionEdit();
  if (!state.decomposition) return;
  await runTask("world", async () => {
    const payload = decompositionToRequest(state.decomposition);
    const data = await requestJson("/api/worlds/script-decomposition/compile", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    ingestDecompositionResponse(data);
    setStage("world", "done");
    setStatus("已用当前剧本理解重新编译世界");
    await loadWorlds();
    renderAll();
  });
}

async function planVisuals() {
  applyOptionalGraphEdit();
  const selectedGraphId = $("#visual-script-graph-picker")?.value || "";
  if (selectedGraphId && selectedGraphId !== state.scriptGraphArtifactId) {
    await loadScriptGraphArtifact(selectedGraphId);
  }
  if (!state.scriptGraph) {
    setStatus("需要先编译或载入故事图谱 ScriptGraphDocument", true);
    return;
  }
  await runTask("prompts", async () => {
    state.visualPlan = null;
    state.imageResult = null;
    state.visualAssetArtifactId = "";
    state.selectedVisualAssetRunId = "";
    state.selectedImageAssetId = "";
    renderAll();
    setRunningDetail("VisualPromptComposerAgent 正在生成提示词", "正在读取故事图谱并统一画风...");
    appendRunLog("running", "VisualPromptComposerAgent", "读取故事图谱，生成 style_guide 和 asset prompts。");
    const plan = await runCancellableRequest("VisualPromptComposerAgent", (signal) =>
      requestJson("/api/worlds/visual-assets/plan", {
        method: "POST",
        body: JSON.stringify(buildVisualRequest({ includePlan: false, includeStyleGuide: false })),
        signal,
      }),
    );
    state.visualPlan = plan;
    state.imageResult = null;
    state.selectedVisualAssetRunId = "";
    state.selectedImageAssetId = "";
    setStage("prompts", "done");
    setDownstreamDirty(["images", "world", "lorebook", "npc", "playtest"]);
    setStatus(`视觉计划完成：${plan.assets?.length || 0} 个资产`);
    appendRunLog("done", "视觉提示词完成", `资产数：${plan.assets?.length || 0}\n警告：${(plan.warnings || []).join("；") || "无"}`);
    renderAll();
  });
}

async function applyVisualPlanEdit() {
  const value = parseJsonEditor("#visual-plan-json");
  if (!value) return;
  state.visualPlan = value;
  state.imageResult = null;
  setStage("prompts", "done");
  setStage("images", "dirty");
  setDownstreamDirty(["world", "lorebook", "npc", "playtest"]);
  try {
    const artifact = await requestJson("/api/worlds/visual-assets/plans", {
      method: "POST",
      body: JSON.stringify(value),
    });
    state.visualAssetArtifactId = artifact.artifact_id || "__current_visual_plan";
    await loadVisualAssetArtifacts();
  } catch (error) {
    appendRunLog("error", "保存视觉计划失败", error.message);
  }
  persistState();
  renderAll();
  setStatus("已应用视觉计划修改");
}

async function generateImages() {
  applyOptionalGraphEdit();
  applyOptionalPlanEdit();
  if (!state.visualPlan && !state.scriptGraph) {
    setStatus("需要先载入视觉计划，或编译/载入故事图谱后生成计划", true);
    return;
  }
  await runTask("images", async () => {
    setRunningDetail("正在生成图片", "可能需要等待图片模型排队和重试...");
    appendRunLog(
      "running",
      "开始生成图片",
      `provider：${$("#image-provider").value}\nmodel：${$("#image-model").value}\nsize：${selectedImageSizeLabel()}`,
    );
    const started = await requestJson("/api/worlds/visual-assets/generate/jobs", {
      method: "POST",
      body: JSON.stringify(buildVisualRequest()),
    });
    state.currentJobId = started.job_id;
    state.currentJobKind = "visual_assets";
    state.cancelRequested = false;
    updateCancelButton();
    const result = await pollVisualAssetGenerationJob(started.job_id);
    state.imageResult = result;
    state.selectedVisualAssetRunId = result.metadata?.generation_run_id || result.plan?.metadata?.generation_run_id || "";
    setStage("images", result.metadata?.cancelled ? "dirty" : result.failed?.length ? "error" : "done");
    if (!result.metadata?.cancelled && !result.failed?.length) {
      setDownstreamDirty(["world", "lorebook", "npc", "playtest"]);
    }
    await loadVisualAssetArtifacts();
    await loadVisualAssetRuns();
    const statusText = result.metadata?.cancelled ? "图片生成已停止" : "图片生成完成";
    setStatus(`${statusText}：${result.generated?.length || 0} 成功，${result.failed?.length || 0} 失败`);
    appendRunLog(result.metadata?.cancelled ? "cancelled" : result.failed?.length ? "error" : "done", "图片生成返回", `成功：${result.generated?.length || 0}\n失败：${result.failed?.length || 0}`);
    renderAll();
  });
}

async function pollVisualAssetGenerationJob(jobId) {
  while (true) {
    const job = await requestJson(`/api/worlds/visual-assets/generate/jobs/${encodeURIComponent(jobId)}`);
    renderJobEvents(job);
    if (job.status === "done" || job.status === "cancelled") {
      state.currentJobId = null;
      state.currentJobKind = "";
      state.cancelRequested = false;
      updateCancelButton();
      if (!job.result && job.status === "cancelled") {
        return {
          plan: state.visualPlan,
          generated: [],
          failed: [],
          metadata: { status: "cancelled", cancelled: true },
        };
      }
      if (!job.result) throw new Error("图片生成任务没有返回结果。");
      return job.result;
    }
    if (job.status === "error") {
      state.currentJobId = null;
      state.currentJobKind = "";
      state.cancelRequested = false;
      updateCancelButton();
      throw new Error(job.error?.message || "图片生成任务失败。");
    }
    if (job.status === "cancelling") {
      setRunningDetail("正在停止图片生成", "当前图片请求可能会先完成，后续资产不会继续生成。");
    }
    await sleep(1200);
  }
}

function renderJobEvents(job, seen = null) {
  const eventSeen = seen || renderJobEvents.seen || (renderJobEvents.seen = new Set());
  for (const event of job.events || []) {
    const key = `${job.job_id || ""}|${event.at}|${event.title}|${event.detail}`;
    if (eventSeen.has(key)) continue;
    eventSeen.add(key);
    appendRunLog(event.status || "running", event.title || "Job", event.detail || "");
  }
}

async function generateWorldFromDecomposition() {
  applyOptionalGraphEdit();
  const selectedGraphId = $("#world-script-graph-picker")?.value || "";
  if (selectedGraphId && selectedGraphId !== state.scriptGraphArtifactId) {
    await loadScriptGraphArtifact(selectedGraphId);
  }
  const source = resolveWorldGenerationSource();
  if (!source) {
    setStatus("需要先选择或载入可消费 JSON。世界生成页支持已保存故事图谱，也可读取当前编辑器中的图谱/拆解 JSON。", true);
    return;
  }
  const visualPlan = await ensureVisualPlanForWorldSource(source);
  const visualResult = await ensureVisualResultForWorldSource(source, visualPlan);
  await runTask("world", async () => {
    setRunningDetail("World API 正在生成可运行世界", "正在读取故事图谱生成 NPC、任务、动作和运行规则...");
    const worldLlm = buildLlmConfig("world");
    appendRunLog("running", "World API 生成世界", summarizeLlmConfig(worldLlm, "world"));
    appendRunLog(
      "running",
      "WorldBuilderAgent 输入",
      `${source.title || "未命名图谱"}\n故事图谱：${source.nodeCount} 节点 / ${source.edgeCount} 关系\n视觉计划：${visualPlan ? `${visualPlan.assets?.length || 0} 个资产` : "未带入"}\n图片批次：${visualResult ? `${visualResult.generated?.length || 0} 张 · ${visualResult.metadata?.generation_run_id || ""}` : "未指定"}`,
    );
    const payload = {
      template: source.kind,
      theme: source.title || "",
      player_name: state.decomposition?.player_name || $("#script-player").value || "主角",
      world_name: source.title || state.decomposition?.title || $("#script-title").value,
      script_graph: source.graph || null,
      script_decomposition: source.decomposition || null,
      visual_plan: visualPlan || null,
      visual_result: visualResult || null,
      world_builder_llm: worldLlm,
    };
    const world = await runCancellableRequest("WorldBuilderAgent", (signal) =>
      requestJson("/api/worlds/generate", { method: "POST", body: JSON.stringify(payload), signal }),
    );
    state.world = world;
    setStage("world", "done");
    setLorebookStageForWorld(world);
    setStage("npc", "dirty");
    setStage("playtest", "dirty");
    await loadWorlds();
    setStatus(`世界 API 已生成：${world.name || world.world_id}`);
    appendRunLog(
      "done",
      "世界生成完成",
      `world_id：${world.world_id}\nNPC：${world.npcs?.length || 0}\n任务：${world.tasks?.length || 0}\n动作：${world.actions?.length || 0}\n世界书：${hasWorldLorebook(world) ? `${getWorldLorebook(world).entries?.length || 0} 条` : "缺失"}`,
    );
    renderAll();
  });
}

async function saveWorldJson() {
  const value = parseJsonEditor("#world-json");
  if (!value) return;
  await runTask("world", async () => {
    setRunningDetail("正在保存世界", "保存后可进入 NPC 对话或试玩验证...");
    appendRunLog("running", "保存世界 JSON", `world_id：${value.world_id || "新建"}\nNPC：${value.npcs?.length || 0}\n任务：${value.tasks?.length || 0}`);
    const method = value.world_id ? "PUT" : "POST";
    const url = value.world_id ? `/api/worlds/${encodeURIComponent(value.world_id)}` : "/api/worlds";
    const world = await requestJson(url, { method, body: JSON.stringify(value) });
    state.world = world;
    setStage("world", "done");
    setLorebookStageForWorld(world);
    setStage("npc", "dirty");
    setStage("playtest", "dirty");
    await loadWorlds();
    setStatus(`已保存世界：${world.world_id}`);
    appendRunLog("done", "世界已保存", `world_id：${world.world_id}`);
    renderAll();
  });
}

async function startPlaytest() {
  const worldId = await ensurePlaytestWorldReady();
  if (!worldId) return;
  await runTask("playtest", async () => {
    setRunningDetail("正在启动试玩", "正在重置运行时 session...");
    appendRunLog("running", "启动试玩验证", `world_id：${worldId}`);
    const data = await requestJson(`/api/worlds/${encodeURIComponent(worldId)}/start`, { method: "POST" });
    addPlaytestLog("启动世界", data.narration || pretty(data));
    await refreshPlaytest(false);
    setStage("playtest", "done");
    setStatus("试玩已启动");
    appendRunLog("done", "试玩已启动", data.narration || "运行时 session 已初始化。");
  });
}

async function refreshPlaytest(useTaskWrapper = true) {
  const worldId = await ensurePlaytestWorldReady();
  if (!worldId) return;
  const task = async () => {
    appendRunLog("running", "刷新试玩状态", `world_id：${worldId}`);
    const snapshot = await requestJson(`/api/worlds/${encodeURIComponent(worldId)}/session`);
    state.playtestSnapshot = snapshot;
    setStage("playtest", "done");
    appendRunLog("done", "试玩状态已刷新", `当前位置：${snapshot.player?.location || "-"}\n附近 NPC：${snapshot.nearby_npcs?.length || 0}`);
    renderAll();
  };
  if (useTaskWrapper) {
    await runTask("playtest", task);
  } else {
    await task();
  }
}

async function playtestMove() {
  const location = $("#playtest-location").value;
  if (!location) {
    setStatus("没有可移动地点", true);
    return;
  }
  await runPlaytestAction({ action: "move_player", payload: { location } }, `移动到 ${location}`);
}

async function playtestInspect() {
  const location = state.playtestSnapshot?.player?.location || $("#playtest-location").value || state.world?.player?.location || "";
  await runPlaytestAction({ action: "inspect_location", payload: { location, query: "观察四周，寻找线索和可交互对象" } }, "观察/搜证");
}

async function playtestRunConfiguredAction() {
  const actionId = $("#playtest-action").value;
  if (!actionId) {
    setStatus("没有可执行动作", true);
    return;
  }
  await runPlaytestAction({ action: actionId, payload: { source: "pipeline_playtest" } }, `执行动作 ${actionId}`);
}

async function playtestRunCustomAction() {
  const payload = parseJsonEditor("#playtest-custom-action");
  if (!payload) return;
  await runPlaytestAction(payload, `执行 JSON ${payload.action || ""}`);
}

async function playtestSendChat() {
  const worldId = await ensurePlaytestWorldReady();
  const message = $("#playtest-chat-message").value.trim();
  if (!worldId) return;
  if (!message) {
    setStatus("请输入玩家发言", true);
    return;
  }
  await runTask("playtest", async () => {
    await ensurePlaytestSessionStarted(worldId);
    renderPlaytestNpcPicker();
    const target = $("#playtest-target-npc").value;
    const targetLabel = selectedPlaytestNpcLabel();
    const npcLlm = buildLlmConfig("npc");
    const payload = {
      message,
      player_name: playtestPlayerName(),
      location: playtestPlayerLocation(),
      target_npc_id: target,
      group_chat: false,
      npc_llm: npcLlm,
    };
    setRunningDetail("玩家发言中", `正在向 ${targetLabel} 发送对话...`);
    appendRunLog(
      "running",
      "试玩玩家发言",
      `目标：${targetLabel}\n玩家输入：${message}\n${summarizeLlmConfig(npcLlm, "npc")}`,
    );
    addPlaytestLog(`玩家 → ${targetLabel}`, message);
    const data = await requestJson(`/api/worlds/${encodeURIComponent(worldId)}/chat`, {
      method: "POST",
      body: JSON.stringify(payload),
    });
    addPlaytestLog("NPC 回复", summarizeNpcResponse(data));
    state.playtestSnapshot = normalizeChatAsPlaytestSnapshot(worldId, data);
    $("#playtest-chat-message").value = "";
    setStage("playtest", "done");
    setStatus("试玩对话已返回");
    appendRunLog("done", "试玩 NPC 对话返回", `${summarizeNpcResponse(data)}\n世界书激活：${summarizeActiveLorebookEntries(data)}`);
    renderAll();
  });
}

async function runPlaytestAction(payload, title) {
  const worldId = await ensurePlaytestWorldReady();
  if (!worldId) return;
  await runTask("playtest", async () => {
    setRunningDetail("正在执行试玩动作", title);
    appendRunLog("running", "执行试玩动作", `${title}\n${pretty(payload)}`);
    const data = await requestJson(`/api/worlds/${encodeURIComponent(worldId)}/action`, {
      method: "POST",
      body: JSON.stringify(payload),
    });
    addPlaytestLog(title, data.narration || pretty(data));
    state.playtestSnapshot = normalizeActionAsSnapshot(worldId, data);
    setStage("playtest", "done");
    setStatus(title);
    appendRunLog("done", "试玩动作返回", data.narration || pretty(data));
    renderAll();
  });
}

function normalizeActionAsSnapshot(worldId, actionResponse) {
  return {
    world_id: worldId,
    started: true,
    state: actionResponse.state || {},
    player: actionResponse.player || actionResponse.state?.player || {},
    active_entity: actionResponse.active_entity || null,
    speaker: actionResponse.speaker || null,
    npcs: actionResponse.npcs || actionResponse.state?.npcs || [],
    nearby_npcs: actionResponse.nearby_npcs || [],
    quest_progress: actionResponse.quest_progress || "",
    goals: state.playtestSnapshot?.goals || state.world?.story_goals || [],
    suggested_actions: actionResponse.suggested_actions || [],
  };
}

function normalizeChatAsPlaytestSnapshot(worldId, chatResponse) {
  const previous = state.playtestSnapshot || {};
  const previousState = previous.state || {};
  const player = chatResponse.player || previous.player || state.world?.player || {};
  return {
    world_id: worldId,
    started: true,
    state: { ...previousState, player },
    player,
    active_entity: chatResponse.active_entity || previous.active_entity || null,
    speaker: chatResponse.speaker || previous.speaker || null,
    npcs: chatResponse.npcs || previous.npcs || state.world?.npcs || [],
    nearby_npcs: chatResponse.nearby_npcs || previous.nearby_npcs || [],
    quest_progress: chatResponse.quest_progress || previous.quest_progress || "",
    goals: chatResponse.goals || previous.goals || state.world?.story_goals || [],
    suggested_actions: chatResponse.suggested_actions || previous.suggested_actions || [],
    messages: chatResponse.messages || previous.messages || [],
  };
}

async function ensurePlaytestWorldReady() {
  const pickedWorldId = $("#playtest-world-picker")?.value || "";
  if (pickedWorldId && state.world?.world_id !== pickedWorldId) {
    await loadWorldById(pickedWorldId, { hot: true });
  }
  const worldId = state.world?.world_id || pickedWorldId;
  if (!worldId) {
    setStatus("请选择试玩世界，或先在「世界生成」保存/载入世界", true);
    return "";
  }
  return worldId;
}

async function ensurePlaytestSessionStarted(worldId) {
  if (state.playtestSnapshot?.started && state.playtestSnapshot?.world_id === worldId) return;
  appendRunLog("running", "自动启动试玩 session", `world_id：${worldId}`);
  const data = await requestJson(`/api/worlds/${encodeURIComponent(worldId)}/start`, { method: "POST" });
  addPlaytestLog("启动世界", data.narration || "运行时 session 已初始化。");
  state.playtestSnapshot = await requestJson(`/api/worlds/${encodeURIComponent(worldId)}/session`);
}

function addPlaytestLog(title, text) {
  state.playtestLog = [...(state.playtestLog || []), { title, text, at: new Date().toISOString() }].slice(-80);
}

function playtestPlayerName() {
  return (
    $("#playtest-player-name").value.trim() ||
    state.playtestSnapshot?.player?.name ||
    state.world?.player?.name ||
    $("#script-player")?.value ||
    state.decomposition?.player_name ||
    "玩家"
  );
}

function playtestPlayerLocation() {
  return state.playtestSnapshot?.player?.location || state.world?.player?.location || $("#playtest-location").value || "";
}

function selectedPlaytestNpcLabel() {
  const picker = $("#playtest-target-npc");
  return picker?.selectedOptions?.[0]?.textContent || "自动选择 NPC";
}

function collectPlaytestNpcOptions() {
  const options = [];
  const seen = new Set();
  const pushNpc = (npc, source) => {
    const id = String(npc?.id || "").trim();
    if (!id || seen.has(id)) return;
    seen.add(id);
    options.push({
      id,
      name: npc?.name || id,
      location: npcLocationLabel(npc),
      source,
    });
  };
  for (const npc of state.playtestSnapshot?.nearby_npcs || []) pushNpc(npc, "nearby");
  for (const npc of state.world?.npcs || []) pushNpc(npc, "world");
  for (const npc of state.playtestSnapshot?.npcs || []) pushNpc(npc, "world");
  return options;
}

function collectWorldLocations() {
  const values = [];
  const playerLocation = state.playtestSnapshot?.player?.location || state.world?.player?.location;
  values.push(...splitLocationValues(playerLocation));
  for (const location of state.world?.locations || []) {
    if (typeof location === "string") values.push(...splitLocationValues(location));
    else if (location?.name) values.push(...splitLocationValues(location.name));
    else if (location?.id) values.push(...splitLocationValues(location.id));
  }
  for (const npc of state.world?.npcs || []) {
    values.push(...npcLocations(npc));
  }
  for (const action of state.world?.actions || []) {
    const location = action.effect?.set_player?.location;
    values.push(...splitLocationValues(location));
  }
  return [...new Set(values.filter(Boolean))];
}

function selectedScriptFiles() {
  const supported = new Set(["txt", "md", "markdown", "json", "docx", "pdf", "rtf", "html", "htm", "csv"]);
  const files = [
    ...Array.from($("#script-file").files || []),
    ...Array.from($("#script-folder").files || []),
  ];
  const deduped = new Map();
  for (const file of files) {
    const key = file.webkitRelativePath || file.name;
    if (!supported.has(fileExtension(file))) continue;
    deduped.set(key, file);
  }
  return Array.from(deduped.entries())
    .sort(([left], [right]) => left.localeCompare(right, "zh-Hans-CN", { numeric: true }))
    .map(([, file]) => file);
}

function updateSelectedFilesStatus() {
  const files = selectedScriptFiles();
  if (!files.length) {
    setStatus("未选择可导入文档");
    return;
  }
  const first = files[0].webkitRelativePath || files[0].name;
  const last = files[files.length - 1].webkitRelativePath || files[files.length - 1].name;
  setStatus(`已选择 ${files.length} 个可导入文档：${first}${files.length > 1 ? ` ... ${last}` : ""}`);
  setStage("script", "dirty");
}

function fileExtension(file) {
  return String(file.name || "").split(".").pop().toLowerCase();
}

function inferTitleFromFiles(files) {
  if (!files.length) return "多文件剧本";
  if (files.length === 1) return files[0].name.replace(/\.[^.]+$/, "");
  const firstPath = files[0].webkitRelativePath || files[0].name;
  const folder = firstPath.includes("/") ? firstPath.split("/")[0] : "";
  return folder || `${files[0].name.replace(/\.[^.]+$/, "")} 等 ${files.length} 个文件`;
}

async function startWorld() {
  const worldId = state.world?.world_id;
  if (!worldId) {
    setStatus("需要先保存或载入世界", true);
    return;
  }
  await runTask("npc", async () => {
    setRunningDetail("正在启动 NPC Runtime", "正在初始化世界状态和 NPC 会话...");
    appendRunLog("running", "启动 NPC Runtime", `world_id：${worldId}\n${formatLorebookRuntimeStatus(state.world)}`);
    const data = await requestJson(`/api/worlds/${encodeURIComponent(worldId)}/start`, { method: "POST" });
    appendNpcLog("系统", data.narration || pretty(data));
    setStage("npc", "done");
    setStatus("世界已启动");
    appendRunLog("done", "NPC Runtime 已启动", `${data.narration || "世界状态已初始化。"}\n${formatLorebookRuntimeStatus(state.world)}`);
    await loadNpcRuntimeContext(false);
  });
}

async function sendChat() {
  const worldId = state.world?.world_id;
  const message = $("#chat-message").value.trim();
  if (!worldId || !message) {
    setStatus("需要世界和玩家输入", true);
    return;
  }
  await runTask("npc", async () => {
    setRunningDetail("NPC Agent 正在回复", "正在调用 NPC API 并更新运行时状态...");
    appendNpcLog("玩家", message);
    const target = $("#target-npc").value;
    const npcLlm = buildLlmConfig("npc");
    appendRunLog(
      "running",
      "NPC Agent 调用",
      `目标：${target || "自动群聊（一轮）"}\n玩家输入：${message}\n${formatLorebookRuntimeStatus(state.world)}\n${summarizeLlmConfig(npcLlm, "npc")}`,
    );
    if (target) {
      const data = await requestNpcChat({ message, target, npcLlm, groupChat: false });
      appendNpcResponse(data);
      appendRunLog("done", "NPC Agent 返回", `${summarizeNpcResponse(data)}\n世界书激活：${summarizeActiveLorebookEntries(data)}`);
    } else {
      await runOneGroupChatRound(message, npcLlm);
    }
    await loadNpcRuntimeContext(false);
    $("#chat-message").value = "";
    setStage("npc", "done");
    setStatus(target ? "NPC 回复已返回" : "NPC 群聊一轮已完成");
  });
}

async function runOneGroupChatRound(playerMessage, npcLlm) {
  const turnMessages = [];
  const participants = shuffledGroupParticipants();
  if (!participants.length) {
    setStatus("当前位置没有可参与群聊的 NPC，请先移动到有 NPC 的地点", true);
    appendRunLog("error", "NPC 群聊未开始", `当前位置：${$("#chat-location").value || state.world?.player?.location || "-"}`);
    return;
  }
  for (const npc of participants) {
    const turnMessage = buildGroupTurnPrompt(playerMessage, turnMessages);
    appendRunLog("running", "NPC 群聊单轮发言", `目标：${npc.name || npc.id}\n已听到前序发言：${turnMessages.length}`);
    const data = await requestNpcChat({ message: turnMessage, target: npc.id, npcLlm, groupChat: false });
    appendNpcResponse(data);
    appendRunLog("done", "NPC 发言返回", `${summarizeNpcResponse(data)}\n世界书激活：${summarizeActiveLorebookEntries(data)}`);
    turnMessages.push(firstNpcMessage(data, npc));
  }
  await loadNpcRuntimeContext(false);
}

async function startContinuousGroupChat() {
  const worldId = state.world?.world_id;
  const firstMessage = $("#chat-message").value.trim();
  if (!worldId || state.groupChatRunning) return;
  const runId = ++groupChatRunId;
  state.groupChatRunning = true;
  renderGroupChatControls();
  try {
    await runTask("npc", async () => {
      setRunningDetail("NPC 群聊进行中", "会持续轮流发言，点击停止群聊后结束。");
      const npcLlm = buildLlmConfig("npc");
      let nextMessage = firstMessage || $("#player-goal").value || "你们继续围绕当前情况交流。";
      if (firstMessage) {
        appendNpcLog("玩家", firstMessage);
        $("#chat-message").value = "";
      }
      while (state.groupChatRunning) {
        const turnMessages = [];
        const participants = shuffledGroupParticipants();
        if (!participants.length) {
          appendRunLog("error", "NPC 群聊未开始", `当前位置没有可参与群聊的 NPC：${$("#chat-location").value || state.world?.player?.location || "-"}`);
          state.groupChatRunning = false;
          break;
        }
        for (const npc of participants) {
          if (!isActiveGroupChatRun(runId)) break;
          const turnMessage = buildGroupTurnPrompt(nextMessage, turnMessages);
          appendRunLog("running", "NPC 群聊发言", `目标：${npc.name || npc.id}\n${formatLorebookRuntimeStatus(state.world)}\n${summarizeLlmConfig(npcLlm, "npc")}`);
          const controller = new AbortController();
          groupChatAbortController = controller;
          let data;
          try {
            data = await requestNpcChat({ message: turnMessage, target: npc.id, npcLlm, groupChat: false, signal: controller.signal });
          } catch (error) {
            if (isCancellationError(error) && !isActiveGroupChatRun(runId)) {
              appendRunLog("cancelling", "NPC 群聊已停止", "当前 NPC 请求已终止。");
              break;
            }
            throw error;
          } finally {
            if (groupChatAbortController === controller) groupChatAbortController = null;
          }
          if (!isActiveGroupChatRun(runId)) break;
          appendNpcResponse(data);
          appendRunLog("done", "NPC 发言返回", `${summarizeNpcResponse(data)}\n世界书激活：${summarizeActiveLorebookEntries(data)}`);
          setStage("npc", "done");
          const message = firstNpcMessage(data, npc);
          turnMessages.push(message);
          await waitForGroupChatPace();
        }
        if (!isActiveGroupChatRun(runId)) break;
        nextMessage = buildGroupChatContinuationFromMessages(turnMessages);
        await loadNpcRuntimeContext(false);
      }
    });
  } finally {
    if (groupChatRunId === runId) {
      state.groupChatRunning = false;
      groupChatAbortController = null;
      renderGroupChatControls();
      setStatus("群聊已停止");
    }
  }
}

function stopContinuousGroupChat() {
  groupChatRunId += 1;
  state.groupChatRunning = false;
  if (groupChatAbortController) {
    groupChatAbortController.abort();
    groupChatAbortController = null;
  }
  renderGroupChatControls();
  appendRunLog("cancelling", "停止 NPC 群聊", "已停止循环，并终止当前正在等待的 NPC 请求。");
  setStatus("群聊已停止");
}

function isActiveGroupChatRun(runId) {
  return state.groupChatRunning && groupChatRunId === runId;
}

async function requestNpcChat({ message, target = "", npcLlm = buildLlmConfig("npc"), groupChat = false, signal = undefined }) {
  const worldId = state.world?.world_id;
  const payload = {
    message,
    player_name: $("#script-player").value || state.decomposition?.player_name || "玩家",
    location: $("#chat-location").value || state.world?.player?.location || "",
    player_goal: $("#player-goal").value || "",
    target_npc_id: target,
    group_chat: groupChat,
    npc_llm: npcLlm,
  };
  return requestJson(`/api/worlds/${encodeURIComponent(worldId)}/chat`, {
    method: "POST",
    body: JSON.stringify(payload),
    signal,
  });
}

function appendNpcResponse(data) {
  setLastNpcResponseJson(data);
  const messages = Array.isArray(data?.messages) ? data.messages : [];
  if (messages.length) {
    for (const message of messages) {
      const speaker = speakerName(message.speaker);
      appendNpcLog(speaker, message.content || "", npcLogLocationForSpeaker(speaker), npcPortraitForSpeaker(speaker, message.npc_id));
    }
    return;
  }
  const speaker = speakerName(data?.speaker);
  appendNpcLog(speaker, data?.reply || pretty(data), npcLogLocationForSpeaker(speaker), npcPortraitForSpeaker(speaker, data?.speaker?.id));
}

function summarizeNpcResponse(data) {
  const messages = Array.isArray(data?.messages) ? data.messages : [];
  if (messages.length) {
    return messages.map((message) => `${speakerName(message.speaker)}：${message.content || ""}`).join("\n");
  }
  return `${speakerName(data?.speaker)}：${data?.reply || pretty(data)}`;
}

function speakerName(speaker) {
  if (!speaker) return "NPC";
  if (typeof speaker === "string") return speaker;
  return speaker.name || speaker.id || "NPC";
}

function buildGroupChatContinuation(data) {
  const messages = Array.isArray(data?.messages) ? data.messages : [];
  const last = messages[messages.length - 1];
  const lastSpeaker = last?.speaker || "上一位";
  return `${lastSpeaker}说完后，其他在场 NPC 自然接话，继续讨论当前目标。`;
}

function shuffledGroupParticipants() {
  const location = $("#chat-location").value || state.world?.player?.location || "";
  const npcs = (state.world?.npcs || []).filter((npc) => !location || npcMatchesLocation(npc, location));
  return npcs
    .map((npc) => ({ ...npc, sortKey: Math.random() }))
    .sort((left, right) => left.sortKey - right.sortKey);
}

function buildGroupTurnPrompt(playerMessage, turnMessages) {
  const worldMessage = worldFacingNpcText(playerMessage);
  if (!turnMessages.length) {
    return `${worldMessage}\n\n这是群聊中的一次独立发言。你可以回应玩家，也可以选择沉默；如果沉默，只说“...”。不要提世界外的数据结构、开发工具、配置台或调试概念。`;
  }
  const transcript = turnMessages.map((message) => `${message.speaker}：${worldFacingNpcText(message.content)}`).join("\n");
  return `${worldMessage}\n\n本轮前面已经有人说过：\n${transcript}\n\n请你基于上面的发言自然接话。你可以补充、反驳、追问，也可以选择沉默；如果沉默，只说“...”。不要提世界外的数据结构、开发工具、配置台或调试概念。`;
}

function npcLocationLabel(npc) {
  return npcLocations(npc).join(" / ");
}

function npcMatchesLocation(npc, location) {
  return npcLocations(npc).includes(String(location || "").trim());
}

function splitLocationValues(value) {
  return String(value || "")
    .split(/[\/／|｜,，、;；]+/)
    .map((item) => item.trim())
    .filter(Boolean);
}

function firstLocationValue(value) {
  return splitLocationValues(value)[0] || "";
}

function npcLocations(npc) {
  if (Array.isArray(npc?.locations) && npc.locations.length) {
    return [...new Set(npc.locations.flatMap((item) => splitLocationValues(item)).filter(Boolean))];
  }
  return splitLocationValues(npc?.location);
}

function firstNpcMessage(data, fallbackNpc = {}) {
  const messages = Array.isArray(data?.messages) ? data.messages : [];
  const first = messages[0];
  return {
    speaker: speakerName(first?.speaker || data?.speaker || fallbackNpc.name || fallbackNpc.id),
    content: first?.content || data?.reply || "...",
  };
}

function buildGroupChatContinuationFromMessages(messages) {
  if (!messages.length) return "继续围绕当前情况自然交流。";
  const transcript = messages.map((message) => `${message.speaker}：${worldFacingNpcText(message.content)}`).join("\n");
  return `上一轮群聊记录：\n${transcript}\n\n请在场 NPC 继续自然交流。`;
}

function worldFacingNpcText(text) {
  const replacements = [
    ["故事图谱", "线索记录"],
    ["剧本图谱", "线索记录"],
    ["图谱", "线索记录"],
    ["ScriptGraphDocument", "线索记录"],
    ["script_graph", "线索记录"],
    ["story graph", "线索记录"],
    ["story_graph", "线索记录"],
    ["WorldTree", "传闻脉络"],
    ["world_tree", "传闻脉络"],
    ["世界树", "传闻脉络"],
    ["JSON", "记录"],
    ["json", "记录"],
    ["节点", "线索"],
    ["边", "关联"],
    ["开发者", "外人"],
    ["测试台", "记录册"],
    ["后台配置", "既有规矩"],
  ];
  return replacements.reduce((value, [source, target]) => value.replaceAll(source, target), String(text || "")).trim();
}

function waitForGroupChatPace() {
  return new Promise((resolve) => window.setTimeout(resolve, 600));
}

function renderGroupChatControls() {
  const start = $("#start-group-chat");
  const stop = $("#stop-group-chat");
  const send = $("#send-chat");
  if (!start || !stop) return;
  start.disabled = state.groupChatRunning || !state.world;
  stop.disabled = !state.groupChatRunning;
  if (send) send.disabled = state.groupChatRunning;
}

async function tickAgent() {
  const worldId = state.world?.world_id;
  if (!worldId) {
    setStatus("需要先保存或载入世界", true);
    return;
  }
  await runTask("npc", async () => {
    setRunningDetail("Autonomous Tick 正在运行", "正在执行一个自动推进步骤...");
    appendRunLog("running", "Autonomous Tick", `world_id：${worldId}\nobjective：${$("#player-goal").value || "-"}\n${formatLorebookRuntimeStatus(state.world)}`);
    const data = await requestJson(`/api/worlds/${encodeURIComponent(worldId)}/agent/tick`, {
      method: "POST",
      body: JSON.stringify({ max_steps: 1, objective: $("#player-goal").value || "" }),
    });
    appendNpcLog("Tick", pretty(data));
    setStage("npc", "done");
    appendRunLog("done", "Autonomous Tick 返回", pretty(data));
  });
}

function buildVisualRequest(options = {}) {
  const includePlan = options.includePlan ?? true;
  const includeStyleGuide = options.includeStyleGuide ?? true;
  const promptModel = buildLlmConfig("visual");
  const imageProvider = buildImageProviderConfig();
  return {
    script_graph: state.scriptGraph,
    plan: includePlan ? state.visualPlan : null,
    output_root: "output/visual_assets",
    provider: imageProvider,
    prompt_model: promptModel,
    prompt_composer: "agent",
    include_characters: $("#include-characters").checked,
    include_scenes: $("#include-scenes").checked,
    max_characters: nullableNumber($("#max-characters").value),
    max_scenes: nullableNumber($("#max-scenes").value),
    style_prompt: $("#style-prompt").value,
    style_guide: includeStyleGuide ? state.visualPlan?.metadata?.style_guide || {} : {},
  };
}

function buildImageProviderConfig() {
  if ($("#image-use-default-image").checked) {
    return imageConfigPayload({
      provider: $("#default-image-provider").value,
      baseUrl: $("#default-image-base-url").value.trim(),
      apiKey: $("#default-image-api-key").value.trim(),
      model: $("#default-image-model").value.trim(),
      size: $("#default-image-size").value.trim(),
      retry: $("#default-image-retry").value,
      seed: $("#default-image-seed").value,
      steps: $("#default-image-steps").value,
      cfgScale: $("#default-image-cfg-scale").value,
      textMode: $("#default-image-text-mode").checked,
    });
  }
  return imageConfigPayload({
    provider: $("#image-provider").value,
    baseUrl: $("#image-base-url").value.trim(),
    apiKey: $("#image-api-key").value.trim(),
    model: $("#image-model").value.trim(),
    size: $("#image-size").value.trim(),
    retry: $("#image-retry").value,
    seed: $("#image-seed").value,
    steps: $("#image-steps").value,
    cfgScale: $("#image-cfg-scale").value,
    textMode: $("#image-text-mode").checked,
  });
}

function selectedImageSizeLabel() {
  const select = $("#image-size");
  return select.selectedOptions?.[0]?.textContent || select.value || "1024x1024";
}

function buildLlmConfig(kind) {
  if (kind === "script") {
    if ($("#script-use-default-llm").checked) return null;
    const model = $("#script-model").value.trim();
    const baseUrl = $("#script-base-url").value.trim();
    const apiKey = $("#script-api-key").value.trim();
    return model || baseUrl || apiKey
      ? { provider: "openai_compatible", model, base_url: baseUrl, api_key: apiKey, timeout: 900, max_retries: 1 }
      : null;
  }
  if (kind === "world") {
    if ($("#world-use-default-llm").checked) return null;
    const model = $("#world-model").value.trim();
    const baseUrl = $("#world-base-url").value.trim();
    const apiKey = $("#world-api-key").value.trim();
    return model || baseUrl || apiKey
      ? { provider: "openai_compatible", model, base_url: baseUrl, api_key: apiKey, timeout: 900, max_retries: 1 }
      : null;
  }
  if (kind === "visual") {
    if ($("#visual-use-default-llm").checked) return null;
    const model = $("#visual-prompt-model").value.trim();
    const apiKey = $("#visual-prompt-api-key").value.trim();
    const baseUrl = $("#visual-prompt-base-url").value.trim();
    return model || apiKey || baseUrl
      ? { provider: "openai_compatible", model, base_url: baseUrl, api_key: apiKey, timeout: 120, max_retries: 0 }
      : null;
  }
  if (kind === "npc") {
    if ($("#npc-use-default-llm").checked) return null;
    const model = $("#npc-model").value.trim();
    const apiKey = $("#npc-api-key").value.trim();
    const baseUrl = $("#npc-base-url").value.trim();
    return model || apiKey || baseUrl
      ? { provider: "openai_compatible", model, base_url: baseUrl, api_key: apiKey, timeout: 90, max_retries: 0 }
      : null;
  }
  return null;
}

function decompositionToRequest(decomposition) {
  return {
    case_id: decomposition.script_id || decomposition.world_mapping?.world_id || "",
    title: decomposition.title || "",
    player_name: decomposition.player_name || "主角",
    public_background: decomposition.public_background || "",
    core_plot: decomposition.core_plot || "",
    hidden_threads: decomposition.hidden_threads || [],
    truth: decomposition.truth || "",
    timeline: decomposition.timeline || [],
    locations: decomposition.locations || [],
    forbidden_spoilers: decomposition.constraints || [],
    characters: (decomposition.characters || []).map((item) => ({
      id: item.id || "",
      name: item.name || "",
      role: item.role || "NPC",
      public_info: item.public_info || "",
      secret: item.secret || "",
      motive: item.motive || "",
      alibi: item.alibi || "",
      location: item.location || "",
    })),
    clues: (decomposition.clues || []).map((item) => ({
      id: item.id || "",
      title: item.title || "",
      content: item.content || "",
      source: item.source || "",
      location: item.location || "",
      owner: item.owner || "",
      reveals: item.reveals || "",
      trigger: item.trigger || "",
    })),
    endings: decomposition.endings || [],
  };
}

function applyOptionalDecompositionEdit() {
  const raw = $("#decomposition-json").value.trim();
  if (!raw) return;
  try {
    state.decomposition = JSON.parse(raw);
  } catch {
    return;
  }
}

function applyOptionalGraphEdit() {
  const raw = $("#script-graph-json").value.trim();
  if (!raw) return;
  try {
    const graph = JSON.parse(raw);
    if (Array.isArray(graph.nodes) && Array.isArray(graph.edges)) {
      state.scriptGraph = graph;
      state.scriptGraphArtifactId = "";
      resetGraphViewport();
    }
  } catch {
    return;
  }
}

function applyOptionalPlanEdit() {
  const raw = $("#visual-plan-json").value.trim();
  if (!raw) return;
  try {
    state.visualPlan = JSON.parse(raw);
  } catch {
    return;
  }
}

function setDownstreamDirty(stageIds) {
  for (const stage of stageIds) {
    if (state.stages[stage] === "done") state.stages[stage] = "dirty";
  }
}

async function runTask(stage, task) {
  const previousStage = state.stages[stage] || "idle";
  setStage(stage, "running");
  showRunBanner(stage, "运行中", "正在处理当前阶段...");
  setStatus(`运行中：${tabs.find((tab) => tab.id === stage)?.title || stage}`);
  appendRunLog("running", `开始：${tabs.find((tab) => tab.id === stage)?.title || stage}`, tabs.find((tab) => tab.id === stage)?.agent || stage);
  try {
    await task();
    appendRunLog("done", `完成：${tabs.find((tab) => tab.id === stage)?.title || stage}`, "本阶段任务已返回。");
    persistState();
  } catch (error) {
    const title = tabs.find((tab) => tab.id === stage)?.title || stage;
    if (isCancellationError(error)) {
      setStage(stage, previousStage === "running" ? "idle" : previousStage);
      setStatus(error.message || "当前任务已终止");
      appendRunLog("cancelling", `已终止：${title}`, error.message || "用户终止了当前任务。");
    } else {
      setStage(stage, "error");
      setStatus(error.message, true);
      appendRunLog("error", `失败：${title}`, error.message);
    }
  } finally {
    hideRunBanner();
  }
}

async function runCancellableRequest(label, task) {
  const controller = new AbortController();
  activeAbortController = controller;
  activeAbortLabel = label || "当前请求";
  state.cancelRequested = false;
  updateCancelButton();
  try {
    return await task(controller.signal);
  } catch (error) {
    if (isCancellationError(error)) {
      throw createCancellationError(`${activeAbortLabel} 已终止。`);
    }
    throw error;
  } finally {
    if (activeAbortController === controller) {
      activeAbortController = null;
      activeAbortLabel = "";
      state.cancelRequested = false;
      updateCancelButton();
    }
  }
}

function createCancellationError(message) {
  const error = new Error(message || "当前任务已终止。");
  error.name = "CancelledTaskError";
  return error;
}

function isCancellationError(error) {
  return error?.name === "AbortError" || error?.name === "CancelledTaskError";
}

function setStatus(message, isError = false) {
  $("#global-status").textContent = message;
  $("#global-status").style.color = isError ? "var(--bad)" : "var(--muted)";
}

function showRunBanner(stage, title, detail) {
  const banner = $("#run-banner");
  banner.hidden = false;
  document.body.classList.add("is-running");
  setBusyControls(true);
  $("#run-banner-title").textContent = title || "运行中";
  $("#run-banner-detail").textContent = detail || `正在处理：${tabs.find((tab) => tab.id === stage)?.title || stage}`;
}

function setRunningDetail(title, detail) {
  const banner = $("#run-banner");
  if (banner.hidden) {
    banner.hidden = false;
  }
  document.body.classList.add("is-running");
  setBusyControls(true);
  $("#run-banner-title").textContent = title || "运行中";
  $("#run-banner-detail").textContent = detail || "";
  setStatus(title || "运行中");
}

function hideRunBanner() {
  $("#run-banner").hidden = true;
  document.body.classList.remove("is-running");
  setBusyControls(false);
}

function setBusyControls(isBusy) {
  $$("button").forEach((button) => {
    if (button.id === "reset-state" || button.id === "clear-run-log" || button.id === "cancel-current-job") return;
    if (button.id === "stop-group-chat") return;
    if (button.classList.contains("tab-button")) return;
    if (button.classList.contains("reveal-secret")) return;
    button.disabled = isBusy;
  });
  renderGroupChatControls();
  updateCancelButton();
}

async function cancelCurrentJob() {
  if (state.cancelRequested) return;
  if (activeAbortController) {
    state.cancelRequested = true;
    updateCancelButton();
    appendRunLog("cancelling", "终止请求已发送", `请求：${activeAbortLabel || "当前请求"}`);
    activeAbortController.abort();
    setRunningDetail("正在终止当前请求", "浏览器已中断当前请求；如果后端已进入外部模型调用，当前 API 调用可能仍会在服务端完成。");
    return;
  }
  if (!state.currentJobId) return;
  state.cancelRequested = true;
  updateCancelButton();
  appendRunLog("cancelling", "终止请求已发送", `job_id：${state.currentJobId}`);
  try {
    if (state.currentJobKind === "visual_assets") {
      await requestJson(`/api/worlds/visual-assets/generate/jobs/${encodeURIComponent(state.currentJobId)}/cancel`, {
        method: "POST",
      });
      setRunningDetail("正在停止图片生成", "当前图片请求可能会先完成，后续资产不会继续生成。");
    } else {
      await requestJson(`/api/worlds/script-decomposition/import/jobs/${encodeURIComponent(state.currentJobId)}/cancel`, {
        method: "POST",
      });
      setRunningDetail("正在终止 ScriptDecompositionAgent", "后端已收到终止请求，正在停止当前任务...");
    }
  } catch (error) {
    state.cancelRequested = false;
    updateCancelButton();
    appendRunLog("error", "终止请求失败", error.message);
  }
}

function updateCancelButton() {
  const button = $("#cancel-current-job");
  if (!button) return;
  const canCancel = Boolean(state.currentJobId || activeAbortController);
  button.hidden = !canCancel;
  button.disabled = !canCancel || state.cancelRequested;
  const actionLabel = state.currentJobKind === "visual_assets" ? "停止生成" : "终止";
  button.textContent = state.cancelRequested ? `${actionLabel}中` : actionLabel;
}

function appendRunLog(status, title, detail = "") {
  const entry = {
    id: `${Date.now()}-${Math.random().toString(16).slice(2)}`,
    status,
    title,
    detail,
    at: new Date().toISOString(),
  };
  state.runLog = [...(state.runLog || []), entry].slice(-120);
  renderRunLog();
  persistState();
}

function appendDecompositionLog(data) {
  const report = data.report || {};
  const decomposition = data.decomposition || {};
  const llmError = decomposition.metadata?.llm_error || "";
  appendRunLog(
    report.passed ? "done" : "error",
    report.passed ? "拆解校验通过" : "拆解校验未通过",
    [
      `标题：${decomposition.title || data.world?.name || "-"}`,
      `图节点：${report.node_count ?? decomposition.story_graph?.entities?.length ?? 0}`,
      `图关系：${report.edge_count ?? decomposition.story_graph?.relations?.length ?? 0}`,
      `证据片段：${report.evidence_count ?? countStoryGraphEvidence(decomposition.story_graph)}`,
      `悬空引用：${(report.unresolved_references || []).join("、") || "无"}`,
      llmError ? `LLM 错误：${llmError}` : "",
    ]
      .filter(Boolean)
      .join("\n"),
  );
}

function summarizeLlmConfig(llmConfig, kind = "") {
  if (!llmConfig) return summarizeDefaultLlmConfig(kind);
  return [
    `模型：${llmConfig.model || "后端默认"}`,
    `base_url：${llmConfig.base_url || "后端默认"}`,
    `timeout：${llmConfig.timeout || "后端默认"}s`,
    `max_retries：${llmConfig.max_retries ?? "后端默认"}`,
  ].join("\n");
}

function summarizeDefaultLlmConfig(kind = "") {
  const agentId = {
    script: "script_decomposition",
    world: "world_builder",
    visual: "visual_prompt_composer",
    npc: "npc_runtime",
  }[kind];
  const effective = agentId ? state.effectiveConfig?.agents?.[agentId]?.effective_llm : null;
  if (!effective) return "模型：使用后端默认 LLM 配置";
  const keyText = effective.has_api_key ? "key 已配置" : "key 未配置";
  return [
    `模型：${effective.model || "后端默认"}`,
    `base_url：${effective.base_url || "后端默认"}`,
    `来源：默认 LLM / ${keyText}`,
  ].join("\n");
}

function renderRunLog() {
  const container = $("#agent-run-log");
  if (!container) return;
  const entries = state.runLog || [];
  if (!entries.length) {
    container.innerHTML = `<article class="run-log-entry"><time>--:--:--</time><div><strong>暂无运行日志</strong><p>启动任一 Agent 后，这里会显示可观察步骤和返回结果。</p></div></article>`;
    return;
  }
  container.innerHTML = entries
    .map((entry) => {
      const time = new Date(entry.at);
      const label = Number.isNaN(time.getTime()) ? "--:--:--" : time.toLocaleTimeString("zh-CN", { hour12: false });
      return `<article class="run-log-entry ${escapeAttribute(entry.status || "")}">
        <time>${escapeHtml(label)}</time>
        <div>
          <strong>${escapeHtml(entry.title || "")}</strong>
          <p>${escapeHtml(entry.detail || "")}</p>
        </div>
      </article>`;
    })
    .join("");
  container.scrollTop = container.scrollHeight;
}

function clearRunLog() {
  state.runLog = [];
  renderRunLog();
  persistState();
}

function appendNpcLog(speaker, content, location = "", portrait = null) {
  state.npcLog = [
    ...(state.npcLog || []),
    {
      speaker: speaker || "NPC",
      location: location || currentNpcLogLocation(),
      portrait: portrait || npcPortraitForSpeaker(speaker),
      content: content || "",
      at: new Date().toISOString(),
    },
  ].slice(-300);
  renderNpcLog();
  persistState();
}

function renderNpcLog() {
  const container = $("#npc-log");
  if (!container) return;
  const entries = state.npcLog || [];
  if (!entries.length) {
    container.innerHTML = `<article class="message"><strong>暂无对话</strong><p>启动世界或发送消息后，这里会保留 NPC 对话；点击清空才会删除。</p></article>`;
    return;
  }
  container.innerHTML = entries
    .map((entry) => {
      const time = new Date(entry.at);
      const label = Number.isNaN(time.getTime()) ? "" : time.toLocaleTimeString("zh-CN", { hour12: false });
      const location = entry.location || npcLogLocationForSpeaker(entry.speaker);
      const headerParts = [entry.speaker || "NPC", location, label].filter(Boolean);
      const portrait = entry.portrait?.url || entry.portrait?.output_path || "";
      const portraitLabel = String(entry.speaker || "NPC").trim().slice(0, 2) || "NPC";
      const image = portrait
        ? `<span class="npc-portrait"><img src="${escapeAttribute(outputPathToUrl(portrait))}" alt="${escapeAttribute(entry.speaker || "NPC")}" loading="lazy" onerror="this.remove(); this.parentElement.classList.add('is-missing');" /><span class="npc-portrait-fallback">${escapeHtml(portraitLabel)}</span></span>`
        : "";
      return `<article class="message npc-message">${image}<div><strong>${escapeHtml(headerParts.join(" · "))}</strong><p>${escapeHtml(entry.content || "")}</p></div></article>`;
    })
    .join("");
  container.scrollTop = container.scrollHeight;
}

function npcLogLocationForSpeaker(speaker) {
  const normalizedSpeaker = String(speaker || "").trim();
  const npc = (state.world?.npcs || []).find((item) => item.id === normalizedSpeaker || item.name === normalizedSpeaker);
  return npc?.location || currentNpcLogLocation();
}

function npcPortraitForSpeaker(speaker, npcId = "") {
  const normalizedSpeaker = String(speaker || "").trim();
  const normalizedId = String(npcId || "").trim();
  const npc = (state.world?.npcs || []).find((item) => item.id === normalizedId || item.id === normalizedSpeaker || item.name === normalizedSpeaker);
  return npc?.portrait || null;
}

function currentNpcLogLocation() {
  return $("#chat-location")?.value || state.world?.player?.location || "";
}

function clearNpcLog() {
  state.npcLog = [];
  state.lastNpcResponseJson = null;
  renderNpcLog();
  renderNpcResponseJson();
  persistState();
}

function setLastNpcResponseJson(data) {
  state.lastNpcResponseJson = data || null;
  renderNpcResponseJson();
  persistState();
}

function renderNpcResponseJson() {
  const editor = $("#npc-response-json");
  if (!editor) return;
  editor.value = state.lastNpcResponseJson ? pretty(state.lastNpcResponseJson) : "";
}

function renderSecretButtons() {
  $$(".reveal-secret").forEach((button) => {
    const input = document.getElementById(button.dataset.target);
    const isVisible = input?.type === "text";
    button.innerHTML = isVisible ? EYE_OFF_ICON : EYE_ICON;
    button.classList.toggle("is-visible", isVisible);
  });
}

function toggleSecretVisibility(event) {
  const button = event.currentTarget;
  const targetId = button.dataset.target;
  const input = document.getElementById(targetId);
  if (!input) return;
  input.type = input.type === "password" ? "text" : "password";
  renderSecretButtons();
}

function parseJsonEditor(selector) {
  try {
    return JSON.parse($(selector).value || "{}");
  } catch (error) {
    setStatus(`JSON 解析失败：${error.message}`, true);
    return null;
  }
}

function pretty(value) {
  return JSON.stringify(value, null, 2);
}

function summarizeNames(items = [], key) {
  return items
    .map((item) => item?.[key] || item?.id || "")
    .filter(Boolean)
    .slice(0, 12)
    .join("、");
}

function summarizeCounts(counts = {}) {
  return Object.entries(counts || {})
    .map(([key, value]) => `${key}: ${value}`)
    .join("\n");
}

function countStoryGraphEvidence(storyGraph = {}) {
  const entities = Array.isArray(storyGraph?.entities) ? storyGraph.entities : [];
  const relations = Array.isArray(storyGraph?.relations) ? storyGraph.relations : [];
  return [...entities, ...relations].reduce((total, item) => total + (Array.isArray(item?.evidence) ? item.evidence.length : 0), 0);
}

function outputPathToUrl(path) {
  if (!path) return "";
  let normalized = String(path).replaceAll("\\", "/");
  const marker = "/output/";
  const markerIndex = normalized.toLowerCase().indexOf(marker);
  if (markerIndex >= 0) normalized = normalized.slice(markerIndex + 1);
  normalized = normalized.replace(/^[A-Za-z]:\/.*?\/output\//, "output/");
  if (normalized.startsWith("output/")) return `/${normalized}`;
  if (normalized.startsWith("/output/")) return normalized;
  return normalized;
}

function nullableNumber(value) {
  const trimmed = String(value || "").trim();
  if (!trimmed) return null;
  const number = Number(trimmed);
  return Number.isFinite(number) ? number : null;
}

function nullableFloat(value) {
  const trimmed = String(value || "").trim();
  if (!trimmed) return null;
  const number = Number.parseFloat(trimmed);
  return Number.isFinite(number) ? number : null;
}

function numberOrDefault(value, fallback) {
  const number = Number(value);
  return Number.isFinite(number) ? number : fallback;
}

function sleep(ms) {
  return new Promise((resolve) => window.setTimeout(resolve, ms));
}

function formatApiError(text) {
  try {
    const data = JSON.parse(text);
    if (typeof data.detail === "string") return data.detail;
    if (data.detail?.message) {
      return `${data.detail.type || "Error"}: ${data.detail.message}`;
    }
    return JSON.stringify(data.detail || data, null, 2);
  } catch {
    return text;
  }
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function escapeAttribute(value) {
  return escapeHtml(value).replaceAll("`", "&#096;");
}

function bindConfigAutosave() {
  const panel = $(".config-panel");
  if (!panel) return;
  panel.addEventListener("input", autoSaveConfigFromForm);
  panel.addEventListener("change", autoSaveConfigFromForm);
}

function autoSaveConfigFromForm() {
  saveConfigFromForm({ silent: true, debounce: true });
}

function configFromForm() {
  return {
    defaultLlmBaseUrl: $("#default-llm-base-url").value.trim(),
    defaultLlmApiKey: $("#default-llm-api-key").value.trim(),
    defaultLlmModel: $("#default-llm-model").value.trim(),
    defaultImageProvider: $("#default-image-provider").value,
    defaultImageBaseUrl: $("#default-image-base-url").value.trim(),
    defaultImageApiKey: $("#default-image-api-key").value.trim(),
    defaultImageModel: $("#default-image-model").value.trim(),
    defaultImageSize: $("#default-image-size").value.trim(),
    defaultImageRetry: numberOrDefault($("#default-image-retry").value, 3),
    defaultImageSeed: $("#default-image-seed").value.trim(),
    defaultImageSteps: nullableNumber($("#default-image-steps").value),
    defaultImageCfgScale: nullableFloat($("#default-image-cfg-scale").value),
    defaultImageTextMode: $("#default-image-text-mode").checked,
    scriptUseDefaultLlm: $("#script-use-default-llm").checked,
    scriptBaseUrl: $("#script-base-url").value.trim(),
    scriptApiKey: $("#script-api-key").value.trim(),
    scriptModel: $("#script-model").value.trim(),
    worldUseDefaultLlm: $("#world-use-default-llm").checked,
    worldBaseUrl: $("#world-base-url").value.trim(),
    worldApiKey: $("#world-api-key").value.trim(),
    worldModel: $("#world-model").value.trim(),
    visualUseDefaultLlm: $("#visual-use-default-llm").checked,
    visualPromptBaseUrl: $("#visual-prompt-base-url").value.trim(),
    visualPromptApiKey: $("#visual-prompt-api-key").value.trim(),
    visualPromptModel: $("#visual-prompt-model").value.trim(),
    imageUseDefaultImage: $("#image-use-default-image").checked,
    imageProvider: $("#image-provider").value,
    imageBaseUrl: $("#image-base-url").value.trim(),
    imageApiKey: $("#image-api-key").value.trim(),
    imageModel: $("#image-model").value.trim(),
    imageSize: $("#image-size").value.trim(),
    imageRetry: numberOrDefault($("#image-retry").value, 3),
    imageSeed: $("#image-seed").value.trim(),
    imageSteps: nullableNumber($("#image-steps").value),
    imageCfgScale: nullableFloat($("#image-cfg-scale").value),
    imageTextMode: $("#image-text-mode").checked,
    npcUseDefaultLlm: $("#npc-use-default-llm").checked,
    npcBaseUrl: $("#npc-base-url").value.trim(),
    npcApiKey: $("#npc-api-key").value.trim(),
    npcModel: $("#npc-model").value.trim(),
  };
}

async function saveConfigFromForm(options = {}) {
  config = configFromForm();
  localStorage.setItem(CONFIG_KEY, JSON.stringify(config));
  if (options.debounce) {
    window.clearTimeout(configSaveTimer);
    configSaveTimer = window.setTimeout(() => saveConfigFromForm({ silent: true }), 500);
    return;
  }
  try {
    const data = await requestJson("/api/config", {
      method: "PUT",
      body: JSON.stringify(configToBackendPayload(config)),
    });
    state.effectiveConfig = data;
    renderEffectiveConfig(data);
    persistState();
  } catch (error) {
    if (!options.silent) {
      setStatus(`配置保存失败：${error.message}`, true);
    }
    return;
  }
  if (!options.silent) {
    setStatus("配置已保存到后端");
  }
}

function configToBackendPayload(value) {
  return {
    defaults: {
      llm: llmConfigPayload(value.defaultLlmModel, value.defaultLlmBaseUrl, value.defaultLlmApiKey),
      image: imageConfigPayload({
        provider: value.defaultImageProvider,
        baseUrl: value.defaultImageBaseUrl,
        apiKey: value.defaultImageApiKey,
        model: value.defaultImageModel,
        size: value.defaultImageSize,
        retry: value.defaultImageRetry,
        seed: value.defaultImageSeed,
        steps: value.defaultImageSteps,
        cfgScale: value.defaultImageCfgScale,
        textMode: value.defaultImageTextMode,
      }),
    },
    agents: {
      script_decomposition: {
        use_default_llm: Boolean(value.scriptUseDefaultLlm),
        llm: llmConfigPayload(value.scriptModel, value.scriptBaseUrl, value.scriptApiKey),
      },
      world_builder: {
        use_default_llm: Boolean(value.worldUseDefaultLlm),
        llm: llmConfigPayload(value.worldModel, value.worldBaseUrl, value.worldApiKey),
      },
      visual_prompt_composer: {
        use_default_llm: Boolean(value.visualUseDefaultLlm),
        llm: llmConfigPayload(value.visualPromptModel, value.visualPromptBaseUrl, value.visualPromptApiKey),
      },
      visual_asset_generation: {
        use_default_image: Boolean(value.imageUseDefaultImage),
        image: imageConfigPayload({
          provider: value.imageProvider,
          baseUrl: value.imageBaseUrl,
          apiKey: value.imageApiKey,
          model: value.imageModel,
          size: value.imageSize,
          retry: value.imageRetry,
          seed: value.imageSeed,
          steps: value.imageSteps,
          cfgScale: value.imageCfgScale,
          textMode: value.imageTextMode,
        }),
      },
      npc_runtime: {
        use_default_llm: Boolean(value.npcUseDefaultLlm),
        llm: llmConfigPayload(value.npcModel, value.npcBaseUrl, value.npcApiKey),
      },
    },
  };
}

function llmConfigPayload(model, baseUrl, apiKey) {
  return {
    provider: "openai_compatible",
    model: model || "",
    base_url: baseUrl || "",
    api_key: apiKey || "",
  };
}

function imageConfigPayload(value) {
  const provider = value.provider || "stepfun";
  return {
    provider,
    api_base_url: value.baseUrl || "",
    model: value.model || "",
    size: value.size || "1024x1024",
    api_key: value.apiKey || "",
    api_key_env: provider === "stepfun" ? "STEPFUN_API_KEY" : "",
    retry_count: numberOrDefault(value.retry, 3),
    seed: nullableNumber(value.seed),
    steps: nullableNumber(value.steps),
    cfg_scale: nullableFloat(value.cfgScale),
    text_mode: Boolean(value.textMode),
    response_format: "b64_json",
  };
}

function fillConfigForm() {
  $("#default-llm-base-url").value = config.defaultLlmBaseUrl || "";
  $("#default-llm-api-key").value = config.defaultLlmApiKey || "";
  $("#default-llm-model").value = config.defaultLlmModel || "";
  $("#default-image-provider").value = config.defaultImageProvider || "stepfun";
  $("#default-image-base-url").value = config.defaultImageBaseUrl || "";
  $("#default-image-api-key").value = config.defaultImageApiKey || "";
  syncImageProviderControls({
    providerSelector: "#default-image-provider",
    modelSelector: "#default-image-model",
    sizeSelector: "#default-image-size",
    preferredModel: config.defaultImageModel || "step-image-edit-2",
    preferredSize: config.defaultImageSize || "1024x1024",
  });
  $("#default-image-retry").value = String(config.defaultImageRetry ?? 3);
  $("#default-image-seed").value = config.defaultImageSeed || "";
  $("#default-image-steps").value = String(config.defaultImageSteps ?? 8);
  $("#default-image-cfg-scale").value = String(config.defaultImageCfgScale ?? 1);
  $("#default-image-text-mode").checked = Boolean(config.defaultImageTextMode);
  $("#script-use-default-llm").checked = config.scriptUseDefaultLlm !== false;
  $("#script-base-url").value = config.scriptBaseUrl || "";
  $("#script-api-key").value = config.scriptApiKey || "";
  $("#script-model").value = config.scriptModel || "";
  $("#world-use-default-llm").checked = config.worldUseDefaultLlm !== false;
  $("#world-base-url").value = config.worldBaseUrl || "";
  $("#world-api-key").value = config.worldApiKey || "";
  $("#world-model").value = config.worldModel || "";
  $("#visual-use-default-llm").checked = config.visualUseDefaultLlm !== false;
  $("#visual-prompt-base-url").value = config.visualPromptBaseUrl || "";
  $("#visual-prompt-api-key").value = config.visualPromptApiKey || "";
  $("#visual-prompt-model").value = config.visualPromptModel || "";
  $("#image-use-default-image").checked = config.imageUseDefaultImage !== false;
  $("#image-provider").value = config.imageProvider || "stepfun";
  $("#image-base-url").value = config.imageBaseUrl || "";
  $("#image-api-key").value = config.imageApiKey || "";
  syncImageProviderControls(config.imageModel || "step-image-edit-2", config.imageSize || "1024x1024");
  $("#image-retry").value = String(config.imageRetry ?? 3);
  $("#image-seed").value = config.imageSeed || "";
  $("#image-steps").value = String(config.imageSteps ?? 8);
  $("#image-cfg-scale").value = String(config.imageCfgScale ?? 1);
  $("#image-text-mode").checked = Boolean(config.imageTextMode);
  $("#npc-use-default-llm").checked = config.npcUseDefaultLlm !== false;
  $("#npc-base-url").value = config.npcBaseUrl || "";
  $("#npc-api-key").value = config.npcApiKey || "";
  $("#npc-model").value = config.npcModel || "";
  syncAgentConfigVisibility();
}

function syncAgentConfigVisibility() {
  const mapping = [
    ["#script-use-default-llm", '[data-agent-fields="script"]'],
    ["#world-use-default-llm", '[data-agent-fields="world"]'],
    ["#visual-use-default-llm", '[data-agent-fields="visual"]'],
    ["#image-use-default-image", '[data-agent-fields="image"]'],
    ["#npc-use-default-llm", '[data-agent-fields="npc"]'],
  ];
  for (const [checkboxSelector, fieldsSelector] of mapping) {
    const checkbox = $(checkboxSelector);
    const fields = $(fieldsSelector);
    if (!checkbox || !fields) continue;
    fields.classList.toggle("is-inherited", checkbox.checked);
  }
}

function restoreConfig() {
  try {
    config = normalizeLocalConfig({ ...defaultConfig, ...JSON.parse(localStorage.getItem(CONFIG_KEY) || "{}") });
  } catch {
    config = { ...defaultConfig };
  }
}

function normalizeLocalConfig(value) {
  const next = { ...defaultConfig, ...value };
  const hasNewDefaults =
    Boolean(next.defaultLlmBaseUrl || next.defaultLlmApiKey || next.defaultLlmModel || next.defaultImageApiKey) ||
    value.scriptUseDefaultLlm !== undefined ||
    value.worldUseDefaultLlm !== undefined;
  if (hasNewDefaults) return next;
  if (next.worldBaseUrl || next.worldApiKey || next.worldModel) {
    next.worldUseDefaultLlm = false;
    next.scriptUseDefaultLlm = false;
    next.scriptBaseUrl = next.worldBaseUrl;
    next.scriptApiKey = next.worldApiKey;
    next.scriptModel = next.worldModel;
  }
  if (next.visualPromptBaseUrl || next.visualPromptApiKey || next.visualPromptModel) {
    next.visualUseDefaultLlm = false;
  }
  if (next.npcBaseUrl || next.npcApiKey || next.npcModel) {
    next.npcUseDefaultLlm = false;
  }
  if (next.imageBaseUrl || next.imageApiKey || next.imageModel) {
    next.imageUseDefaultImage = false;
  }
  return next;
}

async function migrateLocalConfigToBackend() {
  const raw = localStorage.getItem(CONFIG_KEY);
  if (!raw || localStorage.getItem(CONFIG_MIGRATED_KEY)) return;
  try {
    const saved = normalizeLocalConfig({ ...defaultConfig, ...JSON.parse(raw) });
    await requestJson("/api/config", {
      method: "PUT",
      body: JSON.stringify(configToBackendPayload(saved)),
    });
    localStorage.setItem(CONFIG_MIGRATED_KEY, "1");
  } catch (error) {
    setStatus(`本地配置迁移失败：${error.message}`, true);
  }
}

function persistState() {
  const payload = {
    activeTab: state.activeTab,
    stages: state.stages,
    sourceText: state.sourceText,
    decompositionResponse: state.decompositionResponse,
    decomposition: state.decomposition,
    decompositionArtifactId: state.decompositionArtifactId,
    scriptGraph: state.scriptGraph,
    scriptGraphArtifactId: state.scriptGraphArtifactId,
    graphViewport: state.graphViewport,
    graphNodeOverrides: state.graphNodeOverrides,
    report: state.report,
    world: state.world,
    visualPlan: state.visualPlan,
    imageResult: state.imageResult,
    selectedImageAssetId: state.selectedImageAssetId,
    visualAssetArtifactId: state.visualAssetArtifactId,
    visualAssetArtifacts: state.visualAssetArtifacts,
    playtestSnapshot: state.playtestSnapshot,
    playtestLog: state.playtestLog,
    npcLog: state.npcLog,
    lastNpcResponseJson: state.lastNpcResponseJson,
    npcRuntimeSnapshot: state.npcRuntimeSnapshot,
    selectedNpcRuntimeId: state.selectedNpcRuntimeId,
    effectiveConfig: state.effectiveConfig,
  };
  localStorage.setItem(STORAGE_KEY, JSON.stringify(payload));
}

function restoreState() {
  try {
    const restored = JSON.parse(localStorage.getItem(STORAGE_KEY) || "{}");
    Object.assign(state, restored);
    state.stages = { ...stageDefaults, ...(restored.stages || {}) };
    state.visualAssetArtifacts = restored.visualAssetArtifacts || [];
    state.decompositionArtifactId = restored.decompositionArtifactId || "";
    state.graphViewport = restored.graphViewport || { scale: 1, x: 0, y: 0 };
    state.graphNodeOverrides = restored.graphNodeOverrides || {};
    state.npcLog = Array.isArray(restored.npcLog) ? restored.npcLog : [];
    state.lastNpcResponseJson = restored.lastNpcResponseJson || null;
    state.npcRuntimeSnapshot = restored.npcRuntimeSnapshot || null;
    state.selectedNpcRuntimeId = restored.selectedNpcRuntimeId || "";
    state.groupChatRunning = false;
  } catch {
    state.stages = { ...stageDefaults };
    state.npcLog = [];
    state.lastNpcResponseJson = null;
    state.npcRuntimeSnapshot = null;
    state.selectedNpcRuntimeId = "";
    state.groupChatRunning = false;
  }
}

function resetState() {
  if (!window.confirm("清空当前调试台状态？已保存的世界不会删除。")) return;
  localStorage.removeItem(STORAGE_KEY);
  Object.assign(state, {
    activeTab: "script",
    stages: { ...stageDefaults },
    sourceText: "",
    decompositionResponse: null,
    decomposition: null,
    decompositionArtifactId: "",
    scriptGraph: null,
    scriptGraphArtifactId: "",
    graphViewport: { scale: 1, x: 0, y: 0 },
    graphNodeOverrides: {},
    report: null,
    world: null,
    visualPlan: null,
    imageResult: null,
    selectedImageAssetId: "",
    currentJobId: null,
    currentJobKind: "",
    cancelRequested: false,
    groupChatRunning: false,
    playtestSnapshot: null,
    playtestLog: [],
    npcLog: [],
    lastNpcResponseJson: null,
    npcRuntimeSnapshot: null,
    selectedNpcRuntimeId: "",
  });
  renderAll();
  setStatus("已重置");
}

init().catch((error) => {
  setStatus(`初始化失败：${error.message}`, true);
});
