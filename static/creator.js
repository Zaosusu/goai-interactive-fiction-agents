const initialRequestedWorldId = new URLSearchParams(window.location.search).get("world");
const RESERVED_PROJECT_NAMES = new Set(["未命名互动剧情", "尚未命名", "Untitled interactive story"]);

const state = {
  worlds: [],
  project: null,
  selectedNodeId: "",
  currentWorldId: "",
  artifactSourceWorldId: "",
  lastSavedProjectFingerprint: "",
  playtestStarted: false,
  playtestProjectFingerprint: "",
  playtestToastTimer: null,
  runtimeNpcs: [],
  creatorAgentHistory: [],
  creatorConversationMessages: [],
  creatorHistoryWorldId: "",
  creatorToolLogCount: 0,
  pendingChange: null,
  undoStack: [],
  redoStack: [],
  versions: [],
  editBaseline: null,
  creatorAgentRequestController: null,
  creatorAgentStatusTimer: null,
  creatorAgentStartedAt: 0,
  creatorAgentElapsed: 0,
  creatorTools: [],
  activeWorkflowRunId: "",
  workflowEventCount: 0,
  initialProjectLoading: Boolean(initialRequestedWorldId),
  projectLoadVersion: 0,
  creatorDockResize: null,
  dragging: null,
  panning: null,
  zoom: 1,
  canvasPanMargin: 3200,
};

const CANVAS_WIDTH = 2200;
const CANVAS_HEIGHT = 2600;
const INITIAL_CANVAS_PAN_MARGIN = 3200;

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => Array.from(document.querySelectorAll(selector));

function uid(prefix) {
  return `${prefix}_${Math.random().toString(16).slice(2, 8)}`;
}

function pretty(value) {
  return JSON.stringify(value ?? {}, null, 2);
}

function lines(text) {
  return String(text || "")
    .split("\n")
    .map((item) => item.trim())
    .filter(Boolean);
}

function safeJson(text, fallback, label = "JSON") {
  const source = String(text || "").trim();
  if (!source) return fallback;
  try {
    return JSON.parse(source);
  } catch (error) {
    throw new Error(`${label} 格式错误：${error.message}`);
  }
}

async function request(url, options = {}) {
  const response = await fetch(url, {
    headers: { "Content-Type": "application/json; charset=utf-8", ...(options.headers || {}) },
    ...options,
  });
  if (!response.ok) {
    const text = await response.text();
    throw new Error(text || `${response.status} ${response.statusText}`);
  }
  return response.status === 204 ? null : response.json();
}

function emptyProject() {
  return {
    version: "creator_graph.v1",
    world: {
      world_id: `creator_${Date.now()}`,
      name: "未命名互动剧情",
      lore: "玩家通过选择、对话和探索推进剧情。",
      player: {
        name: "玩家",
        location: "开场",
        stats: { money: 100000 },
        inventory: [],
      },
    },
    characters: [
      { id: "npc_1", name: "林薇薇", role: "NPC", personality: "温柔但有自己的目标。", location: "开场", portrait: "" },
    ],
    nodes: [
      {
        id: "start",
        type: "story",
        x: 120,
        y: 120,
        title: "开场",
        character: "npc_1",
        background: "",
        content: "夜色落在办公室的玻璃上，林薇薇看向你。",
        conditions: {},
        effects: {},
        next: "choice_1",
        choices: [],
      },
      {
        id: "choice_1",
        type: "choice",
        x: 420,
        y: 140,
        title: "怎么回答",
        character: "npc_1",
        background: "",
        content: "选择你的回应。",
        conditions: {},
        effects: {},
        next: "",
        choices: [
          {
            text: "大方一点，给她 99 块",
            next: "good_reply",
            effects: { increase_player: { "stats.money": -99, "favor.npc_1": 5 }, set_flags: { generous: true } },
            conditions: {},
          },
          {
            text: "先问清楚她想要什么",
            next: "careful_reply",
            effects: { increase_player: { "favor.npc_1": 1 }, set_flags: { careful: true } },
            conditions: {},
          },
        ],
      },
      {
        id: "good_reply",
        type: "story",
        x: 760,
        y: 70,
        title: "她笑了",
        character: "npc_1",
        background: "",
        content: "她接过钱，眼神比刚才柔和了一点。",
        conditions: {},
        effects: {},
        next: "ending_soft",
        choices: [],
      },
      {
        id: "careful_reply",
        type: "story",
        x: 760,
        y: 250,
        title: "她试探你",
        character: "npc_1",
        background: "",
        content: "她没有立刻回答，只是反问你觉得她值多少。",
        conditions: {},
        effects: {},
        next: "ending_soft",
        choices: [],
      },
      {
        id: "ending_soft",
        type: "ending",
        x: 1080,
        y: 160,
        title: "关系开始",
        character: "npc_1",
        background: "",
        content: "你们之间多了一条还不确定的线。",
        conditions: {},
        effects: {},
        next: "",
        choices: [],
      },
    ],
  };
}

function setProject(project, options = {}) {
  const normalized = normalizeClientProject(project);
  state.project = normalized;
  state.canvasPanMargin = INITIAL_CANVAS_PAN_MARGIN;
  state.playtestStarted = false;
  state.playtestProjectFingerprint = "";
  const requestedSelection = options.selectedNodeId || state.selectedNodeId;
  state.selectedNodeId = normalized.nodes.some((node) => node.id === requestedSelection)
    ? requestedSelection
    : normalized.nodes[0]?.id || "";
  if (options.resetHistory !== false) {
    state.undoStack = [];
    state.redoStack = [];
  }
  if (options.keepPending !== true) clearPendingChange();
  applyZoom();
  fillProjectForm(options);
  renderCharacters();
  renderNodeCanvas();
  renderNodeForm();
  updateCompiledPreview();
  updateHistoryControls();
  window.requestAnimationFrame(() => focusCanvasViewport(state.selectedNodeId));
}

function normalizeClientProject(project) {
  const result = cloneValue(project || emptyProject());
  result.world = result.world || {};
  result.world.player = result.world.player || { stats: {}, inventory: [] };
  result.world.player.stats = result.world.player.stats || {};
  result.world.player.inventory = result.world.player.inventory || [];
  result.characters = Array.isArray(result.characters) ? result.characters : [];
  result.nodes = Array.isArray(result.nodes) ? result.nodes : [];
  const usedChoiceIds = new Set();
  for (const node of result.nodes) {
    node.choices = Array.isArray(node.choices) ? node.choices : [];
    node.choices.forEach((choice, index) => {
      let choiceId = choice.id || `choice_${safeId(node.id)}_${index + 1}`;
      let suffix = 2;
      while (usedChoiceIds.has(choiceId)) {
        choiceId = `${choice.id || `choice_${safeId(node.id)}_${index + 1}`}_${suffix}`;
        suffix += 1;
      }
      choice.id = choiceId;
      usedChoiceIds.add(choiceId);
    });
  }
  return result;
}

function cloneValue(value) {
  return JSON.parse(JSON.stringify(value));
}

function projectFingerprint(project = state.project) {
  return JSON.stringify(project || {});
}

function isProjectDirty() {
  return Boolean(
    state.project
    && state.lastSavedProjectFingerprint
    && projectFingerprint() !== state.lastSavedProjectFingerprint
  );
}

function confirmDiscardUnsavedChanges() {
  return !isProjectDirty() || window.confirm("当前作品有尚未保存的修改。继续后这些修改会丢失，确定吗？");
}

function pushUndo(label) {
  pushUndoSnapshot(label, state.project, state.selectedNodeId);
}

function pushUndoSnapshot(label, project, selectedNodeId) {
  state.undoStack.push({ label, project: cloneValue(project), selectedNodeId });
  if (state.undoStack.length > 50) state.undoStack.shift();
  state.redoStack = [];
  updateHistoryControls();
}

function captureEditBaseline(label) {
  if (state.editBaseline) return;
  state.editBaseline = { label, project: cloneValue(state.project), selectedNodeId: state.selectedNodeId };
}

function commitEditBaseline() {
  const baseline = state.editBaseline;
  state.editBaseline = null;
  if (!baseline) return;
  if (JSON.stringify(baseline.project) === JSON.stringify(state.project)) return;
  pushUndoSnapshot(baseline.label, baseline.project, baseline.selectedNodeId);
}

function undoProject() {
  const snapshot = state.undoStack.pop();
  if (!snapshot) return;
  state.redoStack.push({ label: snapshot.label, project: cloneValue(state.project), selectedNodeId: state.selectedNodeId });
  setProject(snapshot.project, { resetHistory: false, selectedNodeId: snapshot.selectedNodeId });
  addCreatorToolLog("系统", `已撤销：${snapshot.label}`);
  updateHistoryControls();
}

function redoProject() {
  const snapshot = state.redoStack.pop();
  if (!snapshot) return;
  state.undoStack.push({ label: snapshot.label, project: cloneValue(state.project), selectedNodeId: state.selectedNodeId });
  setProject(snapshot.project, { resetHistory: false, selectedNodeId: snapshot.selectedNodeId });
  addCreatorToolLog("系统", `已重做：${snapshot.label}`);
  updateHistoryControls();
}

function updateHistoryControls() {
  const undo = $("#undo-project");
  const redo = $("#redo-project");
  if (undo) {
    undo.disabled = state.undoStack.length === 0;
    undo.title = state.undoStack.length ? `撤销：${state.undoStack.at(-1).label}` : "没有可撤销的修改";
  }
  if (redo) {
    redo.disabled = state.redoStack.length === 0;
    redo.title = state.redoStack.length ? `重做：${state.redoStack.at(-1).label}` : "没有可重做的修改";
  }
}

function fillProjectForm(options = {}) {
  const project = state.project;
  $("#world-id").value = project.world.world_id || "";
  $("#world-name").value = project.world.name || "";
  $("#project-name").value = project.world.name || "";
  $("#player-name").value = project.world.player?.name || "玩家";
  $("#player-location").value = project.world.player?.location || "开场";
  $("#world-lore").value = project.world.lore || "";
  const player = project.world.player || {};
  $("#player-stats-json").value = pretty(player.stats || {});
  $("#items-text").value = (player.inventory || []).map((item) => (typeof item === "string" ? item : item.name || item.id || "")).filter(Boolean).join("\n");
  $("#project-title").textContent = project.world.name || "未命名互动剧情";
  updateCurrentProjectContext(options);
}

function updateCurrentProjectContext(options = {}) {
  const name = state.project?.world?.name || "尚未命名（请填写）";
  const worldId = state.project?.world?.world_id || "尚未分配 ID";
  $("#current-project-name").textContent = name;
  $("#current-project-id").textContent = worldId;
  if (options.updateUrl !== false && state.project?.world?.world_id && window.location.pathname === "/creator") {
    const url = new URL(window.location.href);
    url.searchParams.set("world", state.project.world.world_id);
    window.history.replaceState({}, "", url);
  }
}

function syncProjectForm() {
  const project = state.project;
  project.world.world_id = $("#world-id").value.trim() || project.world.world_id || `creator_${Date.now()}`;
  project.world.name = $("#project-name").value;
  $("#world-name").value = project.world.name;
  project.world.lore = $("#world-lore").value.trim();
  project.world.player = {
    ...(project.world.player || {}),
    name: $("#player-name").value.trim() || "玩家",
    location: $("#player-location").value.trim() || "开场",
    stats: safeJson($("#player-stats-json").value, {}, "玩家属性 JSON"),
    inventory: lines($("#items-text").value).map((name) => ({ name, quantity: 1 })),
  };
  $("#project-title").textContent = project.world.name || "尚未命名";
  updateCurrentProjectContext();
}

function requireProjectName(action = "保存或发布") {
  const name = String(state.project?.world?.name || "").trim();
  if (!name || RESERVED_PROJECT_NAMES.has(name)) {
    setProjectActionStatus("error", "请先填写明确的作品名称", `当前名称为空或仍是占位名称，不能${action}。`);
    $("#project-name").focus();
    throw new Error(`作品名称不能为空或使用占位名称，请先填写明确名称再${action}。`);
  }
  state.project.world.name = name;
  $("#project-name").value = name;
  $("#world-name").value = name;
  updateCurrentProjectContext();
  return name;
}

function renderCharacters() {
  const list = $("#character-list");
  list.innerHTML = "";
  for (const character of state.project.characters) {
    const item = document.createElement("div");
    item.className = "entity-item";
    item.innerHTML = `
      <div class="entity-item-header">
        <strong>${escapeHtml(character.name || character.id)}</strong>
        <button type="button" data-delete-character="${escapeHtml(character.id)}">删除</button>
      </div>
      <label>ID<input data-character-field="id" data-character-id="${escapeHtml(character.id)}" value="${escapeAttr(character.id)}" /></label>
      <label>姓名<input data-character-field="name" data-character-id="${escapeHtml(character.id)}" value="${escapeAttr(character.name || "")}" /></label>
      <label>身份<input data-character-field="role" data-character-id="${escapeHtml(character.id)}" value="${escapeAttr(character.role || "")}" /></label>
      <label>地点<input data-character-field="location" data-character-id="${escapeHtml(character.id)}" value="${escapeAttr(character.location || "")}" /></label>
      <label>立绘<input data-character-field="portrait" data-character-id="${escapeHtml(character.id)}" value="${escapeAttr(character.portrait || "")}" /></label>
    `;
    list.appendChild(item);
  }
  renderCharacterOptions();
}

function renderCharacterOptions() {
  const selectedValue = $("#node-character").value;
  $("#node-character").innerHTML = `<option value="">旁白/系统</option>${state.project.characters
    .map((character) => `<option value="${escapeAttr(character.id)}">${escapeHtml(character.name || character.id)}</option>`)
    .join("")}`;
  $("#node-character").value = selectedValue;
}

function nodeById(id) {
  return state.project.nodes.find((node) => node.id === id);
}

function nodeTitle(id) {
  const node = nodeById(id);
  return node ? `${node.title || node.id} (${node.id})` : id || "无";
}

function renderNodeCanvas() {
  const canvas = $("#node-canvas");
  canvas.innerHTML = "";
  for (const node of state.project.nodes) {
    const el = document.createElement("div");
    el.className = `story-node ${node.type} ${node.id === state.selectedNodeId ? "selected" : ""}`;
    el.style.left = `${node.x || 80}px`;
    el.style.top = `${node.y || 80}px`;
    el.dataset.nodeId = node.id;
    const character = state.project.characters.find((item) => item.id === node.character);
    const optionCount = node.choices?.length || 0;
    el.innerHTML = `
      <div class="node-type-label">${escapeHtml(node.type)}</div>
      <h3>${escapeHtml(node.title || node.id)}</h3>
      <p>${escapeHtml((node.content || "").slice(0, 86))}${(node.content || "").length > 86 ? "..." : ""}</p>
      <div class="node-meta">
        <span>${escapeHtml(character?.name || "旁白")}</span>
        ${optionCount ? `<span>${optionCount} 选项</span>` : ""}
        ${node.next ? `<span>→ ${escapeHtml(nodeTitle(node.next).slice(0, 20))}</span>` : ""}
      </div>
    `;
    canvas.appendChild(el);
  }
  renderEdges();
}

function renderEdges() {
  const svg = $("#edge-layer");
  svg.innerHTML = "";
  for (const node of state.project.nodes) {
    if (node.next) drawEdge(node, node.next, false);
    for (const choice of node.choices || []) {
      if (choice.next) drawEdge(node, choice.next, true);
    }
  }
}

function drawEdge(sourceNode, targetId, isChoice) {
  const target = nodeById(targetId);
  if (!target) return;
  const x1 = (sourceNode.x || 0) + 105;
  const y1 = (sourceNode.y || 0) + 112;
  const x2 = (target.x || 0) + 105;
  const y2 = target.y || 0;
  const midY = (y1 + y2) / 2;
  const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
  path.setAttribute("class", `edge-line ${isChoice ? "choice-edge" : ""}`);
  path.setAttribute("d", `M ${x1} ${y1} C ${x1} ${midY}, ${x2} ${midY}, ${x2} ${y2}`);
  $("#edge-layer").appendChild(path);
}

function applyZoom() {
  const content = $("#canvas-content");
  const space = $("#canvas-space");
  if (!content || !space) return;
  content.style.transform = `scale(${state.zoom})`;
  content.style.left = `${state.canvasPanMargin * state.zoom}px`;
  content.style.top = `${state.canvasPanMargin * state.zoom}px`;
  space.style.width = `${(CANVAS_WIDTH + state.canvasPanMargin * 2) * state.zoom}px`;
  space.style.height = `${(CANVAS_HEIGHT + state.canvasPanMargin * 2) * state.zoom}px`;
}

function focusCanvasViewport(nodeId = "") {
  const panel = $(".canvas-panel");
  if (!panel) return;
  const node = nodeById(nodeId) || state.project?.nodes?.[0];
  const worldX = Number(node?.x || 80) + 105;
  const worldY = Number(node?.y || 80) + 90;
  panel.scrollLeft = (state.canvasPanMargin + worldX) * state.zoom - panel.clientWidth / 2;
  panel.scrollTop = (state.canvasPanMargin + worldY) * state.zoom - panel.clientHeight / 2;
}

function ensureCanvasPanRoom(panel) {
  const edgeRoom = Math.min(720, Math.max(240, Math.min(panel.clientWidth, panel.clientHeight) * 1.5));
  const nearEdge = panel.scrollLeft < edgeRoom ||
    panel.scrollTop < edgeRoom ||
    panel.scrollWidth - panel.clientWidth - panel.scrollLeft < edgeRoom ||
    panel.scrollHeight - panel.clientHeight - panel.scrollTop < edgeRoom;
  if (!nearEdge) return;
  const previousMargin = state.canvasPanMargin;
  state.canvasPanMargin += INITIAL_CANVAS_PAN_MARGIN;
  applyZoom();
  const offset = (state.canvasPanMargin - previousMargin) * state.zoom;
  panel.scrollLeft += offset;
  panel.scrollTop += offset;
}

function wheelPixels(event, panel) {
  if (event.deltaMode === WheelEvent.DOM_DELTA_LINE) return 24;
  if (event.deltaMode === WheelEvent.DOM_DELTA_PAGE) return panel.clientHeight;
  return 1;
}

function renderNodeForm() {
  const node = nodeById(state.selectedNodeId);
  $("#node-empty").hidden = Boolean(node);
  $("#node-form").hidden = !node;
  $("#delete-node").disabled = !node;
  if (!node) return;
  renderCharacterOptions();
  renderNodeOptions();
  $("#node-id").value = node.id;
  $("#node-title").value = node.title || "";
  $("#node-type").value = node.type || "story";
  $("#node-character").value = node.character || "";
  $("#node-background").value = node.background || "";
  $("#node-content").value = node.content || "";
  $("#node-conditions").value = pretty(node.conditions || {});
  $("#node-effects").value = pretty(node.effects || {});
  $("#node-next").value = node.next || "";
  renderChoiceList(node);
}

function renderNodeOptions() {
  const options = [`<option value="">无</option>`].concat(
    state.project.nodes.map((node) => `<option value="${escapeAttr(node.id)}">${escapeHtml(node.title || node.id)} (${escapeHtml(node.id)})</option>`)
  );
  $("#node-next").innerHTML = options.join("");
}

function renderChoiceList(node) {
  const list = $("#choice-list");
  list.innerHTML = "";
  for (const [index, choice] of (node.choices || []).entries()) {
    const item = document.createElement("div");
    item.className = "choice-item";
    item.innerHTML = `
      <div class="choice-item-header">
        <strong>选项 ${index + 1}</strong>
        <button type="button" data-delete-choice="${index}">删除</button>
      </div>
      <label>文本<input data-choice-field="text" data-choice-index="${index}" value="${escapeAttr(choice.text || "")}" /></label>
      <label>跳转<select data-choice-field="next" data-choice-index="${index}">${$("#node-next").innerHTML}</select></label>
      <label>显示条件 JSON<textarea data-choice-field="conditions" data-choice-index="${index}" rows="3">${escapeHtml(pretty(choice.conditions || {}))}</textarea></label>
      <label>效果 JSON<textarea data-choice-field="effects" data-choice-index="${index}" rows="4">${escapeHtml(pretty(choice.effects || {}))}</textarea></label>
    `;
    list.appendChild(item);
    item.querySelector('[data-choice-field="next"]').value = choice.next || "";
  }
}

function syncNodeForm() {
  const node = nodeById(state.selectedNodeId);
  if (!node) return;
  const oldId = node.id;
  const newId = $("#node-id").value.trim() || oldId;
  node.id = newId;
  node.title = $("#node-title").value.trim();
  node.type = $("#node-type").value;
  node.character = $("#node-character").value;
  node.background = $("#node-background").value.trim();
  node.content = $("#node-content").value;
  node.conditions = safeJson($("#node-conditions").value, {}, "进入条件 JSON");
  node.effects = safeJson($("#node-effects").value, {}, "节点效果 JSON");
  node.next = $("#node-next").value;
  if (newId !== oldId) {
    for (const candidate of state.project.nodes) {
      if (candidate.next === oldId) candidate.next = newId;
      for (const choice of candidate.choices || []) {
        if (choice.next === oldId) choice.next = newId;
      }
    }
    state.selectedNodeId = newId;
  }
}

function compileWorld() {
  syncProjectForm();
  syncNodeForm();
  const project = state.project;
  const startNode = project.nodes.find((node) => node.id === "start") || project.nodes[0];
  const player = {
    ...(project.world.player || {}),
    location: startNode?.title || project.world.player?.location || "开场",
  };
  const npcs = project.characters.map((character) => ({
    id: character.id,
    name: character.name || character.id,
    role: character.role || "NPC",
    personality: character.personality || "",
    location: character.location || player.location,
    portrait: character.portrait ? { image: character.portrait } : {},
  }));
  const actions = [];
  const tasks = [];
  for (const node of project.nodes) {
    const baseEffect = normalizeEffect({
      scene: node.content || node.title,
      set_player: { location: node.title || node.id },
      active_npc_id: node.character || undefined,
      ...node.effects,
    });
    if (node.next) {
      actions.push({
        id: `go_${node.id}_next`,
        label: `继续：${node.title || node.id}`,
        description: `从 ${node.id} 前往 ${node.next}`,
        effect: { ...baseEffect, set_flags: { ...(baseEffect.set_flags || {}), [`visited_${node.id}`]: true } },
      });
    }
    for (const [index, choice] of (node.choices || []).entries()) {
      actions.push({
        id: `choice_${node.id}_${index + 1}`,
        label: choice.text || `选择 ${index + 1}`,
        description: `节点 ${node.id} 的选项`,
        effect: normalizeEffect({
          scene: choice.text,
          set_flags: { [`choice_${node.id}_${index + 1}`]: true },
          ...choice.effects,
          set_player: { location: nodeTitle(choice.next), ...(choice.effects?.set_player || {}) },
          active_npc_id: node.character || undefined,
        }),
      });
    }
    if (node.type === "ending") {
      tasks.push({
        id: `ending_${node.id}`,
        title: node.title || node.id,
        description: node.content || "",
        status: "pending",
        completion: Object.keys(node.conditions || {}).length ? node.conditions : { flags: { [`visited_${node.id}`]: true } },
      });
    }
  }
  return {
    world_id: project.world.world_id,
    name: project.world.name,
    description: "由 Creator 节点编辑器编译生成。",
    lore: project.world.lore,
    opening_scene: startNode?.content || "故事开始。",
    player,
    npcs,
    story_goals: tasks.length ? tasks.map((task) => task.title) : ["推进互动剧情"],
    tasks,
    actions,
    initial_memories: [],
    metadata: {
      creator_graph: project,
      visual_bindings: {
        nodes: Object.fromEntries(project.nodes.map((node) => [node.id, { background: node.background || "" }])),
      },
    },
  };
}

function normalizeEffect(effect) {
  const clean = JSON.parse(JSON.stringify(effect || {}));
  for (const key of Object.keys(clean)) {
    if (clean[key] === undefined || clean[key] === "") delete clean[key];
  }
  return clean;
}

function updateCompiledPreview() {
  try {
    $("#compiled-json").value = pretty(compileWorld());
  } catch (error) {
    $("#compiled-json").value = error.message;
  }
}

function setProjectActionStatus(mode, title, detail = "") {
  const status = $("#project-action-status");
  status.hidden = false;
  status.className = `project-action-status ${mode || ""}`.trim();
  $("#project-action-title").textContent = title;
  $("#project-action-detail").textContent = detail;
}

function previewCompilation() {
  updateCompiledPreview();
  setProjectActionStatus("success", "编译预览已更新", `当前画布包含 ${state.project.nodes.length} 个节点、${state.project.characters.length} 位角色。`);
}

async function loadWorlds({ announce = false } = {}) {
  const previous = state.currentWorldId || $("#world-picker").value;
  state.worlds = await request("/api/worlds");
  $("#world-picker").innerHTML = state.worlds
    .map((world) => `<option value="${escapeAttr(world.world_id)}">${escapeHtml(world.name || "未命名互动剧情")} · ${escapeHtml(world.world_id)}</option>`)
    .join("");
  if (previous && state.worlds.some((world) => world.world_id === previous)) {
    $("#world-picker").value = previous;
  }
  if (announce) setProjectActionStatus("success", "项目列表已刷新", `共找到 ${state.worlds.length} 个已保存项目。`);
}

async function loadSelectedWorld() {
  const id = $("#world-picker").value;
  if (!id) {
    setProjectActionStatus("error", "没有可载入的项目", "请先新建并保存一个项目。 ");
    return;
  }
  if (id !== state.currentWorldId && !confirmDiscardUnsavedChanges()) {
    $("#world-picker").value = state.currentWorldId || "";
    return;
  }
  const loadVersion = ++state.projectLoadVersion;
  const button = $("#load-world");
  const original = button.textContent;
  button.disabled = true;
  button.textContent = "载入中…";
  setProjectActionStatus("working", "正在载入项目", id);
  try {
    const world = await request(`/api/worlds/${encodeURIComponent(id)}`);
    if (loadVersion !== state.projectLoadVersion) return;
    state.currentWorldId = world.world_id;
    state.artifactSourceWorldId = world.world_id;
    const graph = world.metadata?.creator_graph || worldToCreatorGraph(world);
    setProject(graph);
    state.lastSavedProjectFingerprint = projectFingerprint();
    await loadCreatorHistory();
    await loadCreatorVersions();
    await recoverLatestCreatorWorkflow();
    setProjectActionStatus("success", `已载入《${world.name || world.world_id}》`, `${state.project.nodes.length} 个剧情节点 · ${state.project.characters.length} 位角色 · ${world.world_id}`);
  } catch (error) {
    setProjectActionStatus("error", "项目载入失败", cleanApiError(error));
    throw error;
  } finally {
    button.disabled = false;
    button.textContent = original;
  }
}

function worldToCreatorGraph(world) {
  const actions = Array.isArray(world.actions) ? world.actions : [];
  const tasks = Array.isArray(world.tasks) ? world.tasks : [];
  const startChoices = actions.slice(0, 10).map((action) => ({
    text: action.label || action.id,
    next: nodeIdFromAction(action),
    effects: action.effect || {},
    conditions: action.conditions || {},
  }));
  const nodes = [
    {
      id: "start",
      type: "story",
      x: 120,
      y: 120,
      title: "开场",
      character: world.npcs?.[0]?.id || "",
      background: firstSceneBackground(world),
      content: world.opening_scene || world.description || world.lore || "",
      conditions: {},
      effects: {},
      next: actions[0] ? nodeIdFromAction(actions[0]) : "",
      choices: startChoices,
    },
  ];

  actions.forEach((action, index) => {
    const effect = action.effect && typeof action.effect === "object" ? action.effect : {};
    const nextAction = actions[index + 1];
    const completedTask = String(effect.complete_task || "");
    const taskNodeId = completedTask ? `task_${safeId(completedTask)}` : "";
    nodes.push({
      id: nodeIdFromAction(action),
      type: "story",
      x: 440 + (index % 4) * 280,
      y: 100 + Math.floor(index / 4) * 210,
      title: action.label || action.id,
      character: effect.active_npc_id || "",
      background: backgroundForAction(world, action),
      content: effect.scene || action.description || action.label || action.id,
      conditions: action.conditions || {},
      effects: effect,
      next: taskNodeId || (nextAction ? nodeIdFromAction(nextAction) : ""),
      choices: nextAction
        ? [
            {
              text: `继续：${nextAction.label || nextAction.id}`,
              next: nodeIdFromAction(nextAction),
              effects: {},
              conditions: {},
            },
          ]
        : [],
    });
  });

  tasks.forEach((task, index) => {
    const linkedAction = actions.find((action) => action.effect?.complete_task === task.id);
    nodes.push({
      id: `task_${safeId(task.id || `task_${index + 1}`)}`,
      type: "ending",
      x: 520 + (index % 3) * 320,
      y: 760 + Math.floor(index / 3) * 180,
      title: task.title || task.id || `任务 ${index + 1}`,
      character: linkedAction?.effect?.active_npc_id || "",
      background: backgroundForAction(world, linkedAction),
      content: task.description || task.title || "",
      conditions: task.completion || {},
      effects: linkedAction?.effect || {},
      next: "",
      choices: [],
    });
  });

  appendGraphNodesFromMetadata(world, nodes);
  return {
    version: "creator_graph.v1",
    world: {
      world_id: world.world_id,
      name: world.name,
      lore: world.lore || "",
      player: world.player || {},
    },
    characters: (world.npcs || []).map((npc) => ({
      id: npc.id,
      name: npc.name,
      role: npc.role || "NPC",
      personality: npc.personality || "",
      location: npc.location || "",
      portrait: npc.portrait?.image || "",
    })),
    nodes,
  };
}

function nodeIdFromAction(action) {
  return `action_${safeId(action?.id || action?.label || "world_action")}`;
}

function safeId(value) {
  return String(value || "node")
    .trim()
    .replace(/[^\w\u4e00-\u9fa5-]+/g, "_")
    .replace(/^_+|_+$/g, "")
    .slice(0, 80) || "node";
}

function firstSceneBackground(world) {
  const visual = world.metadata?.visual_plan || world.metadata?.visual_result;
  const assets = Array.isArray(visual?.assets) ? visual.assets : Array.isArray(visual?.plan?.assets) ? visual.plan.assets : [];
  const scene = assets.find((asset) => ["scene", "location", "background"].includes(String(asset.kind || asset.type || "").toLowerCase()));
  return scene?.output_path || scene?.path || scene?.image || "";
}

function backgroundForAction(world, action) {
  if (!action) return "";
  const location = action.effect?.set_player?.location || action.effect?.location || action.label || "";
  const visual = world.metadata?.visual_plan || world.metadata?.visual_result;
  const assets = Array.isArray(visual?.assets) ? visual.assets : Array.isArray(visual?.plan?.assets) ? visual.plan.assets : [];
  const scene = assets.find((asset) => {
    const haystack = `${asset.id || ""} ${asset.display_name || ""} ${asset.name || ""} ${asset.prompt || ""}`;
    return location && haystack.includes(location);
  });
  return scene?.output_path || scene?.path || scene?.image || "";
}

function appendGraphNodesFromMetadata(world, nodes) {
  const graphNodes = world.metadata?.script_graph?.nodes;
  if (!Array.isArray(graphNodes) || !graphNodes.length) return;
  const existing = new Set(nodes.map((node) => node.id));
  const important = graphNodes
    .filter((node) => ["event", "timeline_event", "scene", "clue", "secret", "truth", "ending"].includes(String(node.kind || "").toLowerCase()))
    .slice(0, 24);
  important.forEach((item, index) => {
    const id = `graph_${safeId(item.id || item.name || index + 1)}`;
    if (existing.has(id)) return;
    existing.add(id);
    nodes.push({
      id,
      type: String(item.kind || "").toLowerCase() === "ending" ? "ending" : "story",
      x: 1280 + (index % 3) * 270,
      y: 100 + Math.floor(index / 3) * 180,
      title: item.name || item.label || item.id || `图谱节点 ${index + 1}`,
      character: "",
      background: "",
      content: item.description || item.summary || JSON.stringify(item.properties || {}),
      conditions: {},
      effects: { scene: item.description || item.name || item.id },
      next: "",
      choices: [],
    });
  });
}

async function saveCreatorWorld() {
  syncProjectForm();
  syncNodeForm();
  requireProjectName("保存草稿");
  setProjectActionStatus("working", "正在保存草稿", "校验并编译当前 Creator Graph。 ");
  const result = await request("/api/creator/mcp/tools/call", {
    method: "POST",
    body: JSON.stringify({
      name: "save_world",
      arguments: {},
      project: state.project,
      artifacts: state.project.pipeline_artifacts || {},
    }),
  });
  if (result.isError) throw new Error(result.structuredContent?.error?.message || result.content?.[0]?.text || "保存草稿失败");
  const saved = result.structuredContent?.artifacts?.saved_world;
  if (!saved?.world_id) throw new Error("保存工具没有返回世界数据");
  state.currentWorldId = saved.world_id;
  addLog("系统", `草稿已保存：${saved.name}`);
  await loadWorlds();
  $("#world-picker").value = saved.world_id;
  try {
    await createCreatorVersion(`保存：${saved.name}`);
  } catch (error) {
    console.warn("creator snapshot failed", error);
  }
  state.lastSavedProjectFingerprint = projectFingerprint();
  setProjectActionStatus("success", `草稿已保存：《${saved.name}》`, `${state.project.nodes.length} 个剧情节点已写入世界库；尚未发布到玩家端。`);
  return saved;
}

async function saveAsNewWorld() {
  syncProjectForm();
  syncNodeForm();
  const name = requireProjectName("另存为新作品");
  const previousId = state.project.world.world_id;
  const previousCurrentWorldId = state.currentWorldId;
  const historyToCopy = cloneValue(state.creatorConversationMessages);
  const newId = `creator_${Date.now()}`;
  state.artifactSourceWorldId ||= previousId;
  state.project.world.world_id = newId;
  state.currentWorldId = newId;
  $("#world-id").value = newId;
  updateCurrentProjectContext();
  setProjectActionStatus("working", `正在创建《${name}》`, `将保存为独立作品 ${newId}，不会覆盖原项目。`);
  try {
    const saved = await saveCreatorWorld();
    await copyCreatorHistory(saved.world_id, historyToCopy);
    setProjectActionStatus("success", `已创建独立作品《${saved.name}》`, `${saved.world_id} · 已保存为草稿；点击“发布并打开玩家端”即可消费。`);
    return saved;
  } catch (error) {
    state.project.world.world_id = previousId;
    state.currentWorldId = previousCurrentWorldId;
    $("#world-id").value = previousId;
    updateCurrentProjectContext();
    throw error;
  }
}

async function recoverRecentVisualAssets() {
  syncProjectForm();
  syncNodeForm();
  const sourceIds = new Set([
    state.artifactSourceWorldId,
    state.currentWorldId,
    state.project.world.world_id,
  ].filter(Boolean));
  setProjectActionStatus("working", "正在查找最近生成的美术", "将恢复图片并重新绑定到当前角色和剧情节点。 ");
  const artifacts = await request("/api/worlds/visual-assets");
  const candidates = (Array.isArray(artifacts) ? artifacts : [])
    .filter((item) => Number(item.generated_count || 0) > 0)
    .sort((left, right) => String(right.updated_at || "").localeCompare(String(left.updated_at || "")));
  const currentName = state.project.world.name?.trim();
  const selected = candidates.find((item) => sourceIds.has(item.world_id))
    || candidates.find((item) => currentName && item.title === currentName);
  if (!selected) {
    throw new Error("没有找到与当前作品对应的已生成美术。请先在 Creator 中生成图片。");
  }
  const loaded = await request(`/api/worlds/visual-assets/${encodeURIComponent(selected.artifact_id)}`);
  if (!loaded?.result || Number(loaded.result.metadata?.generated_count || 0) < 1) {
    throw new Error("最近的美术产物中没有可恢复的图片。");
  }
  const result = await request("/api/creator/mcp/tools/call", {
    method: "POST",
    body: JSON.stringify({
      name: "bind_visual_assets",
      arguments: {},
      project: state.project,
      artifacts: { visual_result: loaded.result },
    }),
  });
  if (result.isError) throw new Error(result.structuredContent?.error?.message || result.content?.[0]?.text || "恢复美术失败");
  const updatedProject = result.structuredContent?.project;
  const counts = result.structuredContent?.artifacts?.visual_bindings || {};
  if (!updatedProject) throw new Error("恢复工具没有返回更新后的 Creator Graph。");
  pushUndo("恢复最近生成美术");
  setProject(updatedProject, { resetHistory: false, selectedNodeId: state.selectedNodeId });
  state.artifactSourceWorldId = selected.world_id || state.artifactSourceWorldId;
  const generatedCount = Number(loaded.result.metadata?.generated_count || selected.generated_count || 0);
  setProjectActionStatus(
    "warning",
    `已恢复 ${generatedCount} 张图片，但尚未保存`,
    `已绑定 ${counts.characters || 0} 个角色、${counts.scenes || 0} 个场景。请“另存为新作品”或保存草稿；玩家端目前还看不到。`
  );
}

async function openPlayerExperience() {
  const button = $("#open-player");
  const original = button.textContent;
  button.disabled = true;
  button.textContent = "正在发布…";
  try {
    syncProjectForm();
    syncNodeForm();
    requireProjectName("发布到玩家端");
    setProjectActionStatus("working", "正在发布到玩家端", "校验剧情图并生成可供 Play 消费的正式世界。 ");
    const result = await request("/api/creator/mcp/tools/call", {
      method: "POST",
      body: JSON.stringify({
        name: "publish_to_play",
        arguments: {},
        project: state.project,
        artifacts: state.project.pipeline_artifacts || {},
      }),
    });
    if (result.isError) throw new Error(result.structuredContent?.error?.message || result.content?.[0]?.text || "发布失败");
    const saved = result.structuredContent?.artifacts?.published_world;
    if (!saved?.world_id) throw new Error("发布工具没有返回世界数据");
    state.currentWorldId = saved.world_id;
    state.lastSavedProjectFingerprint = projectFingerprint();
    await loadWorlds();
    $("#world-picker").value = saved.world_id;
    setProjectActionStatus("success", `已发布《${saved.name}》`, "正在打开该作品的玩家端。 ");
    button.textContent = "正在进入…";
    window.location.href = `/play?world=${encodeURIComponent(saved.world_id)}`;
  } catch (error) {
    setProjectActionStatus("error", "发布失败", cleanApiError(error));
    throw error;
  } finally {
    button.disabled = false;
    button.textContent = original;
  }
}

async function createCreatorVersion(label = "手动快照") {
  syncProjectForm();
  syncNodeForm();
  const worldId = state.project.world.world_id || state.currentWorldId;
  if (!worldId) throw new Error("请先设置世界 ID");
  const version = await request("/api/creator/versions", {
    method: "POST",
    body: JSON.stringify({ world_id: worldId, label, project: state.project }),
  });
  await loadCreatorVersions();
  $("#creator-version-picker").value = version.version_id;
  $("#restore-version").disabled = false;
  addCreatorToolLog("系统", `已创建版本快照：${version.label}`);
  setProjectActionStatus("success", "版本快照已创建", `${version.label} · 后续修改可以随时恢复。`);
  return version;
}

async function loadCreatorVersions() {
  const worldId = state.project?.world?.world_id || state.currentWorldId;
  const picker = $("#creator-version-picker");
  if (!worldId) {
    state.versions = [];
    picker.innerHTML = '<option value="">暂无快照</option>';
    $("#restore-version").disabled = true;
    return;
  }
  state.versions = await request(`/api/creator/versions/${encodeURIComponent(worldId)}`);
  picker.innerHTML = state.versions.length
    ? state.versions.map((version) => `<option value="${escapeAttr(version.version_id)}">${escapeHtml(version.label)} · ${escapeHtml(new Date(version.created_at).toLocaleString())}</option>`).join("")
    : '<option value="">暂无快照</option>';
  $("#restore-version").disabled = state.versions.length === 0;
}

async function restoreCreatorVersion() {
  const versionId = $("#creator-version-picker").value;
  const worldId = state.project?.world?.world_id || state.currentWorldId;
  if (!worldId || !versionId) return;
  const artifact = await request(`/api/creator/versions/${encodeURIComponent(worldId)}/${encodeURIComponent(versionId)}`);
  pushUndo(`恢复快照前：${artifact.label}`);
  setProject(artifact.project, { resetHistory: false });
  addCreatorToolLog("系统", `已恢复版本快照：${artifact.label}`);
  setProjectActionStatus("success", "版本快照已恢复", `${artifact.label} 已回写到当前画布。`);
}

async function startPlaytest() {
  const button = $("#start-playtest");
  const refresh = $("#refresh-session");
  button.disabled = true;
  refresh.disabled = true;
  button.textContent = "启动中...";
  state.playtestStarted = false;
  setPlaytestStatus("working", "正在准备试玩", "检查当前剧情图并初始化运行会话。");
  focusPlaytestPanel();
  try {
    syncProjectForm();
    syncNodeForm();
    compileWorld();
    setPlaytestStatus("working", "正在保存当前剧情", "试玩将消费编辑器中的最新节点、分支、人物和属性。");
    await saveCreatorWorld();
    setPlaytestStatus("working", "正在初始化试玩会话", "加载真实开场、玩家状态和 NPC 运行状态。");
    const data = await request(`/api/worlds/${encodeURIComponent(state.currentWorldId)}/start`, { method: "POST" });
    $("#play-log").innerHTML = "";
    addLog("世界", data.narration || "故事开始。");
    renderRuntime(data);
    state.playtestStarted = true;
    state.playtestProjectFingerprint = projectFingerprint();
    setPlaytestStatus("success", "试玩已启动", `当前开场已加载：${data.narration || "故事开始。"}`);
  } catch (error) {
    setPlaytestStatus("error", "试玩启动失败", String(error.message || error).slice(0, 240));
    throw error;
  } finally {
    button.disabled = false;
    refresh.disabled = false;
    button.textContent = "启动试玩";
  }
}

async function sendChat(message) {
  syncProjectForm();
  syncNodeForm();
  if (!state.playtestStarted || state.playtestProjectFingerprint !== projectFingerprint()) {
    await startPlaytest();
  }
  const target = $("#target-npc").value;
  const world = compileWorld();
  const sendButton = $("#chat-form button[type=submit]");
  sendButton.disabled = true;
  setPlaytestStatus("working", "NPC 正在回应", "正在根据当前地点、记忆、任务和玩家状态生成回复。");
  addLog(world.player.name || "玩家", message);
  try {
    const data = await request(`/api/worlds/${encodeURIComponent(state.currentWorldId)}/chat`, {
      method: "POST",
      body: JSON.stringify({
        message,
        player_name: world.player.name || "玩家",
        location: world.player.location || "",
        player_goal: world.story_goals?.[0] || "",
        target_npc_id: target,
      }),
    });
    if (Array.isArray(data.messages) && data.messages.length) {
      for (const item of data.messages) addLog(item.speaker || "NPC", item.content);
    } else {
      addLog(data.speaker?.name || "NPC", data.reply || "");
    }
    renderRuntime(data);
    setPlaytestStatus("success", "试玩已更新", "NPC 回复已写入运行会话，玩家状态和任务进度已刷新。");
  } catch (error) {
    setPlaytestStatus("error", "对话失败", String(error.message || error).slice(0, 240));
    throw error;
  } finally {
    sendButton.disabled = false;
  }
}

function setPlaytestStatus(mode, title, detail) {
  const status = $("#playtest-status");
  status.className = `playtest-status ${mode}`;
  status.hidden = false;
  $("#playtest-status-title").textContent = title;
  $("#playtest-status-detail").textContent = detail;
  showPlaytestToast(mode, title, detail);
}

function focusPlaytestPanel() {
  const panel = $("#playtest-panel");
  if (!panel) return;
  panel.scrollIntoView({ behavior: "smooth", block: "start", inline: "nearest" });
  panel.classList.remove("playtest-focus-pulse");
  window.requestAnimationFrame(() => panel.classList.add("playtest-focus-pulse"));
}

function showPlaytestToast(mode, title, detail) {
  const toast = $("#playtest-toast");
  if (!toast) return;
  if (state.playtestToastTimer) window.clearTimeout(state.playtestToastTimer);
  toast.hidden = false;
  toast.className = `playtest-toast ${mode}`;
  $("#playtest-toast-title").textContent = title;
  $("#playtest-toast-detail").textContent = detail;
  if (mode !== "working") {
    state.playtestToastTimer = window.setTimeout(() => {
      toast.hidden = true;
      state.playtestToastTimer = null;
    }, mode === "error" ? 10000 : 6500);
  }
}

function renderRuntime(data) {
  state.runtimeNpcs = data.npcs || state.runtimeNpcs || [];
  const speakerId = data.speaker?.id || data.active_entity?.id || "";
  $("#target-npc").innerHTML = (state.runtimeNpcs || [])
    .map((npc) => `<option value="${escapeAttr(npc.id)}">${escapeHtml(npc.name || npc.id)}</option>`)
    .join("");
  if (speakerId) $("#target-npc").value = speakerId;
  const player = data.player || data.state?.player || {};
  const inventory = Array.isArray(player.inventory)
    ? player.inventory.map((item) => typeof item === "string" ? item : `${item.name || "道具"} ×${item.quantity || 1}`).join("、")
    : "";
  $("#runtime-location").textContent = `地点：${player.location || "未知"}`;
  $("#runtime-progress").textContent = `进度：${data.quest_progress || "运行中"}`;
  $("#runtime-inventory").textContent = `道具：${inventory || "无"}`;
}

function cleanApiError(error) {
  const text = String(error?.message || error || "未知错误");
  try {
    const payload = JSON.parse(text);
    const detail = payload.detail;
    if (typeof detail === "string") return detail;
    if (detail?.message) {
      const issues = Array.isArray(detail.issues) ? detail.issues.map((item) => item.message).filter(Boolean).slice(0, 4) : [];
      return [detail.message, ...issues].join(" ");
    }
  } catch {
    // The API may return plain text.
  }
  return text.slice(0, 400);
}

async function loadCreatorTools() {
  const data = await request("/api/creator/mcp/tools/list");
  state.creatorTools = data.tools || [];
  const container = $("#creator-tool-list");
  if (!container) return;
  container.innerHTML = state.creatorTools
    .map((tool) => {
      const owner = tool._meta?.ownerAgent || "";
      const type = tool._meta?.capabilityType || "";
      const detail = [owner, type].filter(Boolean).join(" · ");
      return `<span class="creator-tool-chip" title="${escapeAttr(tool.description || "")}"><strong>${escapeHtml(tool.title || tool.name)}</strong><code>${escapeHtml(tool.name || tool.id || "")}</code>${detail ? `<small>${escapeHtml(detail)}</small>` : ""}</span>`;
    })
    .join("");
}

function currentCreatorHistoryWorldId() {
  return String(state.project?.world?.world_id || state.currentWorldId || "").trim();
}

function updateCreatorHistoryUi() {
  const count = state.creatorConversationMessages.length;
  const counter = $("#creator-history-count");
  const empty = $("#creator-history-empty");
  if (counter) counter.textContent = `${count} 条`;
  if (empty) empty.hidden = count > 0;
}

function renderCreatorHistory(messages = []) {
  state.creatorConversationMessages = Array.isArray(messages) ? messages.map((message) => ({ ...message })) : [];
  state.creatorAgentHistory = state.creatorConversationMessages
    .filter((message) => ["user", "assistant"].includes(message.role))
    .map((message) => ({ role: message.role, content: String(message.content || "") }));
  const container = $("#creator-agent-log");
  container.innerHTML = '<p id="creator-history-empty" class="creator-history-empty">发送创作要求后，对话会保存在当前项目中。</p>';
  state.creatorConversationMessages.forEach(renderCreatorHistoryEntry);
  updateCreatorHistoryUi();
  container.scrollTop = container.scrollHeight;
}

function renderCreatorHistoryEntry(message) {
  const entry = document.createElement("div");
  const roleClass = message.role === "user" ? "user-message" : "agent-message";
  entry.className = `log-entry ${roleClass}`;
  const summary = Array.isArray(message.summary) ? message.summary : [];
  const details = summary.length ? `<ul>${summary.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul>` : "";
  const createdAt = message.created_at ? new Date(message.created_at) : null;
  const time = createdAt && !Number.isNaN(createdAt.getTime())
    ? `<small class="creator-message-time">${escapeHtml(createdAt.toLocaleString("zh-CN", { hour12: false }))}</small>`
    : "";
  entry.innerHTML = `<strong>${escapeHtml(message.speaker || (message.role === "user" ? "你" : "Creator Agent"))}</strong><span>${escapeHtml(message.content || "")}</span>${details}${time}`;
  $("#creator-agent-log").appendChild(entry);
}

function appendCreatorConversationMessage(role, speaker, content, summary = [], createdAt = new Date().toISOString()) {
  const message = { role, speaker, content: String(content || ""), summary: Array.isArray(summary) ? summary : [], created_at: createdAt };
  state.creatorConversationMessages.push(message);
  state.creatorAgentHistory.push({ role, content: message.content });
  renderCreatorHistoryEntry(message);
  updateCreatorHistoryUi();
  const container = $("#creator-agent-log");
  container.scrollTop = container.scrollHeight;
  return message;
}

async function persistCreatorHistoryMessage(message, worldId = currentCreatorHistoryWorldId()) {
  if (!worldId || !message.content.trim()) return null;
  return request(`/api/creator/history/${encodeURIComponent(worldId)}`, {
    method: "POST",
    body: JSON.stringify({
      role: message.role,
      speaker: message.speaker,
      content: message.content,
      summary: message.summary || [],
    }),
  });
}

async function loadCreatorHistory() {
  const worldId = currentCreatorHistoryWorldId();
  state.creatorHistoryWorldId = worldId;
  if (!worldId) {
    renderCreatorHistory([]);
    return;
  }
  try {
    const messages = await request(`/api/creator/history/${encodeURIComponent(worldId)}`);
    if (worldId !== currentCreatorHistoryWorldId()) return;
    renderCreatorHistory(messages);
  } catch (error) {
    renderCreatorHistory([]);
    addCreatorToolLog("系统", `创作对话读取失败：${cleanApiError(error)}`);
  }
}

async function copyCreatorHistory(worldId, messages) {
  if (!worldId || !Array.isArray(messages) || !messages.length) {
    state.creatorHistoryWorldId = worldId || "";
    return;
  }
  try {
    for (const message of messages) {
      await persistCreatorHistoryMessage(message, worldId);
    }
    state.creatorHistoryWorldId = worldId;
  } catch (error) {
    addCreatorToolLog("系统", `作品已另存，但创作对话复制失败：${cleanApiError(error)}`);
  }
}

async function clearCreatorHistory() {
  const worldId = currentCreatorHistoryWorldId();
  if (worldId) {
    await request(`/api/creator/history/${encodeURIComponent(worldId)}`, { method: "DELETE" });
  }
  renderCreatorHistory([]);
  $("#creator-tool-log").innerHTML = "";
  state.creatorToolLogCount = 0;
  $("#creator-tool-log-count").textContent = "0 条";
  clearPendingChange();
  clearCreatorAgentStatus();
}

async function sendCreatorAgentMessage(message) {
  if (state.initialProjectLoading) {
    throw new Error("当前项目仍在载入，请等待项目名称和地址稳定后再提交创作要求。");
  }
  if (state.activeWorkflowRunId) {
    throw new Error("当前 Creator 工作流仍在执行，请等待完成或先取消，再提交新的创作要求。");
  }
  syncProjectForm();
  syncNodeForm();
  const requestHistory = state.creatorAgentHistory.slice(-10);
  const userMessage = appendCreatorConversationMessage("user", "你", message);
  try {
    await persistCreatorHistoryMessage(userMessage);
  } catch (error) {
    addCreatorToolLog("系统", `你的消息已显示，但保存失败：${cleanApiError(error)}`);
  }
  const button = $("#preview-creator-change");
  const controller = new AbortController();
  state.creatorAgentRequestController = controller;
  button.disabled = true;
  button.textContent = "发送中...";
  startCreatorAgentStatus();
  try {
    const data = await request("/api/creator/workflows/preview", {
      method: "POST",
      signal: controller.signal,
      body: JSON.stringify({
        message,
        project: state.project,
        selected_node_id: state.selectedNodeId,
        history: requestHistory,
      }),
    });
    state.pendingChange = data.executable ? data : null;
    renderPendingChange();
    const source = String(data.source || "");
    const speaker = source.includes("llm")
      ? source.includes("repair") || source.includes("fallback")
        ? "Creator Agent / LLM + 校验修复"
        : "Creator Agent / LLM"
      : source === "tool_router"
        ? "Creator Agent / 工具路由"
        : "Creator Agent / 降级规则";
    const intentLabels = { chat: "普通对话", clarify: "需要澄清", graph_edit: "修改当前剧情", workflow: "Router Agent 工作流", error: "处理失败" };
    const routeSummary = `意图：${intentLabels[data.intent] || data.intent || "未知"} · 路由：${data.route || "未指定"}`;
    const agentMessage = appendCreatorConversationMessage(
      "assistant",
      speaker,
      data.reply || "修改预览已生成。",
      [routeSummary, ...(data.summary || [])],
    );
    try {
      await persistCreatorHistoryMessage(agentMessage);
    } catch (error) {
      addCreatorToolLog("系统", `Agent 回复已显示，但保存失败：${cleanApiError(error)}`);
    }
    const toolCount = (data.tool_calls || []).length;
    if (data.intent === "error" || source.startsWith("fallback")) {
      const reason = String(data.raw_excerpt || "模型未返回可执行结果").slice(0, 320);
      finishCreatorAgentStatus("error", "模型编排失败，已显示降级预览", reason);
      addCreatorToolLog("Creator Agent", `LLM 编排失败：${reason}`);
    } else if (!data.executable) {
      finishCreatorAgentStatus(
        data.intent === "clarify" ? "working" : "success",
        data.intent === "clarify" ? "Creator Agent 正在向你确认" : "Creator Agent 已回复",
        data.intent === "clarify" ? "请直接在输入框回答上面的问题。" : "这是普通对话，没有修改项目，也没有调用工具。",
      );
    } else {
      finishCreatorAgentStatus(
        "success",
        "执行预览已生成",
        `已选择 ${toolCount} 个 Pipeline 工具和 ${(data.operations || []).length} 项图修改。当前尚未执行，请检查后确认。`,
      );
    }
  } catch (error) {
    if (error.name === "AbortError") {
      finishCreatorAgentStatus("cancelled", "已取消分析", "本次请求已停止，画布没有发生变化。");
      addCreatorToolLog("系统", "已取消本次剧情分析，画布没有变化。");
    } else {
      const detail = String(error.message || error).slice(0, 180);
      finishCreatorAgentStatus("error", "生成失败", `Creator Agent 未返回修改：${detail}`);
      addCreatorToolLog("系统", `生成失败：${detail}`);
    }
  } finally {
    if (state.creatorAgentRequestController === controller) state.creatorAgentRequestController = null;
    button.disabled = false;
    button.textContent = "发送";
  }
}

const CREATOR_AGENT_STAGES = [
  { after: 0, title: "正在读取当前剧情图", detail: "整理选中节点、人物、属性、道具和已有分支。" },
  { after: 3, title: "正在检查剧情结构", detail: "分析节点连线、可达性、支线和回接关系。" },
  { after: 8, title: "正在生成修改方案", detail: "把自然语言要求转换为可校验的剧情操作。" },
  { after: 15, title: "Creator Agent 仍在工作", detail: "复杂剧情可能需要更长时间；可以继续等待，也可以取消本次分析。" },
];

function startCreatorAgentStatus() {
  stopCreatorAgentStatusTimer();
  state.creatorAgentStartedAt = Date.now();
  state.creatorAgentElapsed = 0;
  const update = () => {
    const elapsed = Math.max(0, Math.floor((Date.now() - state.creatorAgentStartedAt) / 1000));
    state.creatorAgentElapsed = elapsed;
    const stage = [...CREATOR_AGENT_STAGES].reverse().find((item) => elapsed >= item.after) || CREATOR_AGENT_STAGES[0];
    setCreatorAgentStatus("working", stage.title, stage.detail, elapsed);
  };
  update();
  state.creatorAgentStatusTimer = window.setInterval(update, 1000);
}

function finishCreatorAgentStatus(mode, title, detail) {
  const elapsed = state.creatorAgentStatusTimer
    ? Math.max(0, Math.floor((Date.now() - state.creatorAgentStartedAt) / 1000))
    : state.creatorAgentElapsed;
  state.creatorAgentElapsed = elapsed;
  stopCreatorAgentStatusTimer();
  setCreatorAgentStatus(mode, title, detail, elapsed);
}

function stopCreatorAgentStatusTimer() {
  if (state.creatorAgentStatusTimer) window.clearInterval(state.creatorAgentStatusTimer);
  state.creatorAgentStatusTimer = null;
}

function setCreatorAgentStatus(mode, title, detail, elapsed) {
  const status = $("#creator-agent-status");
  status.hidden = false;
  status.className = `creator-agent-status ${mode}`;
  $("#creator-agent-status-title").textContent = title;
  $("#creator-agent-status-detail").textContent = detail;
  $("#creator-agent-elapsed").textContent = `${elapsed} 秒`;
  $("#creator-agent-spinner").hidden = mode !== "working";
  $("#cancel-creator-agent").hidden = mode !== "working";
}

function clearCreatorAgentStatus() {
  stopCreatorAgentStatusTimer();
  const status = $("#creator-agent-status");
  if (status) status.hidden = true;
}

function setCreatorDockHeight(height) {
  const dock = $(".creator-command-dock");
  const maxHeight = Math.max(300, window.innerHeight * 0.7);
  const nextHeight = Math.min(maxHeight, Math.max(240, height));
  dock.style.height = `${Math.round(nextHeight)}px`;
}

function renderPendingChange() {
  const preview = $("#creator-change-preview");
  const change = state.pendingChange;
  if (!change) {
    preview.hidden = true;
    return;
  }
  preview.hidden = false;
  $("#change-preview-reply").textContent = change.reply || "";
  const report = change.report || {};
  const toolCalls = change.tool_calls || [];
  const warnings = (report.issues || []).filter((issue) => issue.severity === "warning");
  const badge = $("#change-validation-badge");
  badge.textContent = !report.valid && toolCalls.length ? "工具执行后校验" : warnings.length ? `${warnings.length} 条提醒` : "校验通过";
  badge.classList.toggle("warning", warnings.length > 0 || !report.valid);
  $("#change-preview-report").textContent = `Pipeline 工具 ${toolCalls.length} · 图修改 ${(change.operations || []).length} · 当前节点 ${report.node_count || 0} · 可达 ${report.reachable_count || 0}/${report.node_count || 0}`;
  const operations = (change.operations || []).map((operation) => `<li>${escapeHtml(describeOperation(operation))}</li>`);
  const tools = toolCalls.map((call, index) => `<li class="tool-call"><strong>${index + 1}. ${escapeHtml(toolDisplayName(call.tool))}</strong> · ${escapeHtml(call.reason || "由 Creator Agent 选择")}</li>`);
  $("#change-operation-list").innerHTML = [...tools, ...operations].join("");
  $("#apply-creator-change").disabled = (!report.valid && !toolCalls.length) || (!toolCalls.length && !(change.operations || []).length);
  $("#apply-creator-change").textContent = toolCalls.length ? "确认执行工作流" : "确认应用修改";
}

function toolDisplayName(toolId) {
  return state.creatorTools.find((tool) => tool.name === toolId || tool.id === toolId)?.title ||
    state.creatorTools.find((tool) => tool.name === toolId || tool.id === toolId)?.name || toolId;
}

function describeOperation(operation) {
  const data = operation.data || {};
  const labels = {
    set_world: "修改世界设定",
    set_player_stat: `设置属性 ${operation.target_id} = ${data.value}`,
    add_item: `新增道具「${data.name || ""}」`,
    remove_item: `移除道具「${data.name || ""}」`,
    add_character: `新增角色「${data.name || data.id || ""}」`,
    update_character: `修改角色 ${operation.target_id}`,
    delete_character: `删除角色 ${operation.target_id}`,
    add_node: `新增节点「${data.title || data.id || ""}」`,
    update_node: `修改节点 ${operation.target_id}`,
    delete_node: `删除节点 ${operation.target_id}`,
    add_choice: `给 ${operation.target_id} 新增选项「${data.text || ""}」`,
    update_choice: `修改选项 ${data.choice_id || ""}`,
    delete_choice: `删除选项 ${data.choice_id || ""}`,
    connect_nodes: `连接 ${operation.target_id} → ${data.target_id || ""}`,
    disconnect_nodes: `断开 ${operation.target_id} → ${data.target_id || ""}`,
    create_branch: `从 ${data.source_node_id || ""} 创建支线「${data.choice_text || ""}」，包含 ${(data.nodes || []).length} 个节点`,
  };
  return labels[operation.type] || operation.type;
}

async function applyPendingChange() {
  const change = state.pendingChange;
  if (!change) return;
  if (state.activeWorkflowRunId) throw new Error("已有 Creator 工作流正在执行，请勿重复启动。");
  const button = $("#apply-creator-change");
  button.disabled = true;
  try {
    const run = await request("/api/creator/workflows/run", {
      method: "POST",
      body: JSON.stringify({
        preview_id: change.preview_id,
        project: state.project,
      }),
    });
    state.activeWorkflowRunId = run.run_id;
    state.workflowEventCount = 0;
    button.textContent = "工作流执行中…";
    setCreatorAgentStatus("working", "Creator 工作流已启动", "正在按顺序调用 Pipeline 工具。", 0);
    await waitForCreatorWorkflow(run.run_id, change);
  } finally {
    button.disabled = false;
    button.textContent = "确认执行";
  }
}

async function recoverLatestCreatorWorkflow() {
  const worldId = currentCreatorHistoryWorldId();
  if (!worldId || state.activeWorkflowRunId) return;
  const run = await request(`/api/creator/workflows/latest/${encodeURIComponent(worldId)}`);
  if (!run) return;
  if (run.acknowledged_at) return;
  const status = run.status;
  const sameProject = run.project && projectFingerprint(normalizeClientProject(run.project)) === projectFingerprint();
  if (status === "done" && sameProject) {
    await request(`/api/creator/workflows/${encodeURIComponent(run.run_id)}/acknowledge`, { method: "POST" });
    return;
  }
  if (!["queued", "running", "cancelling", "done"].includes(status)) return;
  state.activeWorkflowRunId = run.run_id;
  state.workflowEventCount = 0;
  const latest = (run.events || []).at(-1);
  setCreatorAgentStatus(
    "working",
    status === "done" ? "正在恢复已完成的创作结果" : (latest?.title || "正在恢复 Creator 工作流"),
    status === "done" ? "检测到该项目有尚未回写页面的生成结果。" : (latest?.detail || "已重新连接后台运行。"),
    0,
  );
  const recovery = waitForCreatorWorkflow(run.run_id, {
    summary: ["恢复 Creator 工作流结果"],
    operations: [],
    tool_calls: [],
  });
  if (status === "done") await recovery;
}

async function waitForCreatorWorkflow(runId, change) {
  const startedAt = Date.now();
  try {
    while (state.activeWorkflowRunId === runId) {
      const run = await request(`/api/creator/workflows/${encodeURIComponent(runId)}`);
      const freshEvents = (run.events || []).slice(state.workflowEventCount);
      state.workflowEventCount = (run.events || []).length;
      freshEvents.forEach((event) => {
        if (["running", "done", "error", "cancelled"].includes(event.status)) {
          addCreatorToolLog(event.tool ? `工具 · ${toolDisplayName(event.tool)}` : "Creator 工作流", event.title, event.detail ? [event.detail] : []);
        }
      });
      const latest = (run.events || []).at(-1);
      const elapsed = Math.floor((Date.now() - startedAt) / 1000);
      if (["queued", "running", "cancelling"].includes(run.status)) {
        setCreatorAgentStatus(run.status === "cancelling" ? "cancelled" : "working", latest?.title || "工作流执行中", latest?.detail || "正在调用工具。", elapsed);
        await new Promise((resolve) => window.setTimeout(resolve, 750));
        continue;
      }
      if (run.status === "done") {
        pushUndo(change.summary?.[0] || "AI Creator 工作流");
        const selectedNodeId = selectedNodeAfterChange(change.operations || [], change.tool_calls || []) || "start";
        setProject(run.project, { resetHistory: false, selectedNodeId });
        state.currentWorldId = run.project?.world?.world_id || state.currentWorldId;
        if (run.artifacts?.saved_world || run.artifacts?.published_world) {
          await loadWorlds();
          if (state.currentWorldId) $("#world-picker").value = state.currentWorldId;
          state.lastSavedProjectFingerprint = projectFingerprint();
        }
        if (run.artifacts?.visual_result) {
          state.artifactSourceWorldId ||= run.project?.world?.world_id || state.currentWorldId;
        }
        clearPendingChange();
        finishCreatorAgentStatus("success", "Creator 工作流已完成", describeWorkflowArtifacts(run.artifacts, change.tool_calls || []));
        if (run.artifacts?.published_world) {
          setProjectActionStatus(
            "success",
            `已发布《${run.project?.world?.name || "互动剧情"}》`,
            `${state.project.nodes.length} 个剧情节点 · ${state.project.characters.length} 位角色 · 玩家端已可消费。`
          );
        } else if (run.artifacts?.saved_world) {
          setProjectActionStatus(
            "success",
            `草稿已保存：《${run.project?.world?.name || "互动剧情"}》`,
            `${state.project.nodes.length} 个剧情节点已写入世界库；尚未发布到玩家端。`
          );
        } else {
          const generated = run.artifacts?.visual_result?.generated?.length || 0;
          const binding = run.artifacts?.visual_bindings || {};
          setProjectActionStatus(
            "warning",
            "生成结果尚未保存，玩家端不可见",
            generated
              ? `已生成 ${generated} 张图片并绑定 ${binding.characters || 0} 个角色、${binding.scenes || 0} 个场景。请保存草稿，或发布并打开玩家端。`
              : "结果已经回写当前画布。请保存草稿，或发布并打开玩家端。"
          );
        }
        try {
          await request(`/api/creator/workflows/${encodeURIComponent(runId)}/acknowledge`, { method: "POST" });
        } catch (error) {
          addCreatorToolLog("系统", `结果已回写，但确认恢复状态失败：${cleanApiError(error)}`);
        }
        return;
      }
      if (run.status === "cancelled") {
        finishCreatorAgentStatus("cancelled", "工作流已停止", "未执行的工具不会继续运行，当前画布未被回写。 ");
        return;
      }
      const error = run.error?.message || latest?.detail || "Creator 工作流执行失败";
      throw new Error(error);
    }
  } catch (error) {
    finishCreatorAgentStatus("error", "工作流执行失败", cleanApiError(error));
    addCreatorToolLog("系统", `工作流执行失败：${cleanApiError(error)}`);
  } finally {
    if (state.activeWorkflowRunId === runId) state.activeWorkflowRunId = "";
  }
}

function describeWorkflowArtifacts(artifacts = {}, toolCalls = []) {
  const details = [];
  const planAssets = artifacts.visual_plan?.assets || [];
  const generated = artifacts.visual_result?.generated || [];
  const failed = artifacts.visual_result?.failed || [];
  const rejectedCutouts = artifacts.visual_result?.metadata?.background_removal_rejected_count || 0;
  const bindings = artifacts.visual_bindings || {};
  const layout = artifacts.graph_layout;
  if (artifacts.story_authoring) details.push("完整剧情已生成并回写画布");
  if (layout) details.push(`已整理 ${layout.moved_node_count || 0} 个剧情节点`);
  if (artifacts.visual_result) {
    details.push(`已生成 ${generated.length} 张图片，失败 ${failed.length} 张`);
    if (rejectedCutouts) details.push(`${rejectedCutouts} 张人物抠图未通过主体保护，已自动保留原图`);
  } else if (artifacts.visual_plan) {
    details.push(`仅完成 ${planAssets.length} 项视觉方案，尚未生成图片`);
  }
  if (artifacts.visual_bindings) details.push(`已绑定 ${bindings.characters || 0} 张角色立绘、${bindings.scenes || 0} 张场景背景`);
  if (artifacts.saved_world) details.push(`《${artifacts.saved_world.name || artifacts.saved_world.world_id}》已保存为草稿`);
  if (artifacts.published_world) details.push(`《${artifacts.published_world.name || artifacts.published_world.world_id}》已发布到玩家端`);
  return details.length ? `${details.join("；")}。` : `已执行 ${toolCalls.length} 个工具并回写画布。`;
}

async function cancelCreatorAgentWork() {
  if (state.activeWorkflowRunId) {
    await request(`/api/creator/workflows/${encodeURIComponent(state.activeWorkflowRunId)}/cancel`, { method: "POST" });
    return;
  }
  state.creatorAgentRequestController?.abort();
}

function selectedNodeAfterChange(operations, toolCalls = []) {
  const branch = [...operations].reverse().find((operation) => operation.type === "create_branch");
  if (branch) return branch.data?.source_node_id || "";
  const targeted = [...operations].reverse().find((operation) => operation.target_id);
  if (targeted?.target_id) return targeted.target_id;
  const layout = [...toolCalls].reverse().find((call) => call.tool === "layout_creator_graph");
  return layout?.arguments?.root_node_id || "";
}

function clearPendingChange() {
  state.pendingChange = null;
  const preview = $("#creator-change-preview");
  if (preview) preview.hidden = true;
}

function rejectPendingChange() {
  if (!state.pendingChange) return;
  addCreatorToolLog("系统", "已放弃这次修改预览。");
  clearPendingChange();
  finishCreatorAgentStatus("cancelled", "已放弃修改预览", "画布没有发生变化，可以重新描述你的要求。");
}

function applyCreatorOperations(operations) {
  const summary = [];
  for (const operation of operations) {
    const type = operation.type;
    const data = operation.data || {};
    if (type === "set_world") {
      if (data.name) state.project.world.name = String(data.name);
      if (data.lore) state.project.world.lore = String(data.lore);
      if (data.player && typeof data.player === "object") {
        state.project.world.player = { ...(state.project.world.player || {}), ...data.player };
      }
      summary.push("更新世界设定。");
    } else if (type === "set_player_stat") {
      const key = operation.target_id || data.key || data.name;
      if (!key) continue;
      state.project.world.player = state.project.world.player || {};
      state.project.world.player.stats = state.project.world.player.stats || {};
      state.project.world.player.stats[key] = data.value;
      summary.push(`设置玩家属性 ${key}。`);
    } else if (type === "add_item") {
      const name = data.name || data.id;
      if (!name) continue;
      state.project.world.player = state.project.world.player || {};
      state.project.world.player.inventory = state.project.world.player.inventory || [];
      state.project.world.player.inventory.push({ name: String(name), quantity: Number(data.quantity || 1) });
      summary.push(`新增道具「${name}」。`);
    } else if (type === "add_character") {
      const id = uniqueId("npc", data.id || data.name);
      state.project.characters.push({
        id,
        name: String(data.name || id),
        role: String(data.role || "NPC"),
        personality: String(data.personality || ""),
        location: String(data.location || state.project.world.player?.location || ""),
        portrait: String(data.portrait || ""),
      });
      summary.push(`新增角色「${data.name || id}」。`);
    } else if (type === "update_character") {
      const character = state.project.characters.find((item) => item.id === operation.target_id || item.name === operation.target_id);
      if (!character) continue;
      Object.assign(character, pick(data, ["name", "role", "personality", "location", "portrait"]));
      summary.push(`更新角色「${character.name || character.id}」。`);
    } else if (type === "add_node") {
      const after = nodeById(data.after || operation.target_id || state.selectedNodeId);
      const id = uniqueId(data.type || "story", data.id || data.title);
      const node = {
        id,
        type: ["story", "choice", "ending"].includes(data.type) ? data.type : "story",
        x: Math.min(CANVAS_WIDTH - 260, (after?.x || 120) + 300),
        y: Math.min(CANVAS_HEIGHT - 180, (after?.y || 120) + 120),
        title: String(data.title || "新的剧情节点"),
        character: String(data.character || after?.character || state.project.characters[0]?.id || ""),
        background: String(data.background || ""),
        content: String(data.content || data.title || ""),
        conditions: objectOrEmpty(data.conditions),
        effects: objectOrEmpty(data.effects),
        next: String(data.next || ""),
        choices: Array.isArray(data.choices) ? data.choices : [],
      };
      state.project.nodes.push(node);
      if (after && !after.next && after.type !== "ending") after.next = id;
      state.selectedNodeId = id;
      summary.push(`新增节点「${node.title}」。`);
    } else if (type === "update_node") {
      const node = nodeById(operation.target_id || state.selectedNodeId);
      if (!node) continue;
      Object.assign(node, pick(data, ["title", "type", "character", "background", "content", "next"]));
      if (data.conditions && typeof data.conditions === "object") node.conditions = data.conditions;
      if (data.effects && typeof data.effects === "object") node.effects = data.effects;
      state.selectedNodeId = node.id;
      summary.push(`更新节点「${node.title || node.id}」。`);
    } else if (type === "add_choice") {
      const node = nodeById(operation.target_id || state.selectedNodeId);
      if (!node) continue;
      node.choices = node.choices || [];
      node.choices.push({
        text: String(data.text || "新选项"),
        next: String(data.next || ""),
        effects: objectOrEmpty(data.effects),
        conditions: objectOrEmpty(data.conditions),
      });
      state.selectedNodeId = node.id;
      summary.push(`新增选项「${data.text || "新选项"}」。`);
    }
  }
  fillProjectForm();
  renderCharacters();
  renderNodeCanvas();
  renderNodeForm();
  updateCompiledPreview();
  return summary;
}

function addCreatorToolLog(speaker, text, summary = []) {
  const entry = document.createElement("div");
  entry.className = "log-entry";
  const details = summary.length ? `<ul>${summary.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul>` : "";
  entry.innerHTML = `<strong>${escapeHtml(speaker)}</strong><span>${escapeHtml(text || "")}</span>${details}`;
  const container = $("#creator-tool-log");
  container.appendChild(entry);
  container.scrollTop = container.scrollHeight;
  state.creatorToolLogCount += 1;
  $("#creator-tool-log-count").textContent = `${state.creatorToolLogCount} 条`;
}

function addLog(speaker, text) {
  const entry = document.createElement("div");
  entry.className = "log-entry";
  entry.innerHTML = `<strong>${escapeHtml(speaker)}</strong><span>${escapeHtml(text || "")}</span>`;
  $("#play-log").appendChild(entry);
  $("#play-log").scrollTop = $("#play-log").scrollHeight;
}

function uniqueId(prefix, seed = "") {
  const base = String(seed || prefix)
    .trim()
    .replace(/[^\w\u4e00-\u9fa5-]+/g, "_")
    .replace(/^_+|_+$/g, "")
    .slice(0, 32) || prefix;
  let id = `${prefix}_${base}`;
  let index = 2;
  const used = new Set([...state.project.nodes.map((node) => node.id), ...state.project.characters.map((item) => item.id)]);
  while (used.has(id)) {
    id = `${prefix}_${base}_${index}`;
    index += 1;
  }
  return id;
}

function pick(source, keys) {
  const result = {};
  for (const key of keys) {
    if (source[key] !== undefined) result[key] = source[key];
  }
  return result;
}

function objectOrEmpty(value) {
  return value && typeof value === "object" && !Array.isArray(value) ? value : {};
}

function uniqueStrings(items) {
  return Array.from(new Set(items.filter(Boolean).map((item) => String(item))));
}

function addNode(type) {
  syncProjectForm();
  syncNodeForm();
  pushUndo("新增节点");
  const id = type === "ending" ? uid("ending") : type === "choice" ? uid("choice") : uid("story");
  state.project.nodes.push({
    id,
    type,
    x: 180 + state.project.nodes.length * 42,
    y: 220 + state.project.nodes.length * 24,
    title: type === "ending" ? "新结局" : type === "choice" ? "新选择" : "新剧情",
    character: state.project.characters[0]?.id || "",
    background: "",
    content: "",
    conditions: {},
    effects: {},
    next: "",
    choices: type === "choice" ? [{ text: "选项", next: "", effects: {}, conditions: {} }] : [],
  });
  state.selectedNodeId = id;
  renderNodeCanvas();
  renderNodeForm();
  updateCompiledPreview();
}

function deleteSelectedNode() {
  if (!state.selectedNodeId) return;
  if (state.project.nodes.length <= 1) return;
  pushUndo("删除节点");
  const deleted = state.selectedNodeId;
  state.project.nodes = state.project.nodes.filter((node) => node.id !== deleted);
  for (const node of state.project.nodes) {
    if (node.next === deleted) node.next = "";
    node.choices = (node.choices || []).map((choice) => (choice.next === deleted ? { ...choice, next: "" } : choice));
  }
  state.selectedNodeId = state.project.nodes[0]?.id || "";
  renderNodeCanvas();
  renderNodeForm();
  updateCompiledPreview();
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function escapeAttr(value) {
  return escapeHtml(value);
}

function bindEvents() {
  $("#refresh-worlds").addEventListener("click", () => loadWorlds({ announce: true }).catch(alertError));
  $("#load-world").addEventListener("click", () => loadSelectedWorld().catch(alertError));
  $("#world-picker").addEventListener("change", () => loadSelectedWorld().catch(alertError));
  $("#new-project").addEventListener("click", () => {
    if (!confirmDiscardUnsavedChanges()) return;
    state.projectLoadVersion += 1;
    state.initialProjectLoading = false;
    $("#preview-creator-change").disabled = false;
    const project = emptyProject();
    state.currentWorldId = project.world.world_id;
    state.artifactSourceWorldId = "";
    setProject(project);
    loadCreatorHistory().catch(console.warn);
    state.lastSavedProjectFingerprint = projectFingerprint();
    loadCreatorVersions().catch(console.warn);
    setProjectActionStatus("success", "已新建空白项目", "请先填写醒目的作品名称，再添加节点或让 Creator Agent 创作。 ");
    $("#project-name").focus();
  });
  $("#add-character").addEventListener("click", () => {
    pushUndo("新增角色");
    const id = uid("npc");
    state.project.characters.push({ id, name: "新角色", role: "NPC", location: $("#player-location").value || "开场", portrait: "" });
    renderCharacters();
    renderNodeForm();
    updateCompiledPreview();
  });
  $("#character-list").addEventListener("input", (event) => {
    const target = event.target;
    const id = target.dataset.characterId;
    const field = target.dataset.characterField;
    const character = state.project.characters.find((item) => item.id === id);
    if (!character || !field) return;
    const oldId = character.id;
    character[field] = target.value;
    if (field === "id" && target.value && target.value !== oldId) {
      for (const node of state.project.nodes) {
        if (node.character === oldId) node.character = target.value;
      }
      for (const other of state.project.characters) {
        if (other !== character && other.id === target.value) character.id = oldId;
      }
    }
    renderCharacterOptions();
    renderNodeCanvas();
    updateCompiledPreview();
  });
  $("#character-list").addEventListener("click", (event) => {
    const id = event.target.dataset.deleteCharacter;
    if (!id) return;
    pushUndo("删除角色");
    state.project.characters = state.project.characters.filter((item) => item.id !== id);
    renderCharacters();
    renderNodeCanvas();
    updateCompiledPreview();
  });
  for (const button of $$("[data-add-node]")) {
    button.addEventListener("click", () => addNode(button.dataset.addNode));
  }
  // Bind panning to the canvas contents instead of the scroll container. This
  // keeps the browser's horizontal and vertical scrollbar tracks fully native.
  $("#canvas-space").addEventListener("mousedown", (event) => {
    const nodeEl = event.target.closest(".story-node");
    if (!nodeEl) {
      if (event.button !== 0 && event.button !== 1) return;
      const panel = $(".canvas-panel");
      state.panning = {
        lastX: event.clientX,
        lastY: event.clientY,
      };
      panel.classList.add("panning");
      event.preventDefault();
      return;
    }
    if (event.button !== 0) return;
    syncNodeForm();
    state.selectedNodeId = nodeEl.dataset.nodeId;
    const node = nodeById(state.selectedNodeId);
    state.dragging = {
      node,
      startX: event.clientX,
      startY: event.clientY,
      originX: node.x || 0,
      originY: node.y || 0,
      historyCaptured: false,
    };
    renderNodeCanvas();
    renderNodeForm();
  });
  $(".canvas-panel").addEventListener("auxclick", (event) => {
    if (event.button === 1) event.preventDefault();
  });
  $(".canvas-panel").addEventListener(
    "wheel",
    (event) => {
      const panel = $(".canvas-panel");
      const scale = wheelPixels(event, panel);
      const deltaX = event.deltaX * scale;
      const deltaY = event.deltaY * scale;

      // Ctrl/Cmd + wheel is the deliberate zoom gesture. Plain trackpad
      // horizontal swipes and Shift + wheel must remain navigation gestures.
      if (!event.ctrlKey && !event.metaKey) {
        event.preventDefault();
        if (event.shiftKey) {
          panel.scrollLeft += Math.abs(deltaX) > 0.5 ? deltaX : deltaY;
          ensureCanvasPanRoom(panel);
          return;
        }
        panel.scrollLeft += deltaX;
        panel.scrollTop += deltaY;
        ensureCanvasPanRoom(panel);
        return;
      }

      event.preventDefault();
      const rect = panel.getBoundingClientRect();
      const pointerX = event.clientX - rect.left;
      const pointerY = event.clientY - rect.top;
      const worldX = (panel.scrollLeft + pointerX) / state.zoom - state.canvasPanMargin;
      const worldY = (panel.scrollTop + pointerY) / state.zoom - state.canvasPanMargin;
      const factor = deltaY < 0 ? 1.1 : 0.9;
      state.zoom = Math.max(0.35, Math.min(2.5, state.zoom * factor));
      applyZoom();
      panel.scrollLeft = (worldX + state.canvasPanMargin) * state.zoom - pointerX;
      panel.scrollTop = (worldY + state.canvasPanMargin) * state.zoom - pointerY;
      ensureCanvasPanRoom(panel);
    },
    { passive: false }
  );
  window.addEventListener("mousemove", (event) => {
    if (state.panning) {
      const panel = $(".canvas-panel");
      const dx = event.clientX - state.panning.lastX;
      const dy = event.clientY - state.panning.lastY;
      panel.scrollLeft -= dx;
      panel.scrollTop -= dy;
      ensureCanvasPanRoom(panel);
      state.panning.lastX = event.clientX;
      state.panning.lastY = event.clientY;
      event.preventDefault();
      return;
    }
    if (!state.dragging) return;
    const drag = state.dragging;
    if (!drag.historyCaptured) {
      pushUndo("移动节点");
      drag.historyCaptured = true;
    }
    drag.node.x = Math.max(20, drag.originX + (event.clientX - drag.startX) / state.zoom);
    drag.node.y = Math.max(20, drag.originY + (event.clientY - drag.startY) / state.zoom);
    renderNodeCanvas();
  });
  window.addEventListener("mouseup", () => {
    if (state.panning) {
      state.panning = null;
      $(".canvas-panel").classList.remove("panning");
      return;
    }
    if (!state.dragging) return;
    state.dragging = null;
    updateCompiledPreview();
  });
  $("#node-form").addEventListener("input", () => {
    syncNodeForm();
    renderNodeCanvas();
    updateCompiledPreview();
  });
  $("#node-form").addEventListener("change", () => {
    syncNodeForm();
    renderNodeCanvas();
    renderNodeForm();
    updateCompiledPreview();
  });
  $("#add-choice").addEventListener("click", (event) => {
    event.preventDefault();
    const node = nodeById(state.selectedNodeId);
    if (!node) return;
    pushUndo("新增选项");
    node.choices = node.choices || [];
    node.choices.push({ text: "新选项", next: "", effects: {}, conditions: {} });
    renderChoiceList(node);
    renderNodeCanvas();
    updateCompiledPreview();
  });
  $("#choice-list").addEventListener("input", syncChoiceInput);
  $("#choice-list").addEventListener("change", syncChoiceInput);
  $("#choice-list").addEventListener("click", (event) => {
    const index = event.target.dataset.deleteChoice;
    if (index === undefined) return;
    const node = nodeById(state.selectedNodeId);
    pushUndo("删除选项");
    node.choices.splice(Number(index), 1);
    renderChoiceList(node);
    renderNodeCanvas();
    updateCompiledPreview();
  });
  $("#delete-node").addEventListener("click", deleteSelectedNode);
  ["world-id", "player-name", "player-location", "world-lore", "player-stats-json", "items-text"].forEach((id) => {
    $(`#${id}`).addEventListener("input", () => {
      syncProjectForm();
      updateCompiledPreview();
    });
  });
  ["project-name", "world-name"].forEach((id) => {
    $(`#${id}`).addEventListener("input", () => {
      const other = id === "project-name" ? $("#world-name") : $("#project-name");
      other.value = $(`#${id}`).value;
      syncProjectForm();
      updateCompiledPreview();
    });
  });
  $("#compile-preview").addEventListener("click", previewCompilation);
  $("#save-creator-world").addEventListener("click", () => saveCreatorWorld().catch(alertError));
  $("#save-as-new-world").addEventListener("click", () => saveAsNewWorld().catch(alertError));
  $("#recover-visual-assets").addEventListener("click", () => recoverRecentVisualAssets().catch(alertError));
  $("#open-player").addEventListener("click", () => openPlayerExperience().catch(alertError));
  $("#start-playtest").addEventListener("click", () => startPlaytest().catch(alertError));
  $("#refresh-session").addEventListener("click", () => startPlaytest().catch(alertError));
  $("#open-playtest-panel").addEventListener("click", focusPlaytestPanel);
  $("#chat-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const message = $("#chat-message").value.trim();
    if (!message) return;
    $("#chat-message").value = "";
    sendChat(message).catch(alertError);
  });
  $("#creator-agent-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const message = $("#creator-agent-message").value.trim();
    if (!message) return;
    $("#creator-agent-message").value = "";
    sendCreatorAgentMessage(message).catch(alertError);
  });
  $("#creator-agent-message").addEventListener("keydown", (event) => {
    if (event.key !== "Enter" || event.shiftKey || event.isComposing) return;
    event.preventDefault();
    $("#creator-agent-form").requestSubmit();
  });
  const dockResizer = $("#creator-dock-resizer");
  dockResizer.addEventListener("pointerdown", (event) => {
    event.preventDefault();
    dockResizer.setPointerCapture(event.pointerId);
    state.creatorDockResize = {
      pointerId: event.pointerId,
      startY: event.clientY,
      startHeight: $(".creator-command-dock").getBoundingClientRect().height,
    };
  });
  dockResizer.addEventListener("pointermove", (event) => {
    const resize = state.creatorDockResize;
    if (!resize || resize.pointerId !== event.pointerId) return;
    setCreatorDockHeight(resize.startHeight + resize.startY - event.clientY);
  });
  const stopDockResize = (event) => {
    if (!state.creatorDockResize || state.creatorDockResize.pointerId !== event.pointerId) return;
    if (dockResizer.hasPointerCapture(event.pointerId)) dockResizer.releasePointerCapture(event.pointerId);
    state.creatorDockResize = null;
  };
  dockResizer.addEventListener("pointerup", stopDockResize);
  dockResizer.addEventListener("pointercancel", stopDockResize);
  dockResizer.addEventListener("keydown", (event) => {
    if (!["ArrowUp", "ArrowDown"].includes(event.key)) return;
    event.preventDefault();
    const direction = event.key === "ArrowUp" ? 1 : -1;
    const step = event.shiftKey ? 80 : 32;
    setCreatorDockHeight($(".creator-command-dock").getBoundingClientRect().height + direction * step);
  });
  $("#cancel-creator-agent").addEventListener("click", () => cancelCreatorAgentWork().catch(alertError));
  $("#apply-creator-change").addEventListener("click", () => applyPendingChange().catch(alertError));
  $("#reject-creator-change").addEventListener("click", rejectPendingChange);
  $("#undo-project").addEventListener("click", undoProject);
  $("#redo-project").addEventListener("click", redoProject);
  $("#create-version").addEventListener("click", () => createCreatorVersion("手动快照").catch(alertError));
  $("#restore-version").addEventListener("click", () => restoreCreatorVersion().catch(alertError));
  $("#creator-version-picker").addEventListener("change", () => {
    $("#restore-version").disabled = !$("#creator-version-picker").value;
  });
  $("#clear-creator-agent").addEventListener("click", () => {
    if (!window.confirm("确定清空当前项目的创作对话和本页工具日志吗？")) return;
    clearCreatorHistory().catch(alertError);
  });

  $("#node-form").addEventListener("focusin", () => captureEditBaseline("编辑节点"));
  $("#node-form").addEventListener("focusout", (event) => {
    if (!event.currentTarget.contains(event.relatedTarget)) commitEditBaseline();
  });
  $("#character-list").addEventListener("focusin", () => captureEditBaseline("编辑角色"));
  $("#character-list").addEventListener("focusout", (event) => {
    if (!event.currentTarget.contains(event.relatedTarget)) commitEditBaseline();
  });
  $("#choice-list").addEventListener("focusin", () => captureEditBaseline("编辑选项"));
  $("#choice-list").addEventListener("focusout", (event) => {
    if (!event.currentTarget.contains(event.relatedTarget)) commitEditBaseline();
  });
  ["project-name", "world-id", "world-name", "player-name", "player-location", "world-lore", "player-stats-json", "items-text"].forEach((id) => {
    $(`#${id}`).addEventListener("focusin", () => captureEditBaseline("编辑世界设定"));
    $(`#${id}`).addEventListener("change", commitEditBaseline);
  });
  $("#import-world-json").addEventListener("click", () => $("#world-json-file").click());
  $("#world-json-file").addEventListener("change", importWorldFile);
}

function syncChoiceInput(event) {
  const target = event.target;
  const index = Number(target.dataset.choiceIndex);
  const field = target.dataset.choiceField;
  const node = nodeById(state.selectedNodeId);
  if (!node || Number.isNaN(index) || !field) return;
  const choice = node.choices[index];
  if (!choice) return;
  if (field === "effects") choice.effects = safeJson(target.value, {}, "选项效果 JSON");
  else if (field === "conditions") choice.conditions = safeJson(target.value, {}, "选项条件 JSON");
  else choice[field] = target.value;
  renderNodeCanvas();
  updateCompiledPreview();
}

async function importWorldFile(event) {
  const file = event.target.files?.[0];
  if (!file) return;
  const text = await file.text();
  const data = JSON.parse(text);
  if (data.metadata?.creator_graph) setProject(data.metadata.creator_graph);
  else if (data.world_id && data.npcs) setProject(worldToCreatorGraph(data));
  else if (data.version === "creator_graph.v1") setProject(data);
  else throw new Error("无法识别的 JSON 格式");
  event.target.value = "";
}

function alertError(error) {
  console.error(error);
  const message = cleanApiError(error);
  const status = $("#project-action-status");
  if (!status.classList.contains("error")) {
    setProjectActionStatus("error", "操作失败", message);
  }
}

bindEvents();
$("#preview-creator-change").disabled = state.initialProjectLoading;
setProject(emptyProject(), { updateUrl: !initialRequestedWorldId });
renderCreatorHistory([]);
state.lastSavedProjectFingerprint = projectFingerprint();
window.addEventListener("beforeunload", (event) => {
  if (!isProjectDirty()) return;
  event.preventDefault();
  event.returnValue = "";
});
loadWorlds()
  .then(async () => {
    const requestedWorld = initialRequestedWorldId;
    if (!requestedWorld) return;
    if (state.worlds.some((world) => world.world_id === requestedWorld)) {
      $("#world-picker").value = requestedWorld;
      await loadSelectedWorld();
      return;
    }
    const project = emptyProject();
    project.world.world_id = requestedWorld;
    state.currentWorldId = requestedWorld;
    setProject(project);
    state.lastSavedProjectFingerprint = projectFingerprint();
    await loadCreatorHistory();
    await loadCreatorVersions();
    await recoverLatestCreatorWorkflow();
  })
  .catch(console.warn)
  .finally(() => {
    state.initialProjectLoading = false;
    $("#preview-creator-change").disabled = false;
  });
loadCreatorTools().catch((error) => {
  console.warn("creator MCP tool discovery failed", error);
  const container = $("#creator-tool-list");
  if (container) container.innerHTML = "<span>工具目录读取失败</span>";
});
