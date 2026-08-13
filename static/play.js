const ui = (selector) => document.querySelector(selector);

const state = {
  worldId: "",
  sessionId: "",
  session: null,
  typing: false,
  fullText: "",
  renderedText: "",
  typeTimer: null,
  autoTimer: null,
  auto: false,
  skip: false,
  busy: false,
  toastTimer: null,
  libraryOpenedFromEnding: false,
  restartArmed: false,
  restartTimer: null,
};

const storageKey = (worldId) => `interactive-story-session:${worldId}`;
const legacyStorageKey = (worldId) => `qingmeng-player-session:${worldId}`;

async function api(url, options = {}) {
  const response = await fetch(url, {
    headers: { "Content-Type": "application/json; charset=utf-8", ...(options.headers || {}) },
    ...options,
  });
  if (!response.ok) {
    let detail = "";
    try {
      const payload = await response.json();
      detail = payload.detail || JSON.stringify(payload);
    } catch (_) {
      detail = await response.text();
    }
    const error = new Error(detail || `${response.status} ${response.statusText}`);
    error.status = response.status;
    throw error;
  }
  return response.status === 204 ? null : response.json();
}

function makeSessionId() {
  const random = globalThis.crypto?.randomUUID?.().replaceAll("-", "") || Math.random().toString(16).slice(2);
  return `reader_${random}`;
}

function setLoading(visible, title = "正在载入作品", detail = "读取剧情与玩家存档") {
  ui("#loading-overlay").hidden = !visible;
  ui("#loading-title").textContent = title;
  ui("#loading-detail").textContent = detail;
}

function showToast(message, mode = "success", duration = 3600) {
  const toast = ui("#toast");
  if (state.toastTimer) clearTimeout(state.toastTimer);
  toast.textContent = message;
  toast.className = `toast ${mode}`;
  toast.hidden = false;
  state.toastTimer = setTimeout(() => { toast.hidden = true; }, duration);
}

function updateSaveStatus(text, mode = "") {
  const target = ui("#save-status");
  target.textContent = text;
  target.className = `save-status ${mode}`;
}

function initMotes() {
  const container = ui("#floating-motes");
  for (let index = 0; index < 20; index += 1) {
    const mote = document.createElement("i");
    mote.className = "mote";
    mote.style.left = `${4 + Math.random() * 92}%`;
    mote.style.setProperty("--duration", `${9 + Math.random() * 13}s`);
    mote.style.setProperty("--delay", `${-Math.random() * 18}s`);
    mote.style.opacity = String(.2 + Math.random() * .6);
    container.append(mote);
  }
}

async function boot() {
  initMotes();
  bindEvents();
  const worldId = new URLSearchParams(location.search).get("world")?.trim();
  if (!worldId) {
    await showWorldPicker();
    return;
  }
  await loadWorld(worldId);
}

async function showWorldPicker() {
  state.libraryOpenedFromEnding = !ui("#ending-overlay").hidden;
  if (state.libraryOpenedFromEnding) ui("#ending-overlay").hidden = true;
  setLoading(true, "正在读取作品库", "读取所有可游玩的 Creator 世界");
  try {
    const worlds = await api("/api/player/worlds");
    const list = ui("#world-list");
    list.innerHTML = "";
    if (!worlds.length) {
      const empty = document.createElement("p");
      empty.textContent = "还没有已发布的剧情。请先在 Creator 中点击“发布并打开玩家端”。";
      list.append(empty);
    }
    for (const world of worlds) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "world-entry";
      const title = document.createElement("strong");
      title.textContent = world.name || world.world_id;
      const description = document.createElement("span");
      description.textContent = world.description || "一段等待开启的互动故事。";
      const meta = document.createElement("small");
      meta.textContent = `${world.node_count} 个剧情节点 · ${world.character_count} 位角色${world.has_visuals ? " · 已绑定美术" : ""}`;
      button.append(title, description, meta);
      button.addEventListener("click", () => loadWorld(world.world_id));
      list.append(button);
    }
    ui("#world-picker-close").hidden = !state.worldId;
    ui("#world-picker").hidden = false;
  } catch (error) {
    showToast(`无法读取作品列表：${error.message}`, "error", 7000);
  } finally {
    setLoading(false);
  }
}

function closeWorldPicker() {
  if (!state.worldId) return;
  ui("#world-picker").hidden = true;
  if (state.libraryOpenedFromEnding && state.session?.ended) showEnding(state.session);
  state.libraryOpenedFromEnding = false;
}

async function loadWorld(worldId) {
  if (state.busy) return;
  state.busy = true;
  state.worldId = worldId;
  state.libraryOpenedFromEnding = false;
  ui("#world-picker").hidden = true;
  ui("#creator-link").href = `/creator?world=${encodeURIComponent(worldId)}`;
  history.replaceState(null, "", `/play?world=${encodeURIComponent(worldId)}`);
  setLoading(true, "正在进入故事", "检查本地存档并载入当前章节");
  try {
    state.sessionId = localStorage.getItem(storageKey(worldId))
      || localStorage.getItem(legacyStorageKey(worldId))
      || makeSessionId();
    let session;
    let restored = false;
    try {
      session = await api(`/api/player/worlds/${encodeURIComponent(worldId)}/sessions/${encodeURIComponent(state.sessionId)}`);
      restored = true;
    } catch (error) {
      if (error.status !== 404) throw error;
      session = await api(`/api/player/worlds/${encodeURIComponent(worldId)}/start`, {
        method: "POST",
        body: JSON.stringify({ session_id: state.sessionId, restart: false }),
      });
    }
    localStorage.setItem(storageKey(worldId), state.sessionId);
    localStorage.removeItem(legacyStorageKey(worldId));
    renderSession(session);
    if (restored && !session.recovery_notice) showToast("已恢复上次的自动存档");
  } catch (error) {
    updateSaveStatus("载入失败", "error");
    showToast(`无法启动剧情：${error.message}`, "error", 9000);
    ui("#world-picker").hidden = false;
  } finally {
    state.busy = false;
    setLoading(false);
  }
}

function renderSession(session) {
  state.session = session;
  ui("#world-title").textContent = session.world.name || "未命名故事";
  document.title = `${session.world.name || "互动剧情"} · 玩家端`;
  ui("#scene-location").textContent = session.node.location || session.node.title || "未知之境";
  ui("#world-kicker").textContent = session.node.type === "ending" ? "终章" : "互动叙事";
  ui("#objective-card").hidden = !session.node.objective;
  ui("#objective-text").textContent = session.node.objective || "";
  ui("#speaker-name").textContent = session.speaker?.name || "旁白";
  updateBackground(session.node);
  updatePortrait(session.speaker);
  renderHistory(session.history || []);
  renderPostStoryHistory(session.post_story_history || []);
  populatePostStoryCharacters(session.post_story_characters || []);
  ui("#ending-overlay").hidden = true;
  ui("#choice-layer").hidden = true;
  ui("#continue-hint").hidden = true;
  updateSaveStatus(`已存档 · ${formatTime(session.saved_at)}`);
  if (session.recovery_notice) showToast(session.recovery_notice, "warning", 7500);
  typeDialogue(session.node.content || session.node.title || "……", () => finishNodePresentation(session));
}

function updateBackground(node) {
  const target = ui("#scene-background");
  const image = safeImageUrl(node.background);
  if (image) {
    target.style.backgroundImage = `linear-gradient(135deg, rgba(8,16,21,.22), rgba(7,12,18,.44)), url(${JSON.stringify(image)})`;
    return;
  }
  const hue = hashText(`${node.location}|${node.title}`) % 70 + 145;
  target.style.backgroundImage = [
    `radial-gradient(circle at 68% 28%, hsla(${hue}, 35%, 52%, .28), transparent 27%)`,
    `radial-gradient(circle at 24% 70%, hsla(${hue + 32}, 29%, 32%, .24), transparent 38%)`,
    `linear-gradient(135deg, hsl(${hue}, 25%, 16%), hsl(${hue + 24}, 24%, 9%) 58%, hsl(${hue - 20}, 21%, 15%))`,
  ].join(",");
}

function updatePortrait(speaker) {
  const center = ui("#portrait-center");
  ui("#portrait-left").className = "portrait-slot left";
  ui("#portrait-right").className = "portrait-slot right";
  center.innerHTML = "";
  center.style.backgroundImage = "";
  center.className = "portrait-slot center";
  if (!speaker) return;
  const image = safeImageUrl(speaker.portrait);
  if (image) {
    center.style.backgroundImage = `url(${JSON.stringify(image)})`;
  } else {
    const fallback = document.createElement("div");
    fallback.className = "portrait-fallback";
    const initial = document.createElement("span");
    initial.className = "portrait-initial";
    initial.textContent = Array.from(speaker.name || "人")[0] || "人";
    fallback.append(initial);
    center.append(fallback);
  }
  requestAnimationFrame(() => center.classList.add("visible", "active"));
}

function safeImageUrl(value) {
  const text = String(value || "").trim();
  if (/^(https?:\/\/|\/|\.\/|data:image\/)/i.test(text)) return text;
  return "";
}

function hashText(value) {
  let hash = 0;
  for (const character of String(value || "")) hash = ((hash << 5) - hash + character.codePointAt(0)) | 0;
  return Math.abs(hash);
}

function typeDialogue(text, done) {
  clearTimeout(state.typeTimer);
  clearTimeout(state.autoTimer);
  const panel = ui("#dialogue-panel");
  const target = ui("#dialogue-text");
  const characters = Array.from(String(text || ""));
  state.fullText = characters.join("");
  state.renderedText = "";
  state.typing = true;
  panel.classList.remove("typed");
  target.textContent = "";
  let index = 0;
  const reducedMotion = matchMedia("(prefers-reduced-motion: reduce)").matches;
  const tick = () => {
    const step = state.skip || reducedMotion ? Math.max(4, Math.ceil(characters.length / 12)) : 1;
    index = Math.min(characters.length, index + step);
    state.renderedText = characters.slice(0, index).join("");
    target.textContent = state.renderedText;
    if (index >= characters.length) {
      state.typing = false;
      panel.classList.add("typed");
      done?.();
      return;
    }
    const character = characters[index - 1];
    const punctuationPause = /[。！？…]/.test(character) ? 105 : /[，、；：]/.test(character) ? 55 : 0;
    state.typeTimer = setTimeout(tick, (state.skip ? 4 : 24) + punctuationPause);
  };
  tick();
}

function revealDialogue() {
  if (!state.typing) return false;
  clearTimeout(state.typeTimer);
  state.typing = false;
  state.renderedText = state.fullText;
  ui("#dialogue-text").textContent = state.fullText;
  ui("#dialogue-panel").classList.add("typed");
  finishNodePresentation(state.session);
  return true;
}

function finishNodePresentation(session) {
  if (!session || session !== state.session) return;
  if (session.choices?.length) {
    renderChoices(session.choices);
    return;
  }
  if (session.ended) {
    state.autoTimer = setTimeout(() => showEnding(session), state.skip ? 250 : 750);
    return;
  }
  if (session.can_advance) {
    ui("#continue-hint").hidden = false;
    if (state.auto) state.autoTimer = setTimeout(advanceStory, state.skip ? 500 : 1900);
  }
}

function renderChoices(choices) {
  const layer = ui("#choice-layer");
  const list = ui("#choice-list");
  list.innerHTML = "";
  for (const choice of choices) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "choice-button";
    const text = document.createElement("span");
    text.textContent = choice.text;
    button.append(text);
    if (choice.consequence_summary) {
      const summary = document.createElement("small");
      summary.textContent = choice.consequence_summary;
      button.append(summary);
    }
    button.addEventListener("click", () => chooseStory(choice.id));
    list.append(button);
  }
  layer.hidden = false;
}

async function advanceStory() {
  if (state.busy || state.typing || !state.session?.can_advance) return;
  clearTimeout(state.autoTimer);
  state.busy = true;
  ui("#continue-hint").hidden = true;
  updateSaveStatus("正在存档…", "saving");
  try {
    const session = await api(`/api/player/worlds/${encodeURIComponent(state.worldId)}/advance`, {
      method: "POST",
      body: JSON.stringify({ session_id: state.sessionId }),
    });
    renderSession(session);
  } catch (error) {
    updateSaveStatus("存档失败", "error");
    showToast(error.message, "error");
  } finally {
    state.busy = false;
  }
}

async function chooseStory(choiceId) {
  if (state.busy || state.typing) return;
  state.busy = true;
  ui("#choice-list").querySelectorAll("button").forEach((button) => { button.disabled = true; });
  updateSaveStatus("正在记录选择…", "saving");
  try {
    const session = await api(`/api/player/worlds/${encodeURIComponent(state.worldId)}/choose`, {
      method: "POST",
      body: JSON.stringify({ session_id: state.sessionId, choice_id: choiceId }),
    });
    renderSession(session);
  } catch (error) {
    updateSaveStatus("选择未保存", "error");
    showToast(error.message, "error");
    ui("#choice-list").querySelectorAll("button").forEach((button) => { button.disabled = false; });
  } finally {
    state.busy = false;
  }
}

async function restartStory(event) {
  if (!state.worldId || state.busy) return;
  const button = event?.currentTarget;
  if (!state.restartArmed) {
    state.restartArmed = true;
    if (button) {
      button.dataset.restartLabel = button.textContent;
      button.textContent = "再次点击确认";
    }
    showToast("再次点击同一按钮，确认覆盖当前存档", "warning", 4500);
    if (state.restartTimer) clearTimeout(state.restartTimer);
    state.restartTimer = setTimeout(() => {
      state.restartArmed = false;
      if (button?.isConnected) button.textContent = button.dataset.restartLabel || "重来";
    }, 4500);
    return;
  }
  state.restartArmed = false;
  if (state.restartTimer) clearTimeout(state.restartTimer);
  state.restartTimer = null;
  document.querySelectorAll("#restart-button, #ending-restart-button").forEach((item) => {
    item.textContent = item.dataset.restartLabel || (item.id === "ending-restart-button" ? "从头再来" : "重来");
  });
  closeDrawers();
  ui("#ending-overlay").hidden = true;
  state.busy = true;
  setLoading(true, "正在重新开始", "清理当前进度并回到故事开场");
  try {
    const session = await api(`/api/player/worlds/${encodeURIComponent(state.worldId)}/start`, {
      method: "POST",
      body: JSON.stringify({ session_id: state.sessionId, restart: true }),
    });
    renderSession(session);
    showToast("已从故事开场重新开始");
  } catch (error) {
    showToast(error.message, "error");
  } finally {
    state.busy = false;
    setLoading(false);
  }
}

function showEnding(session) {
  const overlay = ui("#ending-overlay");
  ui("#ending-title").textContent = session.node.title || "故事终章";
  ui("#ending-copy").textContent = session.node.content || "这一段故事已经抵达终点。";
  const postStory = Boolean(session.post_story_available);
  ui("#post-story-button").hidden = !postStory;
  ui("#post-story-note").hidden = !postStory;
  overlay.hidden = false;
}

function renderHistory(entries) {
  const list = ui("#history-list");
  list.innerHTML = "";
  for (const entry of entries) {
    const item = document.createElement("article");
    item.className = `history-entry ${entry.kind || "narration"}`;
    const speaker = document.createElement("strong");
    speaker.textContent = entry.kind === "choice" ? `你的选择 · ${entry.speaker_name || "玩家"}` : entry.speaker_name || "旁白";
    const content = document.createElement("p");
    content.textContent = entry.content;
    item.append(speaker, content);
    list.append(item);
  }
  list.scrollTop = list.scrollHeight;
}

function populatePostStoryCharacters(characters) {
  const select = ui("#post-story-npc");
  const current = select.value;
  select.innerHTML = "";
  for (const character of characters) {
    const option = document.createElement("option");
    option.value = character.id;
    option.textContent = `${character.name}${character.role ? ` · ${character.role}` : ""}`;
    select.append(option);
  }
  if (characters.some((item) => item.id === current)) select.value = current;
}

function renderPostStoryHistory(entries) {
  const log = ui("#post-story-log");
  log.innerHTML = "";
  const playerName = state.session?.world?.player_name || "玩家";
  for (const entry of entries) {
    const item = document.createElement("article");
    const isSystem = entry.kind === "system";
    const isPlayer = !isSystem && !entry.speaker_id && entry.speaker_name === playerName;
    item.className = `post-message ${isSystem ? "system" : isPlayer ? "player" : "npc"}`;
    const speaker = document.createElement("strong");
    speaker.textContent = entry.speaker_name || (isPlayer ? playerName : "NPC");
    const content = document.createElement("p");
    content.textContent = entry.content;
    item.append(speaker, content);
    log.append(item);
  }
  log.scrollTop = log.scrollHeight;
}

async function sendPostStoryMessage(event) {
  event.preventDefault();
  if (state.busy || !state.session?.post_story_available) return;
  const input = ui("#post-story-input");
  const message = input.value.trim();
  if (!message) return;
  const button = ui("#post-story-form button");
  state.busy = true;
  button.disabled = true;
  input.disabled = true;
  button.textContent = "回应中…";
  try {
    const response = await api(`/api/player/worlds/${encodeURIComponent(state.worldId)}/post-story/chat`, {
      method: "POST",
      body: JSON.stringify({
        session_id: state.sessionId,
        message,
        target_npc_id: ui("#post-story-npc").value,
      }),
    });
    input.value = "";
    state.session.post_story_history = response.history;
    state.session.player = response.player;
    state.session.flags = response.flags;
    state.session.saved_at = response.saved_at;
    renderPostStoryHistory(response.history);
    updateSaveStatus(`已存档 · ${formatTime(response.saved_at)}`);
    for (const triggered of response.triggered_events || []) {
      showToast(`后日谈事件「${triggered.title}」已触发${triggered.description ? `：${triggered.description}` : ""}`, "success", 6500);
    }
  } catch (error) {
    showToast(`后日谈回应失败：${error.message}`, "error", 7000);
  } finally {
    state.busy = false;
    button.disabled = false;
    input.disabled = false;
    button.textContent = "发送";
    input.focus();
  }
}

function openDrawer(drawer) {
  closeDrawers(false);
  drawer.classList.add("open");
  drawer.setAttribute("aria-hidden", "false");
  ui("#drawer-scrim").hidden = false;
}

function closeDrawers(hideScrim = true) {
  for (const drawer of [ui("#history-drawer"), ui("#post-story-drawer")]) {
    if (drawer.contains(document.activeElement)) document.activeElement.blur();
    drawer.classList.remove("open");
    drawer.setAttribute("aria-hidden", "true");
  }
  const game = ui("#game");
  game.scrollLeft = 0;
  requestAnimationFrame(() => { game.scrollLeft = 0; });
  if (hideScrim) ui("#drawer-scrim").hidden = true;
}

function toggleAuto() {
  state.auto = !state.auto;
  ui("#auto-button").setAttribute("aria-pressed", String(state.auto));
  showToast(state.auto ? "自动播放已开启" : "自动播放已关闭");
  if (state.auto && !state.typing && state.session?.can_advance) {
    clearTimeout(state.autoTimer);
    state.autoTimer = setTimeout(advanceStory, 900);
  } else if (!state.auto) {
    clearTimeout(state.autoTimer);
  }
}

function toggleSkip() {
  state.skip = !state.skip;
  ui("#skip-button").setAttribute("aria-pressed", String(state.skip));
  showToast(state.skip ? "快速显示已开启" : "快速显示已关闭");
  if (state.skip && state.typing) revealDialogue();
}

function formatTime(value) {
  if (!value) return "刚刚";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "刚刚";
  return date.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" });
}

function bindEvents() {
  ui("#dialogue-panel").addEventListener("click", () => {
    if (revealDialogue()) return;
    if (state.session?.can_advance) advanceStory();
  });
  ui("#dialogue-panel").addEventListener("keydown", (event) => {
    if (["Enter", " "].includes(event.key)) {
      event.preventDefault();
      ui("#dialogue-panel").click();
    }
  });
  document.addEventListener("keydown", (event) => {
    const tag = event.target?.tagName?.toLowerCase();
    if (["input", "textarea", "select", "button", "a"].includes(tag)) return;
    if (event.key === " " && ui("#ending-overlay").hidden) {
      event.preventDefault();
      ui("#dialogue-panel").click();
    }
    if (event.key.toLowerCase() === "h") openDrawer(ui("#history-drawer"));
    if (event.key === "Escape") {
      closeDrawers();
      closeWorldPicker();
    }
  });
  ui("#auto-button").addEventListener("click", toggleAuto);
  ui("#skip-button").addEventListener("click", toggleSkip);
  ui("#history-button").addEventListener("click", () => openDrawer(ui("#history-drawer")));
  ui("#library-button").addEventListener("click", showWorldPicker);
  ui("#world-picker-close").addEventListener("click", closeWorldPicker);
  ui("#history-close").addEventListener("click", closeDrawers);
  ui("#restart-button").addEventListener("click", restartStory);
  ui("#ending-restart-button").addEventListener("click", restartStory);
  ui("#ending-history-button").addEventListener("click", () => openDrawer(ui("#history-drawer")));
  ui("#ending-library-button").addEventListener("click", showWorldPicker);
  ui("#post-story-button").addEventListener("click", () => {
    ui("#ending-overlay").hidden = true;
    openDrawer(ui("#post-story-drawer"));
    setTimeout(() => ui("#post-story-input").focus(), 260);
  });
  ui("#post-story-close").addEventListener("click", () => {
    closeDrawers();
    showEnding(state.session);
  });
  ui("#post-story-form").addEventListener("submit", sendPostStoryMessage);
  ui("#drawer-scrim").addEventListener("click", closeDrawers);
}

boot().catch((error) => {
  setLoading(false);
  showToast(`播放器初始化失败：${error.message}`, "error", 10000);
  console.error(error);
});
