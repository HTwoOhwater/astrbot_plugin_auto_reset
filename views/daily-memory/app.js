const $ = id => document.getElementById(id);
const bridge = window.AstrBotPluginView || window.AstrBotPluginPage;
let current = null;
let dirty = false;
let working = false;

const phases = {waiting: ["等待处理", "neutral"], archived: ["原文已归档", "neutral"], imported: ["已导入记忆", "neutral"], prepared: ["正在换新", "warn"], done: ["换新完成", "good"], superseded: ["已结束 / 已替代", "neutral"]};
const summaries = {pending: ["等待整理", "neutral"], paused: ["需要处理", "warn"], complete: ["整理完成", "good"]};
const reasons = {
  session_busy_retry_later: "正在回复，稍后重试", session_lock_busy: "会话正忙，稍后重试",
  companion_background_busy_retry_later: "陪伴插件正在整理群记忆", no_current_conversation: "这个群还没有原生对话",
  companion_group_not_observed: "陪伴插件尚未捕获这个群，请先检查其群白名单",
  companion_group_aliases_require_normalization: "陪伴插件中存在重复群标识，需要先归一化",
  memory_capture_or_summary_disabled: "请开启记忆捕获、群聊连续记忆和摘要",
  memory_scope_capture_disabled: "记忆插件未允许捕获这个群",
  memory_evidence_review_required: "记忆插件中存在待审核证据，处理后再重试",
  isolated_group_sessions_unsupported: "暂不支持按群成员隔离会话",
  multi_persona_companion_unsupported: "暂不支持陪伴多人格模式", third_party_runner_unsupported: "需要使用 AstrBot 原生 local runner",
  unsupported_astrbot_version: "AstrBot 版本不匹配，此版要求 4.29.1",
  unsupported_or_disabled_astrbot_plugin_memory_companion: "记忆插件未启用或版本不匹配，要求 1.10.5",
  unsupported_or_disabled_astrbot_plugin_private_companion: "陪伴插件未启用或版本不匹配，要求 6.7.0",
  conversation_changed_externally: "原生对话已被手动切换，保留当前选择",
  conversation_changed_retry_later: "消息发生变化，将重新归档后重试",
  native_history_changed_retry_later: "原生对话有新消息，稍后重试",
  native_group_cache_changed_retry_later: "群聊缓存有新消息，稍后重试",
  companion_history_changed_retry_later: "陪伴近期记录有变化，稍后重试",
  summary_no_progress: "本批次没有整理进展，请检查记忆插件",
  pending_timeline_missing: "待处理原始时间线缺失，请检查记忆插件和备份",
  archive_too_large: "本次归档超过上限，请先处理积压",
  memory_backlog_too_large: "记忆积压超过上限，请先处理积压",
  native_group_cache_owner_unavailable: "未找到原生群聊缓存，请检查核心插件",
  OSError: "存储或读写异常，请检查磁盘和权限", TimeoutError: "模型或接口超时，可以稍后重试",
};

function notice(message, error = false) {
  $("notice").textContent = message;
  $("notice").className = `notice ${error ? "error" : "info"}`;
  $("notice").hidden = false;
}
function reason(code) { return reasons[code] || (code ? `检查未通过（${code}）` : "—"); }
function badge(text, tone) {
  const span = document.createElement("span");
  span.className = `badge ${tone}`;
  span.textContent = text;
  return span;
}
function cell(text, cls = "") {
  const td = document.createElement("td");
  td.textContent = text;
  td.className = cls;
  return td;
}
function setDirty(value) {
  dirty = value;
  $("dirty").textContent = value ? "有未保存的修改" : "设置已同步";
  updateControls();
}
function updateControls() {
  const ready = Boolean(current) && !working;
  for (const id of ["enabled", "run-time", "sessions", "save", "refresh"]) $(id).disabled = !ready;
  const canAct = ready && current.config.enabled && current.config.sessions.length > 0 && !current.busy && !dirty;
  for (const id of ["action-session", "run-now", "retry"]) $(id).disabled = !canAct;
}
function render(data, fillForm) {
  current = data;
  $("version").textContent = `v${data.version}`;
  $("enabled-status").textContent = data.config.enabled ? "已开启" : "未开启";
  $("scope-status").textContent = `${data.config.sessions.length} 个生效群`;
  $("next-time").textContent = data.next_run.slice(11, 16);
  $("next-date").textContent = `${data.next_run.slice(0, 10)} · 繁忙时自动延后`;
  $("today-count").textContent = `${data.stats.today_done} / ${data.config.sessions.length}`;
  // Different days can reference overlapping timeline IDs. Count jobs instead
  // of summing potentially duplicated rows across those jobs.
  $("pending-count").textContent = `${data.stats.pending_jobs} 项`;
  $("pending-status").textContent = data.stats.paused_jobs > 0 ? "有摘要暂停，请检查后重试" : "原文归档持续保留";
  $("worker-status").textContent = data.busy ? "任务处理中" : "等待巡检";
  $("updated").textContent = `更新于 ${data.now.slice(11, 19)}（北京时间）`;
  if (fillForm) {
    $("enabled").checked = data.config.enabled;
    $("run-time").value = `${String(data.config.hour).padStart(2, "0")}:${String(data.config.minute).padStart(2, "0")}`;
    $("sessions").value = data.config.sessions.join("\n");
    setDirty(false);
  }
  const previous = $("action-session").value;
  $("action-session").replaceChildren();
  for (const session of data.config.sessions) {
    const option = document.createElement("option");
    option.value = session; option.textContent = session;
    $("action-session").append(option);
  }
  if (!data.config.sessions.length) {
    const option = document.createElement("option"); option.value = ""; option.textContent = "请先保存生效群"; $("action-session").append(option);
  } else if (data.config.sessions.includes(previous)) $("action-session").value = previous;
  $("dependencies").replaceChildren();
  const depsGood = data.dependencies.every(d => d.active && d.version.replace(/^v/, "") === d.expected);
  for (const dep of data.dependencies) {
    const li = document.createElement("li");
    const label = document.createElement("span"); label.textContent = dep.name;
    const expected = document.createElement("small"); expected.textContent = `要求 ${dep.expected}`; label.append(expected);
    const ok = dep.active && dep.version.replace(/^v/, "") === dep.expected;
    li.append(label, badge(`${dep.version}${dep.active ? "" : " · 未启用"}`, ok ? "good" : "warn"));
    $("dependencies").append(li);
  }
  const allGood = depsGood && data.checks.length > 0 && data.checks.every(c => c.ok);
  $("check-badge").textContent = allGood ? "检查通过" : data.checks.length ? "请检查" : "待选择群聊";
  $("check-badge").className = `badge ${allGood ? "good" : "warn"}`;
  $("scope-checks").replaceChildren();
  for (const check of data.checks) {
    const item = document.createElement("div"); item.className = `scope-result ${check.ok ? "good" : "warn"}`;
    const title = document.createElement("strong"); title.textContent = check.session;
    const detail = document.createElement("span"); detail.textContent = check.ok ? "捕获范围与接口检查通过" : reason(check.reason);
    item.append(title, detail); $("scope-checks").append(item);
  }
  $("jobs").replaceChildren();
  for (const job of data.jobs) {
    const row = document.createElement("tr");
    const phase = cell(""); phase.append(badge(...(phases[job.phase] || [job.phase, "neutral"])));
    const summary = cell(""); summary.append(badge(...(summaries[job.summary_state] || ["尚未排队", "neutral"])));
    const codes = [job.error, job.summary_error].filter(Boolean);
    row.append(cell(job.day), cell(job.session, "session-cell"), phase, summary, cell(String(job.pending_count)), cell(codes.length ? codes.map(reason).join("；") : "—", "reason-cell"));
    $("jobs").append(row);
  }
  if (!data.jobs.length) {
    const row = document.createElement("tr"); const td = cell("还没有归档任务。开启后将在下一次日界执行，也可以手动排队。", "empty"); td.colSpan = 6; row.append(td); $("jobs").append(row);
  }
  updateControls();
}
async function load(fillForm = false) {
  const data = await bridge.apiGet("panel/status");
  // Keep unsaved form edits paired with their original revision. A refresh
  // must not silently give an old form the latest concurrency token.
  if (dirty && current && data.revision !== current.revision) {
    notice("设置已在其他页面变化。请保存前刷新并放弃本页修改。", true);
    return;
  }
  render(data, fillForm);
}
async function run(operation) {
  if (working) return;
  working = true; updateControls();
  try { await operation(); } catch (error) { notice(error.message || "操作失败，请稍后重试。", true); }
  finally { working = false; updateControls(); }
}
function confirmAction(action, session) {
  const reload = action === "reload";
  $("dialog-title").textContent = reload ? "重新读取设置？" : action === "now" ? "确认归档并换新" : "确认重试摘要";
  $("dialog-description").textContent = reload ? "这会放弃本页尚未保存的修改，重新读取后台设置。不会执行归档或换新。" : action === "now" ? "将为以下群保存当前记录，并在会话空闲后开启新对话。同一逻辑日最多换新一次。" : "将重新排队这个群未完成的摘要，继续使用现有记忆插件整理，不会再建新对话。";
  $("dialog-session").textContent = session;
  $("dialog-session").hidden = reload;
  $("dialog-hint").hidden = reload;
  $("confirm-action").textContent = reload ? "放弃修改并刷新" : "确认执行";
  return new Promise(resolve => {
    const dialog = $("confirmation");
    dialog.addEventListener("close", () => resolve(dialog.returnValue === "confirm"), {once: true});
    dialog.returnValue = "cancel"; dialog.showModal();
  });
}
async function action(kind) {
  const session = $("action-session").value;
  if (!current || dirty || !session) return;
  const revision = current.revision;
  if (!await confirmAction(kind, session)) return;
  await run(async () => {
    const result = await bridge.apiPost("panel/action", {action: kind, session, revision, confirmed: true});
    notice(result.message); await load();
  });
}

$("settings-form").addEventListener("input", () => setDirty(true));
$("settings-form").addEventListener("submit", event => {
  event.preventDefault();
  if (!current) return;
  run(async () => {
    const [hour, minute] = $("run-time").value.split(":").map(Number);
    const sessions = $("sessions").value.split(/\r?\n/).map(s => s.trim()).filter(Boolean);
    const config = {enabled: $("enabled").checked, hour, minute, sessions};
    const result = await bridge.apiPost("panel/settings", {revision: current.revision, config});
    setDirty(false); notice(result.message); await load(true);
  });
});
$("refresh").addEventListener("click", () => run(async () => {
  if (dirty) {
    if (!await confirmAction("reload", "")) return;
    setDirty(false);
  }
  await load(true); notice("状态已刷新，未执行归档或模型摘要。");
}));
$("run-now").addEventListener("click", () => action("now"));
$("retry").addEventListener("click", () => action("retry"));
window.addEventListener("beforeunload", event => { if (dirty) { event.preventDefault(); event.returnValue = ""; } });

if (!bridge) {
  notice("请从 AstrBot 后台侧栏的「每日记忆」打开此页面，以连接插件。", true);
} else {
  await run(async () => { await bridge.ready(); await load(true); });
  const timer = setInterval(() => { if (!dirty && !working && !document.hidden) run(() => load()); }, 15000);
  window.addEventListener("pagehide", () => clearInterval(timer), {once: true});
}
