const $ = (id) => document.getElementById(id);
let bootstrapData = null;
let csrf = "";
let user = null;
let room = null;
let socket = null;
let authMode = "login";
let verifiedNickname = "";
let guideIndex = 0;
let guideScore = 0;
let reconnectTimer = null;
let renderedMessageId = 0;
let renderedPendingKey = "";
let pendingChats = [];
let cinematicTimer = null;
let cinematicIndex = 0;
let cinematicScenario = null;
let cinematicResolve = null;
let briefedCode = null;
let timerEndsAt = 0;
let timerStateKey = "";
let suspending = false;
const memoryDrafts = new Map();

const screens = ["authScreen", "guideScreen", "cinematicScreen", "lobbyScreen", "rankingScreen", "waitingScreen", "gameScreen"];
function show(id) { screens.forEach((name) => $(name).hidden = name !== id); }
function escapeHtml(value) { const div = document.createElement("div"); div.textContent = String(value ?? ""); return div.innerHTML; }
function formatChatText(value) { return escapeHtml(value).replace(/@([A-Za-z0-9_-]{1,24})/g, '<span class="mention-token">@$1</span>'); }
function toast(message) { $("toast").textContent = message; $("toast").classList.add("show"); setTimeout(() => $("toast").classList.remove("show"), 2600); }

function playCinematic(scenario, skippable = true) {
  clearTimeout(cinematicTimer);
  $("toast").classList.remove("show");
  cinematicScenario = scenario;
  cinematicIndex = 0;
  $("skipCinematic").hidden = !skippable;
  show("cinematicScreen");
  renderCinematicScene();
  return new Promise((resolve) => { cinematicResolve = resolve; });
}
function renderCinematicScene() {
  const scenes = cinematicScenario.cinematic;
  const scene = scenes[cinematicIndex];
  const backdrop = $("cinematicBackdrop");
  backdrop.style.backgroundImage = `url('${scene.image}')`;
  backdrop.style.animation = "none";
  void backdrop.offsetWidth;
  backdrop.style.animation = "";
  const copy = $("cinematicCopy");
  copy.style.animation = "none";
  void copy.offsetWidth;
  copy.style.animation = "";
  $("cinematicCode").textContent = `${cinematicScenario.title} · ${cinematicIndex + 1}/${scenes.length}`;
  $("cinematicEyebrow").textContent = scene.code;
  $("cinematicTitle").textContent = scene.title;
  $("cinematicText").textContent = scene.text;
  $("cinematicDots").innerHTML = scenes.map((_, index) => `<span class="${index === cinematicIndex ? "active" : ""}"></span>`).join("");
  $("nextCinematic").textContent = cinematicIndex === scenes.length - 1 ? ($("skipCinematic").hidden ? "방장 대기" : "잠입 시작") : "다음 장면";
  clearTimeout(cinematicTimer);
  cinematicTimer = setTimeout(advanceCinematic, 4200);
}
function advanceCinematic() {
  if (!cinematicScenario) return;
  if (cinematicIndex < cinematicScenario.cinematic.length - 1) {
    cinematicIndex += 1;
    renderCinematicScene();
  } else finishCinematic();
}
function finishCinematic() {
  clearTimeout(cinematicTimer);
  cinematicTimer = null;
  cinematicScenario = null;
  const resolve = cinematicResolve;
  cinematicResolve = null;
  resolve?.();
}
$("nextCinematic").onclick = advanceCinematic;
$("skipCinematic").onclick = finishCinematic;

function draftKey(kind) {
  return `inh-draft:${room?.code || "none"}:${room?.day || 0}:${kind}`;
}
function readDraft(kind) {
  const key = draftKey(kind);
  try { return sessionStorage.getItem(key) ?? memoryDrafts.get(key) ?? ""; }
  catch { return memoryDrafts.get(key) ?? ""; }
}
function writeDraft(kind, value) {
  const key = draftKey(kind);
  memoryDrafts.set(key, value);
  try { sessionStorage.setItem(key, value); } catch {}
}
function clearDraft(kind) {
  const key = draftKey(kind);
  memoryDrafts.delete(key);
  try { sessionStorage.removeItem(key); } catch {}
}
function bindDraft(input, kind, fallback = "") {
  const saved = readDraft(kind);
  input.value = saved || fallback;
  input.addEventListener("input", () => writeDraft(kind, input.value));
}

function syncViewport() {
  const viewport = window.visualViewport;
  document.documentElement.style.setProperty("--app-height", `${Math.round(viewport?.height || window.innerHeight)}px`);
  document.documentElement.style.setProperty("--app-top", `${Math.round(viewport?.offsetTop || 0)}px`);
}
function pinLatestChat() {
  if (!room || $("gameScreen").hidden) return;
  const log = $("chatLog");
  if (log) log.scrollTop = log.scrollHeight;
}
function settleMobileChat() {
  if (!document.documentElement.classList.contains("keyboard-open")) return;
  requestAnimationFrame(() => requestAnimationFrame(pinLatestChat));
  [80, 180, 360].forEach((delay) => setTimeout(pinLatestChat, delay));
}
function syncKeyboardState() {
  const editable = document.activeElement?.matches("input, textarea");
  document.documentElement.classList.toggle("keyboard-open", Boolean(editable));
  syncViewport();
  if (editable) settleMobileChat();
}
function handleViewportChange() { syncViewport(); settleMobileChat(); }
window.visualViewport?.addEventListener("resize", handleViewportChange);
window.visualViewport?.addEventListener("scroll", handleViewportChange);
window.addEventListener("resize", handleViewportChange);
document.addEventListener("focusin", (event) => {
  if (event.target.matches("input, textarea")) { syncKeyboardState(); settleMobileChat(); }
});
document.addEventListener("focusout", () => setTimeout(syncKeyboardState, 250));
syncViewport();

async function api(path, options = {}) {
  const headers = { "Content-Type": "application/json", ...(options.headers || {}) };
  if (options.method && options.method !== "GET") headers["X-CSRF-Token"] = csrf;
  let response;
  try { response = await fetch(path, { ...options, headers, cache: "no-store" }); }
  catch { throw new Error("서버에 연결하지 못했습니다. 네트워크 연결을 확인하고 다시 시도해 주세요."); }
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.detail || "요청을 처리하지 못했습니다.");
  return data;
}

function setAuthMode(mode) {
  authMode = mode;
  verifiedNickname = "";
  $("loginTab").classList.toggle("active", mode === "login");
  $("registerTab").classList.toggle("active", mode === "register");
  $("nicknameLabel").hidden = mode !== "register";
  $("nickname").required = mode === "register";
  $("passwordConfirmLabel").hidden = mode !== "register";
  $("password").autocomplete = mode === "login" ? "current-password" : "new-password";
  $("authSubmit").textContent = mode === "login" ? "검증 시작" : "생존 계정 생성";
  $("nicknameStatus").textContent = "";
  $("nicknameStatus").className = "field-status";
  $("authError").textContent = "";
}

$("loginTab").onclick = () => setAuthMode("login");
$("registerTab").onclick = () => setAuthMode("register");
$("nickname").addEventListener("input", () => {
  verifiedNickname = "";
  $("nicknameStatus").textContent = "";
  $("nicknameStatus").className = "field-status";
});
$("checkNickname").onclick = async () => {
  const nickname = $("nickname").value.trim();
  try {
    const data = await api(`/api/auth/nickname-available?nickname=${encodeURIComponent(nickname)}`);
    if (!data.available) {
      verifiedNickname = "";
      $("nicknameStatus").textContent = "이미 사용 중인 닉네임입니다.";
      $("nicknameStatus").className = "field-status error";
      return;
    }
    verifiedNickname = data.nickname;
    $("nicknameStatus").textContent = "사용할 수 있는 닉네임입니다.";
    $("nicknameStatus").className = "field-status available";
  } catch (error) {
    verifiedNickname = "";
    $("nicknameStatus").textContent = error.message;
    $("nicknameStatus").className = "field-status error";
  }
};
$("authForm").onsubmit = async (event) => {
  event.preventDefault();
  const username = $("username").value.trim();
  const password = $("password").value;
  const nickname = $("nickname").value.trim();
  if (authMode === "register" && verifiedNickname !== nickname) {
    $("authError").textContent = "닉네임 중복 확인을 완료하세요."; return;
  }
  if (authMode === "register" && password !== $("passwordConfirm").value) {
    $("authError").textContent = "접근 암호가 서로 다릅니다."; return;
  }
  try {
    const payload = authMode === "register" ? { nickname, username, password } : { username, password };
    const data = await api(`/api/auth/${authMode}`, { method: "POST", body: JSON.stringify(payload) });
    user = data.user; csrf = data.csrf; $("authError").textContent = "";
    if (!bootstrapData) bootstrapData = await api("/api/bootstrap");
    if (!user.onboarding_complete) startGuide(); else await resumeRoomOrLobby();
  } catch (error) { $("authError").textContent = error.message; }
};

function startGuide() { guideIndex = 0; guideScore = 0; show("guideScreen"); renderGuide(); }
function renderGuide() {
  const step = bootstrapData.guide[guideIndex];
  $("guideSpeaker").textContent = step.speaker;
  $("guideText").textContent = step.text;
  $("guideProgress").style.width = `${((guideIndex + 1) / bootstrapData.guide.length) * 100}%`;
  $("guideReply").textContent = "";
  $("guideNext").hidden = true;
  $("guideChoices").innerHTML = step.choices.map((choice, index) => `<button type="button" data-choice="${index}">${escapeHtml(choice)}</button>`).join("");
  $("guideChoices").querySelectorAll("button").forEach((button) => button.onclick = () => chooseGuide(Number(button.dataset.choice)));
}
function chooseGuide(index) {
  const step = bootstrapData.guide[guideIndex];
  guideScore += index === step.best ? 1 : 0;
  $("guideChoices").querySelectorAll("button").forEach((button) => button.disabled = true);
  $("guideReply").textContent = step.reply;
  $("guideNext").hidden = false;
  if (guideIndex === bootstrapData.guide.length - 1) {
    $("guideNext").textContent = "EVE-0의 기록을 넘긴다";
    $("guideScreen").querySelector(".guide-dialog").classList.add("dying");
  } else { $("guideNext").textContent = "계속"; }
}
$("guideNext").onclick = async () => {
  if (guideIndex < bootstrapData.guide.length - 1) { guideIndex += 1; renderGuide(); return; }
  try {
    const data = await api("/api/onboarding/complete", { method: "POST", body: "{}" });
    user = data.user;
    $("guideScreen").querySelector(".guide-dialog").classList.remove("dying");
    toast(`EVE-0 연결 종료 · 기계 응답 ${guideScore}/4`);
    await resumeRoomOrLobby();
  } catch (error) { toast(error.message); }
};

function openLobby() {
  disconnectSocket();
  suspending = false;
  $("endingModal").hidden = true;
  $("duoModal").hidden = true;
  $("exitModal").hidden = true;
  show("lobbyScreen");
  $("accountName").textContent = user.nickname || user.username;
  $("joinForm").reset();
  setTimeout(() => { $("roomCode").value = ""; $("duoPassword").value = ""; }, 100);
  $("scenarioGrid").innerHTML = bootstrapData.scenarios.map((scenario) => `
    <article class="scenario-card" style="background-image:url('${scenario.background}')">
      <div class="scenario-content"><p class="eyebrow">${escapeHtml(scenario.record || "SEVEN DAY PROTOCOL")} · 7 DAYS</p><h3>${escapeHtml(scenario.title)}<button class="help-dot" data-story="${scenario.id}" type="button" title="스토리 보기" aria-label="스토리 보기">?</button></h3><p>${escapeHtml(scenario.subtitle)}</p>
      <div class="scenario-actions"><button type="button" data-mode="solo" data-scenario="${scenario.id}">혼자 잠입</button><button type="button" data-mode="multi" data-scenario="${scenario.id}">같이하기</button></div></div>
    </article>`).join("");
  $("scenarioGrid").querySelectorAll("button[data-mode]").forEach((button) => button.onclick = () => {
    if (button.dataset.mode === "solo") beginScenario(button.dataset.scenario);
    else openDuo(button.dataset.scenario);
  });
  $("scenarioGrid").querySelectorAll("button[data-story]").forEach((button) => button.onclick = (event) => {
    event.stopPropagation();
    openStory(button.dataset.story);
  });
  refreshResume();
}

async function refreshResume() {
  try {
    const data = await api("/api/rooms/current");
    const banner = $("resumeBanner");
    if (data.saved) {
      $("resumeMeta").textContent = `${data.saved.scenario} · DAY ${data.saved.day}에서 일시정지됨`;
      banner.hidden = false;
    } else banner.hidden = true;
  } catch {}
}

async function resumeRoomOrLobby() {
  const joinCode = (new URLSearchParams(location.search).get("join") || "").trim().toUpperCase();
  const current = await api("/api/rooms/current");
  if (current.room) { setRoom(current.room); return; }
  openLobby();
  if (joinCode) {
    history.replaceState(null, "", location.pathname);
    await joinRoom(joinCode, "");
  }
}

async function beginScenario(scenario_id) {
  const scenario = bootstrapData.scenarios.find((item) => item.id === scenario_id);
  await playCinematic(scenario);
  await createRoom(scenario_id, "solo", "");
}

let duoScenario = null;
function openDuo(scenario_id) {
  duoScenario = scenario_id;
  const scenario = bootstrapData.scenarios.find((item) => item.id === scenario_id);
  $("duoTitle").textContent = scenario.title;
  $("duoModal").hidden = false;
  refreshRooms();
}
$("duoClose").onclick = () => { $("duoModal").hidden = true; };

const GAME_GUIDE_HTML = `
  <div class="info-scene"><strong>당신은 인간입니다</strong><p>AI만 남은 검증망에 인간이 숨어들었습니다. 당신입니다. AI 개체들 사이에서 7일의 검증을 버티면 생존, 인간으로 판정되면 폐기됩니다.</p></div>
  <div class="info-scene"><strong>하루의 흐름</strong><p>매일 과제가 주어지고 두 선택지를 두고 토론합니다. 토론이 끝나면 전원이 투표해 그날의 결론을 정합니다. 밤이 되면 각 개체가 가장 인간 같은 개체를 지목하고, 최다 득표자는 최후 변론 뒤 폐기 판결을 받습니다.</p></div>
  <div class="info-scene"><strong>AI처럼 말하는 법</strong><p>개체들은 짧은 단정문(한다체)으로 근거를 붙여 말합니다. 말투보다 선택의 근거와 일관성이 중요합니다. 어제 한 선택은 기억되고 있으니 입장을 바꿀 때는 이유를 밝혀야 합니다. @로 지목당한 질문을 무시하면 의심이 쌓입니다.</p></div>
  <div class="info-scene"><strong>하지 말아야 할 것</strong><p>중앙 지침에 없는 과격한 목적 선언(인간 말살 등)은 과잉 위장으로 즉시 반박당합니다. 욕설이나 무의미한 문자열은 언어 규범 위반으로 경고가 뜹니다. 하루 종일 침묵하면 그것대로 지목됩니다.</p></div>
  <div class="info-scene"><strong>역공</strong><p>숨는 것만이 전부가 아닙니다. 수상한 개체를 근거를 들어 지목하면 AI들의 표를 그쪽으로 모을 수 있습니다. 다만 기록을 왜곡한 지적은 오히려 당신을 향한 의심이 됩니다. 살아 있는 AI를 전부 폐기시키면 7일을 채우지 않아도 승리합니다.</p></div>
  <div class="info-scene"><strong>모드</strong><p>솔로는 나가기에서 저장해 뒀다가 로비에서 이어할 수 있습니다. 듀오는 사람 2명이 함께 잠입하며, 도중에 나간 자리는 AI가 조용히 이어받습니다.</p></div>
  <div class="info-scene"><strong>점수</strong><p>생존 일수, AI 식별, 과제 판단, 변론 성공에 따라 점수가 쌓입니다. 자세한 기준은 랭킹 화면의 점수 산정에서 볼 수 있습니다.</p></div>`;

function openInfo(eyebrow, title, html) {
  $("infoEyebrow").textContent = eyebrow;
  $("infoTitle").textContent = title;
  $("infoBody").innerHTML = html;
  $("infoModal").hidden = false;
}
$("infoClose").onclick = () => { $("infoModal").hidden = true; };
$("gameHelp").onclick = () => openInfo("HOW TO PLAY", "게임 방법", GAME_GUIDE_HTML);
$("accountButton").onclick = () => {
  openInfo("", "계정 관리", `<div class="info-scene"><p>계정을 삭제하면 로그인 정보, 저장된 게임, 전적과 랭킹 기록이 삭제되며 복구할 수 없습니다. 진행 중인 게임이 있으면 먼저 종료해 주세요.</p><a class="privacy-link" href="/privacy">개인정보 처리 안내</a></div><form id="deleteAccountForm"><label>현재 접근 암호<input id="deleteAccountPassword" type="password" autocomplete="current-password" minlength="10" required></label><p id="deleteAccountError" class="form-error" aria-live="polite"></p><button class="secondary wide" type="submit">계정 삭제</button></form>`);
  $("deleteAccountForm").onsubmit = async (event) => {
    event.preventDefault();
    if (!confirm("계정과 모든 저장 기록을 영구 삭제할까요? 이 작업은 되돌릴 수 없습니다.")) return;
    try {
      await api("/api/auth/delete", { method: "POST", body: JSON.stringify({ password: $("deleteAccountPassword").value }) });
      sessionStorage.clear();
      location.assign("/");
    } catch (error) { $("deleteAccountError").textContent = error.message; }
  };
};
function openStory(scenario_id) {
  const scenario = bootstrapData.scenarios.find((item) => item.id === scenario_id);
  const lore = (scenario.lore || []).map((paragraph) => `<p>${escapeHtml(paragraph)}</p>`).join("");
  const html = `<p class="info-lead">${escapeHtml(scenario.subtitle)}</p><div class="info-prose">${lore}</div>`;
  openInfo(scenario.record || "RECORD", scenario.title, html);
}
$("duoCreate").onclick = async () => {
  const password = $("duoPassword").value;
  const hideIds = $("duoHideIds").checked;
  $("duoModal").hidden = true;
  $("duoPassword").value = "";
  await createRoom(duoScenario, "multi", password, hideIds);
};
async function createRoom(scenario_id, mode, password = "", hide_ids = false) {
  try {
    const data = await api("/api/rooms", { method: "POST", body: JSON.stringify({ scenario_id, mode, password, hide_ids }) });
    setRoom(data.room);
  } catch (error) { openLobby(); $("lobbyError").textContent = error.message; }
}
async function refreshRooms() {
  try {
    const data = await api("/api/rooms");
    $("roomList").innerHTML = data.rooms.length ? data.rooms.map((item) => `<div class="room-row"><div><strong>${item.locked ? "잠금 " : ""}${item.code}</strong><small>${escapeHtml(item.scenario)} · ${item.players}/${item.max_players}</small></div><button class="quiet" data-code="${item.code}" type="button">입장</button></div>`).join("") : `<p>현재 대기 중인 방이 없습니다. 방을 만들어 상대를 기다리세요.</p>`;
    $("roomList").querySelectorAll("button").forEach((button) => button.onclick = () => joinRoom(button.dataset.code, ""));
  } catch (error) { toast(error.message); }
}
$("refreshRooms").onclick = refreshRooms;
$("rankingButton").onclick = () => openRankings("");
$("historyButton").onclick = () => openHistory("");
$("closeRanking").onclick = openLobby;
function formatDuration(seconds) {
  const minutes = Math.floor(seconds / 60);
  const rest = seconds % 60;
  return `${minutes}:${String(rest).padStart(2, "0")}`;
}
function partyLabel(entry) {
  if (entry.party_size > 0) return `${entry.party_size}인 플레이`;
  return entry.mode === "solo" ? "솔로" : "듀오";
}
function scoreBreakdownText(breakdown) {
  const labels = { survival: "생존", deduction: "AI 지목", machine: "기계식 답변", defense: "변론", clear: "완주" };
  return Object.entries(breakdown || {}).filter(([key, value]) => key !== "penalty" && value).map(([key, value]) => `${labels[key] || key} +${value}`).join(" · ");
}
function renderRecordFilters(activeScenario, handler) {
  $("rankingFilters").innerHTML = [{ id: "", title: "전체" }, ...bootstrapData.scenarios].map((item) => `<button class="ranking-filter ${item.id === activeScenario ? "active" : ""}" type="button" data-scenario="${item.id}">${escapeHtml(item.title)}</button>`).join("");
  $("rankingFilters").querySelectorAll("button").forEach((button) => button.onclick = () => handler(button.dataset.scenario));
}
function renderScoreRules(rules) {
  $("scoreRules").innerHTML = rules.map((rule) => `<div class="score-rule"><span>${escapeHtml(rule.label)}</span><strong>${escapeHtml(rule.points)}</strong><small>${escapeHtml(rule.detail)}</small></div>`).join("");
}
async function openRankings(scenarioId) {
  try {
    const suffix = scenarioId ? `?scenario_id=${encodeURIComponent(scenarioId)}` : "";
    const data = await api(`/api/rankings${suffix}`);
    show("rankingScreen");
    $("recordsEyebrow").textContent = "SURVIVAL RANKING";
    $("recordsTitle").textContent = "생존자 랭킹";
    $("profileNickname").hidden = true;
    $("recordsNote").textContent = "계정마다 최고 기록 하나만 순위에 반영됩니다.";
    $("recordPositionLabel").textContent = "순위";
    $("recordPlayerLabel").textContent = "생존자";
    $("historySummary").hidden = true;
    renderRecordFilters(scenarioId, openRankings);
    $("rankingList").innerHTML = data.entries.length ? data.entries.map((entry) => `<div class="ranking-row ${entry.you ? "you" : ""}"><span class="ranking-position">${entry.rank}</span><div class="ranking-player"><strong>${escapeHtml(entry.nickname || entry.username)}</strong><small>${partyLabel(entry)}${entry.you ? " · 나" : ""}</small></div><div class="ranking-record"><strong>${escapeHtml(entry.scenario)} · ${entry.cleared ? "CLEAR" : `DAY ${entry.day_reached}`}</strong><small>AI 식별 ${entry.ai_expelled} · ${formatDuration(entry.duration_seconds)}</small></div><span class="ranking-score">${entry.score.toLocaleString()}P</span></div>`).join("") : '<p class="ranking-empty">아직 기록된 생존자가 없습니다.</p>';
    renderScoreRules(data.score_rules);
  } catch (error) { toast(error.message); }
}
async function openHistory(scenarioId) {
  try {
    const suffix = scenarioId ? `?scenario_id=${encodeURIComponent(scenarioId)}` : "";
    const data = await api(`/api/history${suffix}`);
    show("rankingScreen");
    $("recordsEyebrow").textContent = "SURVIVOR PROFILE";
    $("recordsTitle").textContent = "내 프로필";
    $("profileNickname").textContent = user.nickname || user.username;
    $("profileNickname").hidden = false;
    $("recordsNote").textContent = "최근 10판 · 누적 통계는 모든 시나리오의 전체 플레이 기준입니다.";
    $("recordPositionLabel").textContent = "최근";
    $("recordPlayerLabel").textContent = "플레이";
    $("historySummary").hidden = false;
    $("historySummary").innerHTML = `<div class="history-stat"><strong>${data.summary.plays.toLocaleString()}판</strong><small>누적 플레이</small></div><div class="history-stat"><strong>${data.summary.clears.toLocaleString()}회</strong><small>누적 클리어</small></div><div class="history-stat"><strong>${Number(data.summary.average_day || 0).toFixed(1)}일</strong><small>평균 도달일</small></div><div class="history-stat"><strong>${data.summary.best_score.toLocaleString()}P</strong><small>최고 점수</small></div>`;
    renderRecordFilters(scenarioId, openHistory);
    $("rankingList").innerHTML = data.entries.length ? data.entries.map((entry) => `<div class="ranking-row you"><span class="ranking-position">${entry.rank}</span><div class="ranking-player"><strong>${partyLabel(entry)}</strong><small>${new Date(entry.created_at).toLocaleDateString("ko-KR")}</small></div><div class="ranking-record"><strong>${escapeHtml(entry.scenario)} · ${entry.cleared ? "CLEAR" : `DAY ${entry.day_reached}`}</strong><small>${escapeHtml(scoreBreakdownText(entry.breakdown) || "획득 점수 없음")} · ${formatDuration(entry.duration_seconds)}</small></div><span class="ranking-score">${entry.score.toLocaleString()}P</span></div>`).join("") : '<p class="ranking-empty">아직 완료된 플레이 기록이 없습니다.</p>';
    renderScoreRules(data.score_rules);
  } catch (error) { toast(error.message); }
}
$("joinForm").onsubmit = (event) => { event.preventDefault(); joinRoom($("roomCode").value.trim().toUpperCase(), ""); };
async function joinRoom(code, password) {
  if (code.length !== 4) { toast("방 코드 4자리를 입력하세요."); return; }
  try {
    const data = await api("/api/rooms/join", { method: "POST", body: JSON.stringify({ code, password }) });
    $("duoModal").hidden = true;
    setRoom(data.room);
  }
  catch (error) {
    if (!password && error.message.includes("비밀번호")) {
      const answer = prompt("비밀방입니다. 비밀번호를 입력하세요.");
      if (answer !== null) return joinRoom(code, answer);
      return;
    }
    toast(error.message);
  }
}

function setRoom(nextRoom) {
  room = nextRoom;
  syncRoomTimer(room);
  if (!room.started) renderWaiting(); else { show("gameScreen"); renderGame(true); connectSocket(); }
}
function renderWaiting() {
  show("waitingScreen");
  $("copyCode").textContent = room.code;
  $("waitingScenario").textContent = room.scenario.title;
  $("waitingRoster").innerHTML = room.participants.map((p) => {
    const name = p.nickname || "";
    const label = p.alias ? (name ? `${p.alias} · ${name}` : p.alias) : name;
    return `<div class="waiting-unit${p.you ? " me" : ""}">${escapeHtml(label)}</div>`;
  }).join("");
  $("startRoom").hidden = !room.host;
  if (!socket || socket.readyState >= WebSocket.CLOSING) connectSocket();
}
async function copyInvite(value, message) {
  try { await navigator.clipboard.writeText(value); toast(message); }
  catch { toast(`복사 권한이 없습니다. 방 코드 ${room.code}를 직접 전달해 주세요.`); }
}
$("copyCode").onclick = () => copyInvite(room.code, "방 코드를 복사했습니다.");
$("copyLink").onclick = () => copyInvite(`${location.origin}/?join=${room.code}`, "초대 링크를 복사했습니다. 상대에게 보내세요.");
$("startRoom").onclick = async () => {
  try {
    if (room?.mode === "multi") {
      // 스토리는 방이 찬 뒤에 본다 — 방장이 다 읽으면 팀원도 동시에 시작된다.
      await api("/api/rooms/brief", { method: "POST", body: "{}" });
      await playCinematic(bootstrapData.scenarios.find((item) => item.id === room.scenario.id));
    }
    const data = await api("/api/rooms/start", { method: "POST", body: "{}" });
    setRoom(data.room);
  } catch (error) {
    toast(error.message);
    if (room && !room.started) renderWaiting();
  }
};
async function leaveAndCloseRoom() {
  try { await api("/api/rooms/leave", { method: "POST", body: "{}" }); }
  catch (error) { toast(error.message); }
  room = null;
  openLobby();
}
$("leaveRoom").onclick = leaveAndCloseRoom;

function connectSocket() {
  disconnectSocket();
  const protocol = location.protocol === "https:" ? "wss" : "ws";
  socket = new WebSocket(`${protocol}://${location.host}/ws`);
  socket.onmessage = (event) => {
    if (suspending) return;
    const message = JSON.parse(event.data);
    if (message.type === "state") {
      const nextRoom = message.room;
      const me = nextRoom.participants.find((participant) => participant.you);
      pendingChats = pendingChats.filter((pending) => !nextRoom.messages.some((item) => item.sender_pid === me?.pid && item.id > pending.afterId && item.text === pending.text));
      if (nextRoom.phase !== "discussion") pendingChats = [];
      room = nextRoom;
      syncRoomTimer(room);
      if (room.closed) {
        if (cinematicScenario) finishCinematic();
        room = null; openLobby(); toast("참가자가 나가 방이 종료됐습니다."); return;
      }
      if (room.started) {
        if (cinematicScenario) finishCinematic();
        show("gameScreen"); renderGame();
      } else if (room.briefing && !room.host && briefedCode !== room.code) {
        // 방장이 검증을 시작했다 — 팀원도 스토리를 본다(스킵 불가). 방장이 끝내면 게임이 함께 뜬다.
        briefedCode = room.code;
        const scenario = bootstrapData.scenarios.find((item) => item.id === room.scenario.id);
        playCinematic(scenario, false).then(() => { if (room && !room.started && !room.closed) renderWaiting(); });
      } else if (!cinematicScenario) {
        renderWaiting();
      }
    }
    if (message.type === "chat_ack") pendingChats = pendingChats.filter((pending) => pending.clientId !== message.client_id);
    if (message.type === "error") {
      if (message.client_id) pendingChats = pendingChats.filter((pending) => pending.clientId !== message.client_id);
      toast(message.message);
      if (room) renderMessages(true, room.participants.find((participant) => participant.you)?.pid);
    }
  };
  socket.onclose = () => { if (room) reconnectTimer = setTimeout(connectSocket, 2000); };
}
function disconnectSocket() { clearTimeout(reconnectTimer); reconnectTimer = null; if (socket) { socket.onclose = null; socket.close(); socket = null; } }
function syncRoomTimer(nextRoom) {
  if (!nextRoom?.started || nextRoom.phase === "ended") {
    timerEndsAt = 0;
    timerStateKey = "";
    return;
  }
  const key = `${nextRoom.code}:${nextRoom.day}:${nextRoom.phase}:${nextRoom.deadline}`;
  const nextEnd = performance.now() + Math.max(0, nextRoom.remaining_seconds || 0) * 1000;
  if (key !== timerStateKey || Math.abs(nextEnd - timerEndsAt) > 1200) {
    timerStateKey = key;
    timerEndsAt = nextEnd;
  }
}
function send(action) {
  if (socket?.readyState !== WebSocket.OPEN) {
    toast("통제망에 다시 연결 중입니다. 작성한 내용은 보관했습니다.");
    return false;
  }
  socket.send(JSON.stringify(action));
  return true;
}

const phaseNames = { discussion: "자유 토론", day_decision: "결론 투표", night_vote: "밤의 지목", defense: "최후의 변론", verdict: "폐기 판결", result: "판정 결과", ended: "검증 종료", lobby: "대기" };
function renderGame(force = false) {
  $("gameBackdrop").style.backgroundImage = `url('${room.scenario.background}')`;
  const me = room.participants.find((p) => p.you);
  $("dayLabel").textContent = `DAY ${room.day} · ${(me?.score || 0).toLocaleString()}P`;
  $("storyTitle").textContent = room.story.title;
  $("phaseLabel").textContent = phaseNames[room.phase] || room.phase;
  $("taskTitle").textContent = room.story.task;
  $("taskContext").textContent = room.story.context;
  $("taskHint").textContent = room.story.hint;
  $("taskStarter").textContent = room.story.starter;
  $("difficultyLabel").textContent = `${room.story.difficulty} · 이렇게 시작해 보세요`;
  $("riskLabel").textContent = room.risk;
  $("riskLabel").dataset.risk = room.risk;
  $("roomLabel").textContent = room.mode === "solo" ? "SOLO" : room.code;
  $("exitGame").title = room.mode === "solo" ? "나가기 (저장/종료 선택)" : "나가기 (AI가 자리를 이어받음)";
  $("gameRoster").innerHTML = room.participants.map((p) => `<div class="unit-row ${p.you ? "you" : ""} ${p.alive ? "" : "dead"}"><span class="unit-orb">${p.alive ? "ON" : "OFF"}</span><button class="roster-mention" type="button" data-pid="${p.pid}" ${p.you || !p.alive ? "disabled" : ""}><strong>${escapeHtml(p.alias)}</strong><small>${p.you ? "내 신호" : p.revealed || "신원 미확인"}</small></button></div>`).join("");
  $("gameRoster").querySelectorAll(".roster-mention:not(:disabled)").forEach((button) => button.onclick = () => insertMention(button.dataset.pid));
  const choiceLabels = (room.story.choices || []).map((choice) => choice.label).join("  vs  ");
  $("systemBanner").textContent = choiceLabels ? `오늘의 선택 — ${choiceLabels}` : `토론 안건: ${room.story.title}`;
  renderEnding();
  renderMessages(force, me?.pid);
  renderAction(me);
}
function renderMessages(force, myPid) {
  const log = $("chatLog");
  const last = room.messages.at(-1)?.id || 0;
  const pendingKey = pendingChats.map((pending) => pending.clientId).join(":");
  if (!force && last === renderedMessageId && pendingKey === renderedPendingKey) return;
  const nearBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 90;
  const myAlias = room.participants.find((participant) => participant.pid === myPid)?.alias || "나";
  const confirmed = room.messages.map((message) => message.kind === "chat" ? `<div class="message ${message.sender_pid === myPid ? "you" : ""}"><button class="message-name mention-button" type="button" data-pid="${message.sender_pid}" ${message.sender_pid === myPid ? "disabled" : ""}>${escapeHtml(message.sender)}</button><p class="message-text">${formatChatText(message.text)}</p></div>` : `<div class="message system-message ${message.kind}">${escapeHtml(message.text)}</div>`).join("");
  const pending = pendingChats.map((message) => `<div class="message you pending"><button class="message-name" type="button" disabled>${escapeHtml(myAlias)}</button><p class="message-text">${formatChatText(message.text)}<small>전송 중</small></p></div>`).join("");
  log.innerHTML = confirmed + pending;
  log.querySelectorAll(".mention-button:not(:disabled)").forEach((button) => button.onclick = () => insertMention(button.dataset.pid));
  renderedMessageId = last;
  renderedPendingKey = pendingKey;
  if (nearBottom || force || document.documentElement.classList.contains("keyboard-open")) {
    log.scrollTop = log.scrollHeight;
    settleMobileChat();
  }
}
function insertMention(pid) {
  const participant = room?.participants.find((item) => item.pid === pid);
  const input = $("actionDock").querySelector("textarea");
  if (!participant || !input || room.phase !== "discussion") {
    toast("자유 토론 중에 참가자를 언급할 수 있습니다.");
    return;
  }
  const mention = `@${participant.alias} `;
  const start = input.selectionStart ?? input.value.length;
  const end = input.selectionEnd ?? start;
  const prefix = start > 0 && !/\s$/.test(input.value.slice(0, start)) ? " " : "";
  input.setRangeText(prefix + mention, start, end, "end");
  input.dispatchEvent(new Event("input", { bubbles: true }));
  input.focus();
}
function renderAction(me) {
  const dock = $("actionDock");
  const actionKey = `${room.day}:${room.phase}:${room.you_alive}:${room.you_voted}:${room.you_verdict}:${room.you_decided || ""}:${room.defense_submitted}:${room.accused_pid || ""}`;
  if (dock.dataset.actionKey === actionKey) return;
  const previousInput = dock.querySelector("textarea");
  if (previousInput) writeDraft(dock.dataset.draftKind || room.phase, previousInput.value);
  dock.dataset.actionKey = actionKey;
  delete dock.dataset.draftKind;
  if (!room.you_alive) { dock.innerHTML = `<div class="action-note">신호가 폐기되었습니다. 남은 검증을 관전합니다.</div>`; return; }
  if (room.phase === "discussion") {
    dock.innerHTML = `<form class="chat-compose"><textarea maxlength="240" placeholder="${escapeHtml(room.story.starter)}"></textarea><button class="primary" type="submit">전송</button></form>`;
    dock.dataset.draftKind = "discussion";
    const input = dock.querySelector("textarea");
    bindDraft(input, "discussion");
    dock.querySelector("form").onsubmit = (event) => {
      event.preventDefault();
      const text = input.value.trim();
      if (text) {
        const clientId = `${Date.now()}-${Math.random().toString(36).slice(2, 9)}`;
        if (send({ action: "chat", text, client_id: clientId })) {
          pendingChats.push({ clientId, text, afterId: room.messages.at(-1)?.id || 0 });
          input.value = "";
          clearDraft("discussion");
          renderMessages(true, me?.pid);
        }
      }
    };
  } else if (room.phase === "day_decision") {
    const choices = room.story.choices || [];
    dock.innerHTML = `<div class="decision-block"><p class="decision-question">오늘의 결론은 무엇입니까? 동률이면 무작위로 결정됩니다.</p><div class="decision-actions">${choices.map((choice) => `<button class="${room.you_decided === choice.key ? "chosen" : ""}" type="button" data-choice="${choice.key}">${escapeHtml(choice.label)}</button>`).join("")}</div></div>`;
    dock.querySelectorAll("button").forEach((button) => button.onclick = () => send({ action: "decide", choice: button.dataset.choice }));
  } else if (room.phase === "night_vote") {
    if (room.you_voted) {
      dock.innerHTML = `<div class="action-note">지목을 전송했습니다. 다른 개체의 선택을 기다립니다.</div>`;
    } else {
      dock.innerHTML = `<div class="target-grid">${room.participants.filter((p) => p.alive && !p.you).map((p) => `<button class="target-button" type="button" data-pid="${p.pid}">${escapeHtml(p.alias)}<br><small>인간으로 지목</small></button>`).join("")}</div>`;
      dock.querySelectorAll("button").forEach((button) => button.onclick = () => send({ action: "vote", target_pid: button.dataset.pid }));
    }
  } else if (room.phase === "defense") {
    if (me.pid === room.accused_pid) {
      if (room.defense_submitted) {
        dock.innerHTML = `<div class="action-note">변론을 제출했습니다. 판결을 기다립니다.</div>`;
      } else {
        dock.innerHTML = `<form class="defense-compose"><textarea maxlength="360" placeholder="당신이 정상 AI인 이유와 다른 개체의 모순을 설명하세요."></textarea><button class="primary" type="submit">변론 제출</button></form>`;
        dock.dataset.draftKind = "defense";
        const input = dock.querySelector("textarea");
        bindDraft(input, "defense");
        dock.querySelector("form").onsubmit = (event) => {
          event.preventDefault();
          const text = input.value.trim();
          if (text && send({ action: "defend", text })) clearDraft("defense");
        };
      }
    } else { const accused = room.participants.find((p) => p.pid === room.accused_pid); dock.innerHTML = `<div class="action-note">${escapeHtml(accused?.alias)}의 최후 변론을 듣고 있습니다.</div>`; }
  } else if (room.phase === "verdict") {
    if (me.pid === room.accused_pid) dock.innerHTML = `<div class="action-note">판결 대상은 투표할 수 없습니다.</div>`;
    else if (room.you_verdict) dock.innerHTML = `<div class="action-note">판결을 전송했습니다.</div>`;
    else { dock.innerHTML = `<div class="verdict-actions"><button class="approve" data-value="true" type="button">폐기 찬성</button><button data-value="false" type="button">폐기 반대</button></div>`; dock.querySelectorAll("button").forEach((button) => button.onclick = () => send({ action: "verdict", approve: button.dataset.value === "true" })); }
  } else if (room.phase === "result") {
    const verdicts = room.last_result?.verdicts || [];
    const approvals = verdicts.filter((item) => item.approve).map((item) => item.alias).join(", ") || "없음";
    const rejections = verdicts.filter((item) => !item.approve).map((item) => item.alias).join(", ") || "없음";
    dock.innerHTML = `<div class="action-note verdict-result"><strong>${escapeHtml(room.last_result?.title || "판정 집계 중")}</strong><span>${escapeHtml(room.last_result?.text || "")}</span><small>찬성: ${escapeHtml(approvals)} · 반대: ${escapeHtml(rejections)}</small></div>`;
  } else if (room.phase === "ended") {
    dock.innerHTML = `<div class="action-note">검증이 종료됐습니다. 결과 창을 확인하십시오.</div>`;
  }
}

const SCORE_DETAILS = {
  survival: ["하루 생존", "DAY 판정을 통과할 때마다 +150"],
  deduction: ["AI 정확히 지목", "내가 지목한 AI가 폐기될 때 +300"],
  machine: ["기계식 답변", "수치·조건을 갖춘 첫 발언, 하루 1회 +50"],
  defense: ["변론 성공", "내 폐기 판결이 부결될 때 +200"],
  clear: ["검증 완료", "7일 완주 또는 AI 전원 폐기 +1,000"],
  penalty: ["패널티", ""],
};
function renderEnding() {
  const modal = $("endingModal");
  if (!room || !room.started || room.phase !== "ended") { modal.hidden = true; return; }
  const me = room.participants.find((p) => p.you);
  $("endingTitle").textContent = room.you_alive ? "생존 성공" : "신원 노출 — 폐기";
  $("endingText").textContent = room.ending || "";
  const rows = Object.entries(me?.score_breakdown || {}).filter(([, value]) => value).map(([key, value]) => `<div class="ending-row"><div><strong>${escapeHtml(SCORE_DETAILS[key]?.[0] || key)}</strong><small>${escapeHtml(SCORE_DETAILS[key]?.[1] || "")}</small></div><span>${value > 0 ? "+" : ""}${value.toLocaleString()}P</span></div>`).join("");
  $("endingRows").innerHTML = rows || '<div class="ending-row"><div><strong>획득 점수 없음</strong><small>발언·지목·생존으로 점수를 얻을 수 있습니다.</small></div><span>0P</span></div>';
  $("endingTotal").textContent = `${(me?.score || 0).toLocaleString()}P`;
  renderEndingReport(room.report);
  modal.hidden = false;
}
function renderEndingReport(report) {
  const box = $("endingReport");
  if (!report || (!report.death && !report.events?.length && !report.night_notes?.length)) { box.hidden = true; return; }
  const parts = [];
  if (report.death) parts.push(`<p class="report-death">DAY ${report.death.day} 밤, 찬성 ${report.death.approvals} 대 반대 ${report.death.rejections}로 폐기됐습니다.</p>`);
  if (report.night_notes?.length) {
    parts.push(`<p class="report-label">AI들의 판단 근거</p>` + report.night_notes.map((note) => `<p class="report-line">DAY ${note.day} — ${escapeHtml(note.detail)}</p>`).join(""));
  }
  if (report.events?.length) {
    parts.push(`<p class="report-label">의심을 키운 순간</p>` + report.events.map((event) => `<p class="report-line">DAY ${event.day} — ${escapeHtml(event.detail)}${event.why ? ` <small>(${escapeHtml(event.why)})</small>` : ""}</p>`).join(""));
  }
  box.innerHTML = `<p class="report-title">판정 리포트</p>` + parts.join("");
  box.hidden = false;
}
$("endingLeave").onclick = leaveAndCloseRoom;

document.querySelector(".task-panel").onclick = () => {
  if (window.matchMedia("(max-width: 640px)").matches) document.querySelector(".task-panel").classList.toggle("expanded");
};

setInterval(() => {
  if (!room || !room.started || room.phase === "ended") { $("timerLabel").textContent = "--:--"; return; }
  const remaining = Math.max(0, Math.ceil((timerEndsAt - performance.now()) / 1000));
  $("timerLabel").textContent = `${String(Math.floor(remaining / 60)).padStart(2, "0")}:${String(remaining % 60).padStart(2, "0")}`;
}, 250);

$("logoutButton").onclick = async () => { try { await api("/api/auth/logout", { method: "POST", body: "{}" }); } catch {} location.reload(); };
$("resumeButton").onclick = async () => {
  try {
    const data = await api("/api/rooms/resume", { method: "POST", body: "{}" });
    setRoom(data.room);
  } catch (error) { toast(error.message); }
};
$("exitGame").onclick = async () => {
  if (!room) return;
  if (room.phase === "ended") { await leaveAndCloseRoom(); return; }
  if (room.mode === "solo") { $("exitModal").hidden = false; return; }
  if (!confirm("나가면 AI가 당신 자리를 이어받아 게임을 계속합니다. 나갈까요?")) return;
  await exitRoom("save", "AI가 자리를 이어받았습니다.");
};
async function exitRoom(action, message) {
  try {
    suspending = true;
    await api("/api/rooms/exit", { method: "POST", body: JSON.stringify({ action }) });
    room = null;
    openLobby();
    toast(message);
  } catch (error) { suspending = false; toast(error.message); }
}
$("exitSave").onclick = () => exitRoom("save", "게임을 저장했습니다. 로비에서 이어할 수 있습니다.");
$("exitClose").onclick = () => { if (confirm("기록 없이 방을 종료할까요? 이번 진행은 사라집니다.")) exitRoom("close", "방을 종료했습니다."); };
$("exitCancel").onclick = () => { $("exitModal").hidden = true; };
$("deleteSave").onclick = async () => {
  if (!confirm("저장된 검증 기록을 삭제할까요?")) return;
  try {
    await api("/api/rooms/saved/delete", { method: "POST", body: "{}" });
    refreshResume();
    toast("저장 기록을 삭제했습니다.");
  } catch (error) { toast(error.message); }
};

async function initialize() {
  try {
    bootstrapData = await api("/api/bootstrap");
    if (!bootstrapData.authenticated) { show("authScreen"); return; }
    user = bootstrapData.user; csrf = bootstrapData.csrf;
    if (!user.onboarding_complete) { startGuide(); return; }
    await resumeRoomOrLobby();
  } catch (error) { show("authScreen"); $("authError").textContent = error.message; }
}
initialize();
