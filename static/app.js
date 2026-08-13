const state = {
  worlds: [],
  templates: [],
  currentWorldId: null,
  currentWorld: null,
  dirty: false,
  suggestionsVisible: true,
  runtimeNpcs: [],
  nearbyNpcs: [],
  currentPlayer: {},
  busy: false,
  generatingWorld: false,
  importingWorld: false,
  generationTimer: null,
  importTimer: null,
  previousPlayStatus: "",
  hydrating: false,
};

const $ = (selector) => document.querySelector(selector);
const sessionKey = (worldId) => `npc-agent-session:${worldId}`;

const complexityPresets = {
  simple: { min_npcs: 3, min_tasks: 5, min_actions: 5 },
  medium: { min_npcs: 5, min_tasks: 8, min_actions: 8 },
  complex: { min_npcs: 8, min_tasks: 12, min_actions: 14 },
  ultra: { min_npcs: 12, min_tasks: 18, min_actions: 22 },
};

const fields = {
  worldId: $("#world-id"),
  name: $("#world-name"),
  description: $("#world-description"),
  lore: $("#world-lore"),
  openingScene: $("#opening-scene"),
  playerName: $("#player-name-config"),
  playerLocation: $("#player-location-config"),
  playerRole: $("#player-role-config"),
  playerState: $("#player-state-config"),
  playerExtra: $("#player-extra-json"),
  npcs: $("#npcs-json"),
  goals: $("#goals-text"),
  tasks: $("#tasks-json"),
  actions: $("#actions-json"),
};

function pretty(value) {
  return JSON.stringify(value ?? {}, null, 2);
}

function parseJsonField(field, fallback) {
  const text = field.value.trim();
  if (!text) return fallback;
  try {
    return JSON.parse(text);
  } catch (error) {
    throw new Error(`${field.previousSibling?.textContent?.trim() || "JSON"} 格式错误：${error.message}`);
  }
}

function splitPlayer(player) {
  const source = { ...(player || {}) };
  const name = source.name || "";
  const location = source.location || "";
  const role = source.role || source.identity || source.faction || source.class || "";
  const status = source.status || source.state || source.description || "";
  delete source.name;
  delete source.location;
  delete source.role;
  delete source.identity;
  delete source.faction;
  delete source.class;
  delete source.status;
  delete source.state;
  delete source.description;
  return { name, location, role, status, extra: source };
}

function readPlayerConfig() {
  const extra = parseJsonField(fields.playerExtra, {});
  const player = { ...extra };
  player.name = fields.playerName.value.trim() || "玩家";
  player.location = fields.playerLocation.value.trim() || "起始地点";
  if (fields.playerRole.value.trim()) player.role = fields.playerRole.value.trim();
  if (fields.playerState.value.trim()) player.status = fields.playerState.value.trim();
  return player;
}

function syncComposerFromPlayer(player) {
  if (!player) return;
  $("#player-name").value = player.name || "玩家";
  state.currentPlayer = player;
  renderCurrentLocation(player.location || "起始地点");
}

function renderCurrentLocation(location) {
  $("#current-location").textContent = `地点：${location || "起始地点"}`;
  const input = $("#location-input");
  if (input) input.value = location || "";
}

function lines(text) {
  return text
    .split("\n")
    .map((item) => item.trim())
    .filter(Boolean);
}

function formatErrorMessage(text) {
  try {
    const data = JSON.parse(text);
    if (typeof data.detail === "string") return data.detail;
    if (Array.isArray(data.detail)) {
      return data.detail.map((item) => item.msg || JSON.stringify(item)).join("\n");
    }
    return data.detail ? JSON.stringify(data.detail) : text;
  } catch {
    return text;
  }
}

async function request(url, options = {}) {
  const response = await fetch(url, {
    headers: { "Content-Type": "application/json; charset=utf-8", ...(options.headers || {}) },
    ...options,
  });
  if (!response.ok) {
    const text = await response.text();
    throw new Error(formatErrorMessage(text));
  }
  return response.status === 204 ? null : response.json();
}

function renderWorldTemplates() {
  const select = $("#generator-template");
  if (!select || !state.templates.length) return;
  select.innerHTML = state.templates
    .map(
      (template) =>
        `<option value="${escapeHtml(template.id)}" title="${escapeHtml(template.description || "")}">${escapeHtml(template.name)}</option>`
    )
    .join("");
}

async function loadWorldTemplates() {
  try {
    state.templates = await request("/api/world-templates");
    renderWorldTemplates();
  } catch (error) {
    setGeneratorStatus(`模板列表加载失败，已使用页面默认模板。${error.message}`, true);
  }
}

function setGeneratorStatus(message, isError = false) {
  const status = $("#generator-status");
  if (!status) return;
  status.textContent = message || "";
  status.classList.toggle("error", Boolean(isError));
}

function setGeneratingWorld(enabled, startedAt = Date.now()) {
  state.generatingWorld = enabled;
  const button = $("#generate-world");
  if (state.generationTimer) {
    clearInterval(state.generationTimer);
    state.generationTimer = null;
  }
  if (!enabled) {
    button.disabled = false;
    button.textContent = "生成可玩世界观";
    return;
  }

  button.disabled = true;
  button.textContent = "正在生成...";
  const updateStatus = () => {
    const seconds = Math.max(1, Math.round((Date.now() - startedAt) / 1000));
    setGeneratorStatus(`正在调用 Agent 生成世界观，已等待 ${seconds} 秒。请不要重复点击。`);
  };
  updateStatus();
  state.generationTimer = setInterval(updateStatus, 1000);
}

function setImportStatus(message, isError = false) {
  const status = $("#import-status");
  if (!status) return;
  status.textContent = message || "";
  status.classList.toggle("error", Boolean(isError));
}

function setImportingWorld(enabled, startedAt = Date.now()) {
  state.importingWorld = enabled;
  const button = $("#import-world");
  if (state.importTimer) {
    clearInterval(state.importTimer);
    state.importTimer = null;
  }
  if (!enabled) {
    button.disabled = false;
    button.textContent = "从文档导入世界观";
    return;
  }

  button.disabled = true;
  button.textContent = "正在导入...";
  const updateStatus = () => {
    const seconds = Math.max(1, Math.round((Date.now() - startedAt) / 1000));
    setImportStatus(`正在读取文档并生成可玩世界，已等待 ${seconds} 秒。`);
  };
  updateStatus();
  state.importTimer = setInterval(updateStatus, 1000);
}

async function loadWorlds() {
  state.worlds = await request("/api/worlds");
  renderWorldList();
  if (!state.currentWorldId && state.worlds.length) {
    await selectWorld(state.worlds[0].world_id);
  }
}

function renderWorldList() {
  $("#world-list").innerHTML = state.worlds
    .map(
      (world) => `
        <button class="world-card ${world.world_id === state.currentWorldId ? "active" : ""}" data-id="${escapeHtml(world.world_id)}">
          <strong>${escapeHtml(world.name)}</strong>
          <span>${escapeHtml(world.kind)} · ${escapeHtml(world.world_id)}</span>
          <p>${escapeHtml(world.description || "暂无简介")}</p>
        </button>
      `,
    )
    .join("");
}

async function selectWorld(worldId) {
  state.currentWorldId = worldId;
  renderWorldList();
  clearRunState();

  setEditorEnabled(true);
  const config = await request(`/api/worlds/${encodeURIComponent(worldId)}`);
  state.currentWorld = config;
  $("#current-world-name").textContent = config.name;
  fillEditor(config);
  await restoreRunSession(worldId);
}

function fillEditor(config) {
  fields.worldId.value = config.world_id || "";
  fields.name.value = config.name || "";
  fields.description.value = config.description || "";
  fields.lore.value = config.lore || "";
  fields.openingScene.value = config.opening_scene || "";
  const player = splitPlayer(config.player || {});
  fields.playerName.value = player.name;
  fields.playerLocation.value = player.location;
  fields.playerRole.value = player.role;
  fields.playerState.value = player.status;
  fields.playerExtra.value = pretty(player.extra || {});
  syncComposerFromPlayer(config.player || {});
  fields.npcs.value = pretty(config.npcs || []);
  renderNpcSelector(config.npcs || []);
  fields.goals.value = (config.story_goals || []).join("\n");
  fields.tasks.value = pretty(config.tasks || []);
  fields.actions.value = pretty(config.actions || []);
  renderMvpChecklist(config.tasks || []);
  renderWorldActions();
  state.dirty = false;
}

function readEditor() {
  return {
    world_id: fields.worldId.value.trim(),
    name: fields.name.value.trim() || "未命名世界观",
    description: fields.description.value.trim(),
    lore: fields.lore.value.trim(),
    opening_scene: fields.openingScene.value.trim(),
    player: readPlayerConfig(),
    npcs: parseJsonField(fields.npcs, []),
    story_goals: lines(fields.goals.value),
    tasks: parseJsonField(fields.tasks, []),
    actions: parseJsonField(fields.actions, []),
    initial_memories: state.currentWorld?.initial_memories || [],
    metadata: state.currentWorld?.metadata || {},
  };
}

function setEditorEnabled(enabled) {
  Object.values(fields).forEach((field) => {
    field.disabled = !enabled;
  });
  $("#save-world").disabled = !enabled;
  $("#load-example").disabled = !enabled;
  $("#delete-world").disabled = !enabled;
}

async function createWorld() {
  const config = await request("/api/worlds", { method: "POST" });
  await loadWorlds();
  await selectWorld(config.world_id);
}

async function generateWorld() {
  if (state.generatingWorld) return;
  const startedAt = Date.now();
  setGeneratingWorld(true, startedAt);
  try {
    const minNpcs = Number($("#generator-min-npcs").value || 0);
    const minTasks = Number($("#generator-min-tasks").value || 0);
    const minActions = Number($("#generator-min-actions").value || 0);
    const config = await request("/api/worlds/generate", {
      method: "POST",
      body: JSON.stringify({
        template: $("#generator-template").value,
        theme: $("#generator-theme").value.trim(),
        player_name: $("#generator-player").value.trim() || "主角",
        complexity: $("#generator-complexity").value,
        min_npcs: minNpcs || null,
        min_tasks: minTasks || null,
        min_actions: minActions || null,
        final_task_requires_previous: $("#generator-final-gate").checked,
      }),
    });
    await loadWorlds();
    await selectWorld(config.world_id);
    const seconds = Math.max(1, Math.round((Date.now() - startedAt) / 1000));
    setGeneratorStatus(`生成完成：${config.name}（耗时 ${seconds} 秒）`);
    addMessage("system", `已生成世界观：${config.name}。现在可以直接点击“开始游戏”。`, "系统");
  } catch (error) {
    setGeneratorStatus(`生成失败：${error.message}`, true);
    throw error;
  } finally {
    setGeneratingWorld(false);
  }
}

function applyComplexityPreset() {
  const preset = complexityPresets[$("#generator-complexity").value] || complexityPresets.medium;
  $("#generator-min-npcs").value = preset.min_npcs;
  $("#generator-min-tasks").value = preset.min_tasks;
  $("#generator-min-actions").value = preset.min_actions;
}

async function importWorld() {
  if (state.importingWorld) return;
  const input = $("#import-world-file");
  const file = input.files?.[0];
  if (!file) {
    setImportStatus("请先选择一个 Word、PDF、Markdown、TXT 或 JSON 文档。", true);
    return;
  }

  const startedAt = Date.now();
  setImportingWorld(true, startedAt);
  try {
    const body = new FormData();
    body.append("file", file);
    body.append("player_name", $("#generator-player").value.trim() || "主角");
    body.append("world_name", $("#import-world-name").value.trim());
    const response = await fetch("/api/worlds/import", { method: "POST", body });
    if (!response.ok) {
      throw new Error(formatErrorMessage(await response.text()));
    }
    const config = await response.json();
    await loadWorlds();
    await selectWorld(config.world_id);
    const seconds = Math.max(1, Math.round((Date.now() - startedAt) / 1000));
    setImportStatus(`导入完成：${config.name}（耗时 ${seconds} 秒）`);
    addMessage("system", `已从文档导入世界观：${config.name}。系统已生成 NPC、任务、动作和闭环验证。`, "系统");
  } catch (error) {
    setImportStatus(`导入失败：${error.message}`, true);
    throw error;
  } finally {
    setImportingWorld(false);
  }
}

async function saveWorld() {
  if (!state.currentWorldId) return;
  const config = readEditor();
  const saved = await request(`/api/worlds/${encodeURIComponent(config.world_id)}`, {
    method: "PUT",
    body: JSON.stringify(config),
  });
  state.currentWorld = saved;
  state.currentWorldId = saved.world_id;
  $("#current-world-name").textContent = saved.name;
  addMessage("system", `已保存世界观：${saved.name}`, "系统");
  await loadWorlds();
}

async function deleteCurrentWorld() {
  if (!state.currentWorldId) return;
  const worldName = state.currentWorld?.name || fields.name.value || state.currentWorldId;
  const confirmed = window.confirm(`确定删除世界观「${worldName}」吗？该操作不可恢复。`);
  if (!confirmed) return;

  const deletedId = state.currentWorldId;
  await request(`/api/worlds/${encodeURIComponent(deletedId)}`, { method: "DELETE" });
  localStorage.removeItem(sessionKey(deletedId));
  state.currentWorldId = null;
  state.currentWorld = null;
  state.dirty = false;
  $("#messages").innerHTML = "";
  addMessage("system", `已删除世界观：${worldName}`, "系统");
  await loadWorlds();
}

function clearChatSession() {
  if (state.currentWorldId) {
    localStorage.removeItem(sessionKey(state.currentWorldId));
  }
  state.hydrating = true;
  try {
    $("#messages").innerHTML = "";
    $("#suggested-actions").innerHTML = "";
    $("#memory-result").textContent = "暂无";
  } finally {
    state.hydrating = false;
  }
  setBusy(false);
  $("#play-status").textContent = state.currentWorldId
    ? `已清空记录：${state.currentWorldId}。点击“开始游戏”重新开始。`
    : "已清空记录。";
  $("#start-world").textContent = "开始游戏";
  addMessage("system", "聊天记录和本地游玩存档已清空。可以点击“开始游戏”重新开始。", "系统");
}

async function startWorld() {
  if (!state.currentWorldId) return;
  if (state.dirty) {
    await saveWorld();
  }
  const data = await request(`/api/worlds/${encodeURIComponent(state.currentWorldId)}/start`, { method: "POST" });
  $("#messages").innerHTML = "";
  addMessage("system", data.narration, "世界");
  renderRuntime(data);
  $("#play-status").textContent = `正在运行：${state.currentWorldId}`;
  $("#start-world").textContent = "重新开始游戏";
  saveRunSession();
}

async function sendChat(message) {
  if (!$("#target-npc").value) {
    addMessage("system", "当前位置没有可对话 NPC。请先前往有 NPC 的地点，或用“执行”观察当前位置。", "系统");
    return;
  }
  const selectedTarget = $("#target-npc").value || "";
  const isGroupChat = selectedTarget === "__nearby__";
  const participantIds = isGroupChat
    ? (state.nearbyNpcs?.length ? state.nearbyNpcs : nearbyNpcsForLocation(state.currentPlayer?.location)).map((npc) => npc.id).filter(Boolean)
    : [];
  addMessage("player", message, $("#player-name").value || "玩家");
  const waitingName = currentNpcName();
  const waiting = showWaiting(`${waitingName}回复中...`, waitingName);
  try {
    const data = await request(`/api/worlds/${encodeURIComponent(state.currentWorldId)}/chat`, {
      method: "POST",
      body: JSON.stringify({
        message,
        player_name: $("#player-name").value || "玩家",
        location: state.currentPlayer?.location || fields.playerLocation.value || "起始地点",
        player_goal: fields.goals.value.split("\n")[0] || "",
        target_npc_id: isGroupChat ? "" : selectedTarget,
        target_npc_ids: participantIds,
        group_chat: isGroupChat,
        max_npc_replies: 4,
      }),
    });
    waiting.remove();
    if (data.action_type === "wait") {
      addMessage("system", data.reply, "模型响应");
    } else if (Array.isArray(data.messages) && data.messages.length) {
      data.messages.forEach((item) => {
        addMessage(item.role || "npc", item.content, item.speaker || "NPC");
      });
    } else {
      addMessage("npc", data.reply, data.speaker?.name || data.active_entity?.name || "NPC");
    }
    renderRuntime(data);
  } catch (error) {
    waiting.remove();
    throw error;
  }
}

async function moveToLocation(location) {
  if (!state.currentWorldId) return;
  const nextLocation = location.trim();
  if (!nextLocation) return;
  state.currentPlayer = { ...(state.currentPlayer || {}), location: nextLocation };
  fields.playerLocation.value = nextLocation;
  renderCurrentLocation(nextLocation);
  const nearby = updateNearbyNpcsForLocation(nextLocation);
  const data = await request(`/api/worlds/${encodeURIComponent(state.currentWorldId)}/action`, {
    method: "POST",
    body: JSON.stringify({ action: "move_player", payload: { location: nextLocation } }),
  });
  addMessage("system", data.narration || `你前往：${nextLocation}`, "地点");
  renderRuntime(data);
  saveRunSession();
}

async function lookOrFind(text) {
  if (!state.currentWorldId) return;
  const query = text.trim();
  const location = state.currentPlayer?.location || fields.playerLocation.value || "起始地点";
  const wantsObservation = !query || /查看|观察|看看|四周|线索|异常|找人|寻找|附近|这里/.test(query);
  if (!$("#target-npc").value || wantsObservation) {
    const data = await request(`/api/worlds/${encodeURIComponent(state.currentWorldId)}/action`, {
      method: "POST",
      body: JSON.stringify({ action: "inspect_location", payload: { location, query } }),
    });
    addMessage("system", data.narration, "观察");
    renderRuntime(data);
    saveRunSession();
    return;
  }
  const message = query || `我在${location}观察四周，看看这里有什么人、线索或异常。`;
  await sendChat(message);
}

function safeParseActions() {
  try {
    return parseJsonField(fields.actions, []);
  } catch {
    return [];
  }
}

function safeParseTasks() {
  try {
    return parseJsonField(fields.tasks, []);
  } catch {
    return [];
  }
}

async function tickAgent() {
  const data = await request(`/api/worlds/${encodeURIComponent(state.currentWorldId)}/agent/tick`, {
    method: "POST",
    body: JSON.stringify({ max_steps: 1, objective: fields.goals.value.split("\n")[0] || "" }),
  });
  const text = data.executed.map((item) => item.narration).join("\n") || data.stopped_reason;
  addMessage("system", text, "Agent");
}

async function queryMemory() {
  const query = $("#memory-query").value.trim();
  if (!query || !state.currentWorldId) return;
  const data = await request(`/api/worlds/${encodeURIComponent(state.currentWorldId)}/memory/query`, {
    method: "POST",
    body: JSON.stringify({ query, limit: 8 }),
  });
  $("#memory-result").textContent = pretty(data.rag);
}

async function loadExperienceProfile() {
  const profile = await request("/api/experience/profile");
  renderExperienceProfile(profile);
  if (!profile.sample_count) return;
  if (!$("#generator-min-npcs").value) $("#generator-min-npcs").value = profile.recommended_npcs;
  if (!$("#generator-min-tasks").value) $("#generator-min-tasks").value = profile.recommended_tasks;
  if (!$("#generator-min-actions").value) $("#generator-min-actions").value = profile.recommended_actions;
}

function renderExperienceProfile(profile) {
  const box = $("#experience-profile");
  if (!box) return;
  box.textContent = profile?.summary || "暂无学习样本。";
}

async function submitExperienceFeedback() {
  if (!state.currentWorld) return;
  const tasks = Array.isArray(state.currentWorld.tasks) ? state.currentWorld.tasks : [];
  const npcs = Array.isArray(state.currentWorld.npcs) ? state.currentWorld.npcs : [];
  const actions = Array.isArray(state.currentWorld.actions) ? state.currentWorld.actions : [];
  const profile = await request("/api/experience/feedback", {
    method: "POST",
    body: JSON.stringify({
      world_id: state.currentWorld.world_id || state.currentWorldId || "",
      world_name: state.currentWorld.name || "",
      template: state.currentWorld.metadata?.template || "",
      complexity: state.currentWorld.metadata?.complexity?.key || "",
      npc_count: npcs.length,
      task_count: tasks.length,
      action_count: actions.length,
      immersion_score: Number($("#experience-score").value || 5),
      pacing: $("#experience-pacing").value,
      notes: $("#experience-notes").value.trim(),
    }),
  });
  renderExperienceProfile(profile);
  $("#experience-notes").value = "";
  addMessage("system", `体验反馈已记录：${profile.summary}`, "学习Agent");
}

function renderRuntime(data) {
  const serverNearby = data.nearby_npcs?.length ? data.nearby_npcs : null;
  if (data.npcs?.length) {
    state.runtimeNpcs = data.npcs;
    state.nearbyNpcs = serverNearby || fuzzyNpcsForLocation(data.player?.location || state.currentPlayer?.location);
    renderNpcSelector(state.runtimeNpcs, data.speaker?.id);
  } else if (data.speaker?.id && !state.runtimeNpcs.some((npc) => npc.id === data.speaker.id)) {
    state.runtimeNpcs = [...state.runtimeNpcs, data.speaker];
    state.nearbyNpcs = serverNearby || fuzzyNpcsForLocation(data.player?.location || state.currentPlayer?.location);
    renderNpcSelector(state.runtimeNpcs, data.speaker.id);
  }
  $("#inner-thought").textContent = data.inner_thought || "暂无";
  const traceBox = $("#llm-trace");
  if (traceBox) traceBox.textContent = pretty(data.debug_trace || {});
  if (data.quest_progress) {
    $("#quest-progress").textContent = data.quest_progress;
  }
  $("#player-state").textContent = pretty(data.player);
  renderPlayerSummary(data.player || {});
  if (data.player) {
    state.currentPlayer = data.player;
    renderCurrentLocation(data.player.location || "起始地点");
    state.nearbyNpcs = serverNearby || fuzzyNpcsForLocation(data.player.location);
    renderNpcSelector(state.runtimeNpcs, data.speaker?.id);
  }
  renderNearbyNpcs(state.nearbyNpcs, data.player?.location || state.currentPlayer?.location);
  $("#active-entity").textContent = pretty(data.active_entity || data.speaker || {});
  renderMvpChecklist(data.state?.tasks || []);
  renderWorldActions();
  renderSuggested(data.suggested_actions || []);
  if (!state.hydrating) saveRunSession();
}

function renderMvpChecklist(tasks) {
  const configuredTasks = tasks.length ? tasks : safeParseTasks();
  if (!configuredTasks.length) return;
  $("#mvp-checklist").innerHTML = configuredTasks
    .map((task) => {
      const done = task.status === "done";
      return `
        <li class="${done ? "done" : ""}">
          <div>
            <strong>${done ? "已完成" : "待完成"}：${escapeHtml(task.title || task.id)}</strong>
            ${task.description ? `<p>${escapeHtml(task.description)}</p>` : ""}
            ${task.completion ? `<p class="completion-rule">完成条件：${escapeHtml(formatCompletion(task.completion))}</p>` : ""}
          </div>
        </li>
      `;
    })
    .join("");
}

function renderWorldActions() {
  const locationInput = $("#location-input");
  if (locationInput && !locationInput.value) {
    locationInput.value = state.currentPlayer?.location || fields.playerLocation.value || "";
  }
}

function formatCompletion(completion) {
  if (!completion || typeof completion !== "object" || !Object.keys(completion).length) return "未配置";
  const parts = [];
  if (completion.items) parts.push(`持有 ${asArray(completion.items).join("、")}`);
  if (completion.keywords) parts.push(`说出关键词 ${asArray(completion.keywords).join("、")}`);
  if (completion.location) parts.push(`到达 ${completion.location}`);
  if (completion.actions) parts.push(`执行 ${asArray(completion.actions).join("、")}`);
  if (completion.stats) {
    Object.entries(completion.stats).forEach(([key, rule]) => parts.push(`${key} ${formatRule(rule)}`));
  }
  if (completion.relations) {
    Object.entries(completion.relations).forEach(([key, rule]) => parts.push(`好感/关系 ${key} ${formatRule(rule)}`));
  }
  if (completion.player) {
    Object.entries(completion.player).forEach(([key, value]) => parts.push(`${key} = ${formatValue(value)}`));
  }
  if (completion.flags) {
    Object.entries(completion.flags).forEach(([key, value]) => parts.push(`标记 ${key} = ${formatValue(value)}`));
  }
  return parts.join("；") || JSON.stringify(completion);
}

function formatRule(rule) {
  if (rule && typeof rule === "object") {
    if (rule.min !== undefined) return `>= ${rule.min}`;
    if (rule.max !== undefined) return `<= ${rule.max}`;
    if (rule.eq !== undefined) return `= ${rule.eq}`;
  }
  return `>= ${rule}`;
}

function asArray(value) {
  return Array.isArray(value) ? value : [value];
}

function renderSuggested(actions) {
  if (!state.suggestionsVisible) {
    $("#suggested-actions").innerHTML = "";
    return;
  }
  $("#suggested-actions").innerHTML = actions.length
    ? `<ul class="clue-list">${actions.map((action) => `<li>${escapeHtml(action)}</li>`).join("")}</ul>`
    : `<p class="muted">暂无。先问问当前 NPC：我现在该做什么？去哪里？这里有什么人？</p>`;
}

function currentNpcName() {
  const selected = $("#target-npc")?.selectedOptions?.[0]?.textContent?.trim();
  if ($("#target-npc")?.value === "__nearby__") return "在场 NPC";
  if (selected && selected !== "NPC") {
    return selected.split(" · ")[0] || selected;
  }
  const current = state.runtimeNpcs.find((npc) => npc.id === $("#target-npc")?.value) || state.runtimeNpcs[0];
  return current?.name || "NPC";
}

function normalizeLocation(value) {
  return String(value || "").trim();
}

function nearbyNpcsForLocation(location) {
  const current = normalizeLocation(location);
  if (!current) return [];
  return state.runtimeNpcs.filter((npc) => normalizeLocation(npc.location) === current);
}

function fuzzyNpcsForLocation(location) {
  const current = normalizeLocation(location);
  if (!current) return [];
  const exact = nearbyNpcsForLocation(current);
  if (exact.length) return exact;
  return state.runtimeNpcs.filter((npc) => {
    const npcLocation = normalizeLocation(npc.location);
    return npcLocation && (npcLocation.includes(current) || current.includes(npcLocation));
  });
}

function updateNearbyNpcsForLocation(location) {
  const nearby = fuzzyNpcsForLocation(location);
  state.nearbyNpcs = nearby;
  renderNearbyNpcs(nearby, location);
  renderNpcSelector(state.runtimeNpcs, nearby[0]?.id || "");
  return nearby;
}

function renderNearbyNpcs(npcs, location = "") {
  const target = $("#nearby-npcs");
  if (!target) return;
  const current = normalizeLocation(location);
  if (!npcs?.length) {
    target.classList.add("muted");
    target.innerHTML = current ? `「${escapeHtml(current)}」暂无已知 NPC` : "暂无";
    return;
  }
  target.classList.remove("muted");
  target.innerHTML = `<ul class="npc-list-items">${npcs
    .map((npc) => {
      const npcLocation = normalizeLocation(npc.location);
      const locationText = npcLocation && npcLocation !== current ? ` · ${escapeHtml(npcLocation)}` : "";
      return `<li><strong>${escapeHtml(npc.name || npc.id)}</strong><span>${escapeHtml(npc.role || "NPC")}${locationText}</span></li>`;
    })
    .join("")}</ul>`;
}

function renderPlayerSummary(player) {
  const summary = $("#player-summary");
  if (!summary) return;
  const data = player || {};
  const rows = [];
  const basics = [
    ["姓名", data.name],
    ["地点", data.location],
    ["身份", data.role || data.identity],
    ["状态", data.status],
    ["境界", data.realm],
    ["战力", data.battle_power],
    ["修为", data.cultivation],
    ["灵石", data.spirit_stones],
  ].filter(([, value]) => value !== undefined && value !== null && value !== "");

  if (basics.length) {
    rows.push(`<div class="summary-grid">${basics.map(([label, value]) => `<span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong>`).join("")}</div>`);
  }

  const statRows = collectPlayerStats(data);
  if (statRows.length) {
    rows.push(`
      <div class="summary-block">
        <strong>技能 / 熟练度</strong>
        <div class="summary-grid small">${statRows.map((item) => `<span>${escapeHtml(item.label)}</span><strong>${escapeHtml(item.value)}</strong>`).join("")}</div>
      </div>
    `);
  }

  const items = collectPlayerItems(data);
  rows.push(`
    <div class="summary-block">
      <strong>已获得</strong>
      ${items.obtained.length ? `<ul>${items.obtained.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul>` : `<p class="muted">暂无明确道具</p>`}
    </div>
  `);
  rows.push(`
    <div class="summary-block">
      <strong>缺少 / 待确认</strong>
      ${items.missing.length ? `<ul>${items.missing.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul>` : `<p class="good-text">当前关键条件已满足</p>`}
    </div>
  `);

  const knownKeys = new Set([
    "name",
    "location",
    "role",
    "identity",
    "status",
    "realm",
    "battle_power",
    "cultivation",
    "spirit_stones",
    "inventory",
    "items",
    "skills",
    "trial_token",
    "spirit_seal",
    "trial_complete",
  ]);
  const extras = Object.entries(data).filter(([key, value]) => !knownKeys.has(key) && typeof value !== "object");
  if (extras.length) {
    rows.push(`<div class="summary-grid small">${extras.map(([key, value]) => `<span>${escapeHtml(formatKeyLabel(key))}</span><strong>${escapeHtml(formatValue(value))}</strong>`).join("")}</div>`);
  }

  summary.classList.remove("muted");
  summary.innerHTML = rows.join("");
}

function collectPlayerItems(player) {
  const obtained = [];
  const missing = [];
  const addFlag = (key, label) => {
    if (player[key] === true) obtained.push(label);
    if (player[key] === false) missing.push(label);
  };

  addFlag("trial_token", "灵雾谷试炼令");
  addFlag("spirit_seal", "灵印");
  addFlag("trial_complete", "试炼完成");

  const inventory = [...normalizeItemList(player.inventory), ...normalizeItemList(player.items)];
  inventory.forEach((item) => {
    if (!obtained.includes(item)) obtained.push(item);
  });

  Object.entries(player || {}).forEach(([key, value]) => {
    if (!key.endsWith("_obtained") && !key.endsWith("_owned")) return;
    const label = formatKeyLabel(key.replace(/_(obtained|owned)$/, ""));
    if (value === true) obtained.push(label);
    if (value === false) missing.push(label);
  });

  return {
    obtained: [...new Set(obtained)],
    missing: [...new Set(missing)],
  };
}

function collectPlayerStats(player) {
  const rows = [];
  const targets = currentStatTargets();
  const seen = new Set();
  const add = (path, value) => {
    if (value === undefined || value === null || value === "" || typeof value === "object") return;
    const target = targets[path];
    const suffix = target !== undefined && target !== "" ? ` / ${target}` : "";
    rows.push({ label: formatKeyLabel(path), value: `${value}${suffix}` });
    seen.add(path);
  };

  flattenNumericFields(player.skills || {}, "skills").forEach(([path, value]) => add(path, value));
  ["stage_confidence", "fan_count", "reputation"].forEach((key) => add(key, player[key]));
  Object.entries(targets).forEach(([path, target]) => {
    if (!seen.has(path)) {
      rows.push({ label: formatKeyLabel(path), value: `0 / ${target}` });
    }
  });
  return rows;
}

function flattenNumericFields(source, prefix = "") {
  const rows = [];
  Object.entries(source || {}).forEach(([key, value]) => {
    const path = prefix ? `${prefix}.${key}` : key;
    if (value && typeof value === "object" && !Array.isArray(value)) {
      rows.push(...flattenNumericFields(value, path));
    } else if (typeof value === "number" || typeof value === "string") {
      rows.push([path, value]);
    }
  });
  return rows;
}

function currentStatTargets() {
  const targets = {};
  safeParseTasks().forEach((task) => {
    const stats = task.completion?.stats || {};
    Object.entries(stats).forEach(([path, rule]) => {
      if (rule && typeof rule === "object") {
        targets[path] = rule.min ?? rule.eq ?? rule.max ?? "";
      } else {
        targets[path] = rule;
      }
    });
  });
  return targets;
}

function normalizeItemList(value) {
  if (!Array.isArray(value)) return [];
  return value
    .map((item) => {
      if (typeof item === "string") return item;
      if (item && typeof item === "object") {
        const name = item.name || item.label || item.id;
        if (!name) return JSON.stringify(item);
        return item.quantity && Number(item.quantity) !== 1 ? `${name} x${item.quantity}` : name;
      }
      return "";
    })
    .filter(Boolean);
}

function formatKeyLabel(key) {
  const labels = {
    defeated_monsters: "击败妖灵",
    reputation: "声望",
    "skills.dance": "舞蹈熟练度",
    "skills.vocal": "声乐熟练度",
    "skills.stage": "舞台表现",
    stage_confidence: "舞台自信",
    fan_count: "粉丝数",
  };
  return labels[key] || key.replaceAll("_", " ");
}

function formatValue(value) {
  if (value === true) return "是";
  if (value === false) return "否";
  return value;
}

function renderNpcSelector(npcs, selectedId = "") {
  const select = $("#target-npc");
  const locationScoped = state.nearbyNpcs?.length ? state.nearbyNpcs : nearbyNpcsForLocation(state.currentPlayer?.location);
  const available = locationScoped.length ? locationScoped : [];
  const previous = selectedId || select.value;
  const groupOption =
    available.length > 1 ? `<option value="__nearby__">在场群聊 · ${available.length} NPC</option>` : "";
  const options = groupOption + available
    .map((npc) => `<option value="${escapeHtml(npc.id)}">${escapeHtml(npc.name || npc.id)}${npc.role ? ` · ${escapeHtml(npc.role)}` : ""}</option>`)
    .join("");
  select.innerHTML = options || `<option value="">此处暂无 NPC</option>`;
  if (previous && [...select.options].some((option) => option.value === previous)) {
    select.value = previous;
  }
  select.disabled = state.busy || !options;
  const send = $("#send");
  if (send) send.disabled = state.busy || !options;
}

function clearRunState() {
  $("#quest-progress").textContent = "暂无";
  $("#player-state").textContent = "{}";
  $("#player-summary").textContent = "暂无";
  $("#active-entity").textContent = "{}";
  $("#inner-thought").textContent = "暂无";
  $("#memory-result").textContent = "暂无";
  const traceBox = $("#llm-trace");
  if (traceBox) traceBox.textContent = "{}";
  $("#suggested-actions").innerHTML = "";
  state.runtimeNpcs = [];
  state.nearbyNpcs = [];
  state.currentPlayer = {};
  $("#play-status").textContent = "先和 NPC 对话获得线索；移动、找人和观察只处理你明确输入的当前行动。";
  $("#start-world").textContent = "开始游戏";
  renderCurrentLocation("起始地点");
  renderNearbyNpcs([], "起始地点");
}

function addMessage(role, text, speaker) {
  const bubble = document.createElement("div");
  bubble.className = `bubble ${role}`;
  bubble.innerHTML = `<strong>${escapeHtml(speaker || role)}</strong><p></p>`;
  bubble.querySelector("p").textContent = text;
  $("#messages").appendChild(bubble);
  requestAnimationFrame(() => {
    bubble.scrollIntoView({ block: "end", behavior: "auto" });
  });
  if (!state.hydrating && !role.includes("pending")) saveRunSession();
  return bubble;
}

function readMessagesFromDom() {
  return [...document.querySelectorAll("#messages .bubble:not(.pending)")].map((bubble) => ({
    role: [...bubble.classList].filter((item) => item !== "bubble").join(" "),
    speaker: bubble.querySelector("strong")?.textContent || "",
    text: bubble.querySelector("p")?.textContent || "",
  }));
}

function renderStoredMessages(messages) {
  $("#messages").innerHTML = "";
  state.hydrating = true;
  try {
    (messages || []).forEach((item) => addMessage(item.role || "system", item.text || "", item.speaker || ""));
  } finally {
    state.hydrating = false;
  }
}

function conversationLogToMessages(worldState) {
  const log = worldState?.conversation_log;
  if (!Array.isArray(log)) return [];
  return log
    .map((item) => {
      if (!item || typeof item !== "object") return null;
      const role = item.role === "player" ? "player" : item.role === "system" ? "system" : "npc";
      const text = String(item.content || "").trim();
      if (!text) return null;
      return {
        role,
        speaker: item.speaker || role,
        text,
      };
    })
    .filter(Boolean);
}

function renderServerConversation(worldState, { replace = false } = {}) {
  const messages = conversationLogToMessages(worldState);
  if (!messages.length) return false;
  if (replace || !readMessagesFromDom().length) {
    renderStoredMessages(messages);
    return true;
  }
  return false;
}

function saveRunSession() {
  if (!state.currentWorldId || state.hydrating) return;
  const payload = {
    worldId: state.currentWorldId,
    playStatus: $("#play-status").textContent,
    messages: readMessagesFromDom(),
    runtime: {
      player: state.currentPlayer || {},
      npcs: state.runtimeNpcs || [],
      questProgress: $("#quest-progress").textContent,
      playerSummaryHtml: $("#player-summary").innerHTML,
      activeEntity: $("#active-entity").textContent,
      playerState: $("#player-state").textContent,
      innerThought: $("#inner-thought").textContent,
      llmTrace: $("#llm-trace")?.textContent || "{}",
      suggestionsHtml: $("#suggested-actions").innerHTML,
      nearbyNpcs: state.nearbyNpcs || [],
      selectedNpcId: $("#target-npc").value || "",
    },
    savedAt: new Date().toISOString(),
  };
  localStorage.setItem(sessionKey(state.currentWorldId), JSON.stringify(payload));
}

async function restoreRunSession(worldId) {
  const raw = localStorage.getItem(sessionKey(worldId));
  let restored = false;

  if (raw) {
    let saved;
    try {
      saved = JSON.parse(raw);
    } catch {
      saved = null;
    }

    if (saved?.messages?.length) {
      state.hydrating = true;
      try {
        renderStoredMessages(saved.messages);
        $("#play-status").textContent = saved.playStatus || `正在运行：${worldId}`;
        $("#quest-progress").textContent = saved.runtime?.questProgress || "暂无";
        $("#player-summary").innerHTML = saved.runtime?.playerSummaryHtml || "暂无";
        $("#active-entity").textContent = saved.runtime?.activeEntity || "{}";
        $("#player-state").textContent = saved.runtime?.playerState || "{}";
        $("#inner-thought").textContent = saved.runtime?.innerThought || "暂无";
        const traceBox = $("#llm-trace");
        if (traceBox) traceBox.textContent = saved.runtime?.llmTrace || "{}";
        $("#suggested-actions").innerHTML = saved.runtime?.suggestionsHtml || "";
        state.currentPlayer = saved.runtime?.player || {};
        state.runtimeNpcs = saved.runtime?.npcs || [];
        renderCurrentLocation(state.currentPlayer?.location || fields.playerLocation.value || "起始地点");
        state.nearbyNpcs = saved.runtime?.nearbyNpcs || nearbyNpcsForLocation(state.currentPlayer?.location);
        renderNearbyNpcs(state.nearbyNpcs, state.currentPlayer?.location || fields.playerLocation.value || "起始地点");
        if (state.runtimeNpcs.length) renderNpcSelector(state.runtimeNpcs, saved.runtime?.selectedNpcId || "");
        $("#start-world").textContent = "重新开始游戏";
        restored = true;
      } finally {
        state.hydrating = false;
      }
    }
  }

  try {
    const snapshot = await request(`/api/worlds/${encodeURIComponent(worldId)}/session`);
    if (snapshot.started) {
      state.hydrating = true;
      try {
        renderRuntime({
          state: snapshot.state,
          player: snapshot.player,
          active_entity: snapshot.active_entity,
          speaker: snapshot.speaker,
          npcs: snapshot.npcs,
          nearby_npcs: snapshot.nearby_npcs,
          quest_progress: snapshot.quest_progress,
          suggested_actions: snapshot.suggested_actions,
          inner_thought: snapshot.inner_thought,
        });
        const restoredFromServer = renderServerConversation(snapshot.state, { replace: !restored });
        $("#play-status").textContent = `正在运行：${worldId}`;
        $("#start-world").textContent = "重新开始游戏";
        restored = restored || restoredFromServer || true;
      } finally {
        state.hydrating = false;
      }
    }
  } catch {
    // 前端本地记录仍可用；后端 session 可能刚重启。
  }
  return restored;
}
function showWaiting(text, speaker = "NPC") {
  return addMessage("npc pending", text, speaker);
}

function setBusy(isBusy, label = "") {
  state.busy = isBusy;
  const controls = [
    "#send",
    "#move-location",
    "#look-around",
    "#message",
    "#location-input",
    "#look-input",
    "#target-npc",
  ];
  controls.forEach((selector) => {
    const element = $(selector);
    if (element) element.disabled = isBusy;
  });
  if (!isBusy) renderNpcSelector(state.runtimeNpcs, $("#target-npc").value || "");

  const status = $("#play-status");
  if (status) {
    if (isBusy) {
      state.previousPlayStatus = status.textContent;
      status.textContent = label || `${currentNpcName()}回复中...`;
      status.classList.add("is-busy");
    } else {
      status.textContent = state.previousPlayStatus || `正在运行：${state.currentWorldId || "未开始"}`;
      status.classList.remove("is-busy");
    }
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

async function loadQinglanMvpExample() {
  if (!state.currentWorldId) return;
  const template = await request("/api/worlds/sandbox_1");
  template.world_id = fields.worldId.value.trim() || state.currentWorldId;
  fillEditor(template);
  state.dirty = true;
  addMessage("system", "已填充青岚修真界 MVP：5 步闭环可直接验证。", "系统");
}

function appendJsonArrayItem(field, item) {
  const items = parseJsonField(field, []);
  items.push(item);
  field.value = pretty(items);
  state.dirty = true;
}

function addNpcTemplate() {
  appendJsonArrayItem(fields.npcs, {
    id: `npc_${Date.now()}`,
    name: "新NPC",
    role: "待设定身份",
    personality: "他说话方式、性格、限制条件。",
    goals: ["他想推动或阻止什么"],
    location: fields.playerLocation.value || "起始地点",
  });
  renderNpcSelector(parseJsonField(fields.npcs, []));
}

function addTaskTemplate() {
  appendJsonArrayItem(fields.tasks, {
    id: `task_${Date.now()}`,
    title: "新任务",
    description: "玩家要完成什么，完成后世界状态如何变化。",
    status: "pending",
  });
}

function addActionTemplate() {
  appendJsonArrayItem(fields.actions, {
    id: `action_${Date.now()}`,
    label: "新世界动作",
    description: "后台世界规则：由 NPC 对话、状态校验或 Agent 决策触发。",
    effect: {
      set_player: { status: "状态已更新" },
      complete_task: "task_id_here",
      scene: "新的场景描述",
    },
  });
  renderWorldActions();
}

$("#world-list").addEventListener("click", async (event) => {
  const card = event.target.closest(".world-card");
  if (!card) return;
  await selectWorld(card.dataset.id);
});

Object.values(fields).forEach((field) => {
  field.addEventListener("input", () => {
    state.dirty = true;
    if (field === fields.npcs) {
      try {
        renderNpcSelector(parseJsonField(fields.npcs, []));
      } catch {
        // Ignore while the user is midway through editing JSON.
      }
    }
    if (field === fields.actions) {
      renderWorldActions();
    }
    if (field === fields.tasks) {
      renderMvpChecklist(safeParseTasks());
    }
  });
});

$("#target-npc").addEventListener("change", () => {
  const selected = $("#target-npc").selectedOptions[0]?.textContent || "NPC";
  addMessage("system", `当前对话对象：${selected}`, "系统");
  if (state.currentWorldId && $("#target-npc").value !== "__nearby__") {
    request(`/api/worlds/${encodeURIComponent(state.currentWorldId)}/action`, {
      method: "POST",
      body: JSON.stringify({ action: "switch_npc", payload: { npc_id: $("#target-npc").value } }),
    })
      .then(renderRuntime)
      .catch((error) => addMessage("system", error.message, "错误"));
  }
});

$("#toggle-suggestions").addEventListener("click", () => {
  state.suggestionsVisible = !state.suggestionsVisible;
  $("#toggle-suggestions").textContent = state.suggestionsVisible ? "隐藏" : "显示";
  if (!state.suggestionsVisible) {
    $("#suggested-actions").innerHTML = "";
  }
});

$("#create-world").addEventListener("click", () => createWorld().catch((error) => addMessage("system", error.message, "错误")));
$("#generator-complexity").addEventListener("change", applyComplexityPreset);
$("#generate-world").addEventListener("click", () => generateWorld().catch((error) => addMessage("system", error.message, "错误")));
$("#import-world").addEventListener("click", () => importWorld().catch((error) => addMessage("system", error.message, "错误")));
$("#load-example").addEventListener("click", () => loadQinglanMvpExample().catch((error) => addMessage("system", error.message, "错误")));
$("#add-npc-template").addEventListener("click", () => {
  try {
    addNpcTemplate();
  } catch (error) {
    addMessage("system", error.message, "错误");
  }
});
$("#add-task-template").addEventListener("click", () => {
  try {
    addTaskTemplate();
  } catch (error) {
    addMessage("system", error.message, "错误");
  }
});
$("#add-action-template").addEventListener("click", () => {
  try {
    addActionTemplate();
  } catch (error) {
    addMessage("system", error.message, "错误");
  }
});
$("#save-world").addEventListener("click", () => saveWorld().catch((error) => addMessage("system", error.message, "错误")));
$("#clear-chat").addEventListener("click", () => clearChatSession());
$("#delete-world").addEventListener("click", () => deleteCurrentWorld().catch((error) => addMessage("system", error.message, "错误")));
$("#start-world").addEventListener("click", () => startWorld().catch((error) => addMessage("system", error.message, "错误")));
const tickAgentButton = $("#tick-agent");
if (tickAgentButton) {
  tickAgentButton.addEventListener("click", () => tickAgent().catch((error) => addMessage("system", error.message, "错误")));
}
$("#query-memory").addEventListener("click", () => queryMemory().catch((error) => addMessage("system", error.message, "错误")));
$("#submit-experience").addEventListener("click", () => submitExperienceFeedback().catch((error) => addMessage("system", error.message, "错误")));

$("#location-form").addEventListener("submit", (event) => {
  event.preventDefault();
  if (state.busy) return;
  setBusy(true, "正在切换地点...");
  moveToLocation($("#location-input").value)
    .catch((error) => addMessage("system", error.message, "错误"))
    .finally(() => setBusy(false));
});

$("#look-form").addEventListener("submit", (event) => {
  event.preventDefault();
  if (state.busy) return;
  const input = $("#look-input");
  const text = input.value;
  input.value = "";
  setBusy(true);
  lookOrFind(text)
    .catch((error) => addMessage("system", error.message, "错误"))
    .finally(() => setBusy(false));
});

$("#chat-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!state.currentWorldId) return;
  const input = $("#message");
  const message = input.value.trim();
  if (!message || state.busy) return;
  input.value = "";
  setBusy(true);
  try {
    await sendChat(message);
  } catch (error) {
    addMessage("system", error.message, "错误");
  } finally {
    setBusy(false);
    input.focus();
  }
});

async function init() {
  applyComplexityPreset();
  await loadExperienceProfile();
  await loadWorldTemplates();
  await loadWorlds();
}

init().catch((error) => addMessage("system", error.message, "错误"));
