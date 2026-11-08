import { SCENARIOS } from "/scenarios.mjs";

const $ = (id) => document.getElementById(id);
const terminal = new Set(["completed", "declined", "cancelled", "failed", "unknown", "clarification"]);
const active = new Set(["starting", "preparing", "recording", "transcribing", "review", "planning", "plan", "approval", "executing"]);
const capturing = new Set(["preparing", "recording"]);
const names = { list_directory: "List a directory", create_directory: "Create a folder", create_note: "Create a text note", copy_file: "Copy a file", delete_file: "Permanently delete a file", create_python_script: "Create a Python script", run_python_script: "Run a Python script" };
const glyphs = { list_directory: "list", create_directory: "folder", create_note: "note", copy_file: "copy", delete_file: "delete", create_python_script: "code", run_python_script: "play" };
// Text nodes prevent HTML execution. Make misleading invisible/control characters
// visible as well; this display escaping never changes approved source/arguments.
const visible = (text) => String(text).replace(/[\u0000-\u0008\u000b-\u001f\u007f-\u009f\u061c\u200b-\u200f\u202a-\u202e\u2060-\u206f\ufeff]/g, (value) => `\\u${value.charCodeAt(0).toString(16).padStart(4, "0")}`);
function node(tag, className = "", text) { const value = document.createElement(tag); if (className) value.className = className; if (text !== undefined) value.textContent = visible(text); return value; }
function icon(name) { const value = document.createElementNS("http://www.w3.org/2000/svg", "svg"); value.setAttribute("class", "icon"); value.setAttribute("aria-hidden", "true"); const use = document.createElementNS("http://www.w3.org/2000/svg", "use"); use.setAttribute("href", `#i-${name}`); value.append(use); return value; }
function button(label, style, handler, name) { const value = node("button", style, label); value.type = "button"; if (name) value.append(icon(name)); value.addEventListener("click", handler); return value; }
function safety(text, danger = false) { const value = node("div", `safety-note${danger ? " is-danger" : ""}`); value.append(icon(danger ? "info" : "shield"), node("p", "", text)); return value; }
const announce = (text) => { $("announcement").textContent = text; };

let config;
let state = { phase: "disconnected", request_id: "", connected: false, blocked: false, working: false, steps: [], results: [], pending: null, error: null, version: -1, transcript: "" };
let corrections = [];
let commandBusy = false;
let networkLost = false;
let latestVersion = -1;
let signature = "";
let suspended = false;
let backgrounded = document.visibilityState === "hidden";
let consentGranted = false;
let presenceSequence = 0;
let presencePending = 0;
let lastPhase = "";
let notice = "";

const errorText = {
  unavailable: "The local session is unavailable. Check the terminal that launched MacIntent.",
  unauthorized: "This page belongs to a different server session. Reload before reconnecting; no action was retried.",
  not_owner: "Another page owns this session. Wait for it to disconnect.",
  busy: "A request is still finishing. Wait for its actual outcome.",
  stale_request: "This control belongs to an earlier request. Nothing was resubmitted.",
  stale_approval: "That approval is no longer current. Inspect the latest action; no approval was retried.",
  approval_expired: "Approval expired. No new approval was sent; inspect the final result.",
  invalid_request: "The request or correction notes exceeded the accepted text/format limits.",
  session_blocked: "Execution stopped for inspection. Check the workspace and audit, then restart the local app deliberately.",
  not_connected: "Connect before recording or submitting a request.",
  invalid_state: "That control is no longer available for the current request.",
};

async function api(path, payload, { keepalive = false } = {}) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 8000);
  try {
    const headers = config ? { "x-macintent-token": config.token, "x-macintent-client": config.client_id } : {};
    if (payload !== undefined) headers["Content-Type"] = "application/json";
    const response = await fetch(path, { method: payload === undefined ? "GET" : "POST", headers,
      ...(payload === undefined ? {} : { body: JSON.stringify(payload) }), keepalive, cache: "no-store", credentials: "omit", signal: controller.signal });
    const text = await response.text();
    if (text.length > 1024 * 1024) throw new Error("network");
    const data = JSON.parse(text);
    if (!response.ok || data.ok === false) {
      const error = new Error("rejected"); error.code = Object.hasOwn(errorText, data.error) ? data.error : "unavailable"; throw error;
    }
    return data;
  } finally { clearTimeout(timeout); }
}

function apply(next) {
  if (!Number.isSafeInteger(next.version) || next.version < latestVersion) return;
  latestVersion = next.version;
  if (next.request_id !== state.request_id || terminal.has(next.phase) || next.phase === "disconnected" || next.phase === "blocked") {
    corrections = []; $("correction-input").value = "";
  }
  const previousApproval = state.pending?.id;
  state = next;
  if (networkLost) notice = "Connection restored. Showing the observed state only; no command, approval or cloud submission was retried.";
  networkLost = false;
  const current = JSON.stringify({ ...next, version: 0 });
  if (current !== signature) { signature = current; render(); }
  if (next.pending && next.pending.id !== previousApproval && $("scenario-panel").hidden && !$("connection-dialog").open) { $("action-title").tabIndex = -1; $("action-title").focus(); }
  syncControls();
}

async function command(operation, fields = {}) {
  if (!config || commandBusy || networkLost || suspended || backgrounded || presencePending) return;
  if (operation === "connect") consentGranted = true;
  if (operation === "disconnect") consentGranted = false;
  commandBusy = true;
  notice = "";
  syncControls();
  try {
    const data = await api(`/api/${operation}`, operation === "connect" ? { consent: true } : { request_id: state.request_id, ...fields });
    apply(data.state);
    if (operation === "connect") consentGranted = true;
  } catch (error) {
    if (error.code) notice = errorText[error.code];
    else { networkLost = true; notice = "Connection interrupted. The current effect may still be finishing. Only status will be checked—no action or approval is automatically retried."; }
    announce(notice);
  } finally { commandBusy = false; syncControls(); }
}

async function poll() {
  if (config && !suspended && !backgrounded) {
    try {
      apply((await api("/api/state")).state);
      if (!backgrounded && !suspended && consentGranted && !presencePending && !state.blocked && state.phase !== "occupied"
          && (state.backgrounded || !state.connected && !state.working && state.phase === "disconnected")) await updatePresence(false);
    }
    catch (error) { networkLost = true; notice = error.code ? errorText[error.code] : "Local connection lost. Controls are paused. The server stops future work if this page cannot maintain its connection; completed effects are not undone."; syncControls(); }
  }
  setTimeout(poll, 700);
}

function syncControls() {
  const disabled = commandBusy || networkLost || !config || suspended || backgrounded || presencePending > 0 || state.backgrounded;
  const review = state.phase === "review";
  const canRecord = state.connected && ["idle", "cancelled", "failed"].includes(state.phase) && !state.working;
  const canCancelCapture = state.connected && capturing.has(state.phase);
  $("record-button").disabled = disabled || state.blocked || !(canRecord || canCancelCapture);
  $("record-button").setAttribute("aria-label", canCancelCapture ? "Cancel recording and discard audio" : "Record one command");
  $("new-request").disabled = disabled || !state.connected || state.working || state.blocked || !(review || terminal.has(state.phase));
  $("cancel-request").disabled = disabled || !active.has(state.phase);
  $("send-request").disabled = disabled || !review || state.working || state.blocked;
  $("correction-input").disabled = disabled || !review;
  $("add-correction").disabled = disabled || !review || corrections.length >= (config?.max_corrections || 8);
  $("connection-button").disabled = disabled || state.phase === "occupied" || state.blocked || state.phase === "cancelling";
  $("confirm-connect").disabled = disabled || state.blocked || state.phase === "occupied";
  for (const control of $("action-content").querySelectorAll("button")) {
    control.disabled = disabled || state.blocked || !["approval", "plan"].includes(state.phase) || !state.pending || Date.now() >= state.pending.expires_at * 1000;
  }
  $("session-notice").hidden = !notice && !state.blocked;
  $("session-notice").textContent = notice || (state.blocked ? errorText.session_blocked : "");
  updateCaptureCue();
  updateDeadline();
}

function updateCaptureCue() {
  const cues = {
    preparing: ["Preparing—stay quiet", "Wait for Listening before reading the command. Microphone setup and calibration are still in progress."],
    recording: ["Listening—speak now", "Read your request, then pause naturally. The microphone button now cancels and discards; it does not submit audio."],
    transcribing: ["Microphone off · transcribing", "Capture has ended. Local speech recognition is running; nothing has been sent to Groq."],
    review: ["Transcript ready · microphone off", "Review the original and any notes in the main pane. Nothing goes to Groq until Send."],
    cancelling: ["Stopping—wait for cleanup", "Cancellation was requested. Do not start another recording until the owned recorder has finished cleanup."],
    starting: ["Getting ready · microphone off", "Loading the local speech engine. Recording never starts automatically."],
    idle: ["Ready to record", "Use Record in the main pane. Stay quiet during Preparing, then speak only after Listening."],
    cancelled: [state.working ? "Stopping—wait for cleanup" : state.connected ? "Cancelled · ready to record again" : "Cancelled · not connected", state.working ? "The previous request is still finishing cleanup. Record will unlock only when it is safe." : state.connected ? "Choose Record directly or New request. No refresh or reconnect is needed; earlier completed effects remain." : "Connect deliberately before recording again."],
    failed: [state.working ? "Finishing cleanup" : state.connected ? "Request failed · ready for a new recording" : "Request failed · not connected", state.working ? "Wait for owned cleanup before another request." : state.connected ? "Inspect the error and results, then choose Record or New request. Nothing is automatically retried." : "Connect deliberately before recording again."],
  };
  let cue = cues[state.phase] || ["Microphone off", "You can keep this reference panel open during recording and action review."];
  if (state.error?.code === "recording_limit") cue = ["Recording discarded at the limit", "No transcript or planner request was produced. Choose New request before trying again; stay quiet until Listening."];
  if (state.phase === "unknown" || state.blocked) cue = ["Stop and inspect", "Cleanup or an effect could not be confirmed. Do not record or retry actions until the fault is inspected."];
  if (networkLost) cue = ["Recording status unavailable", "The server cannot currently be reached. This panel cannot confirm whether capture has finished; no action will be retried."];
  if ($("sidebar-capture-label").textContent !== cue[0]) $("sidebar-capture-label").textContent = cue[0];
  if ($("sidebar-capture-help").textContent !== cue[1]) $("sidebar-capture-help").textContent = cue[1];
  $("sidebar-capture-status").dataset.phase = networkLost ? "unavailable" : state.phase;
}

function updateDeadline() {
  const value = $("action-content").querySelector(".approval-deadline");
  if (!value || !state.pending) return;
  const seconds = Math.max(0, Math.ceil(state.pending.expires_at - Date.now() / 1000));
  value.textContent = seconds ? `This review expires in ${seconds}s. No response means no approval.` : "This review expired. Waiting for the broker's final result…";
  value.classList.toggle("is-expired", seconds === 0);
  if (seconds === 0) for (const control of $("action-content").querySelectorAll("button")) control.disabled = true;
}
setInterval(updateDeadline, 500);

function detail(label, value, code = false) {
  const block = node("div", "detail-block"); block.append(node("span", "detail-label", label));
  if (code) block.append(node("pre", "content-preview code-preview", value));
  else if (label.includes("TEXT")) block.append(node("div", "content-preview", value));
  else { const path = node("div", "path-value"); path.append(icon("folder"), node("code", "", value)); block.append(path); }
  return block;
}
function actionDetails(action, compact = false) {
  const fragment = document.createDocumentFragment();
  const args = action.arguments;
  if (args.source !== undefined) fragment.append(detail("COPY FROM", args.source));
  fragment.append(detail(action.tool === "copy_file" ? "COPY TO" : "EXACT PATH", args.destination ?? args.path));
  if (args.content !== undefined) fragment.append(detail("EXACT NOTE TEXT", args.content));
  if (args.code !== undefined) fragment.append(detail("SOURCE CODE", args.code, true));
  if (action.tool === "run_python_script" && args.code === undefined) fragment.append(node("p", "review-instruction", "Exact existing source is captured locally and shown at this step's separate run approval. It is never uploaded to the planner."));
  if (!compact && args.code !== undefined) fragment.append(node("p", "field-help", "Invisible/control characters are escaped for review. Approved source bytes are not rewritten."));
  return fragment;
}
function planList() {
  const list = node("ol", "plan-list");
  state.steps.forEach((step, index) => {
    const item = node("li", "plan-step"); const copy = node("div"); copy.append(node("h3", "", names[step.tool] || "Workspace action"), actionDetails(step, true));
    item.append(node("span", "plan-step-number", String(index + 1)), copy); list.append(item);
  });
  return list;
}
function empty(title, text) {
  const value = node("div", "empty-action"); const orbit = node("div", "empty-orbit"); orbit.append(icon("orbit"));
  value.append(orbit, node("h3", "", title), node("p", "", text)); return value;
}

function renderAction() {
  const content = $("action-content"); content.replaceChildren();
  const phase = state.phase;
  $("action-badge").textContent = { disconnected: "Your call", starting: "Getting ready", idle: "Your call", preparing: "Mic preparing", recording: "Listening", transcribing: "Local ASR", review: "Not submitted", planning: "Groq planning", plan: "Review plan", approval: "Awaiting approval", executing: "In progress", cancelling: "Stopping", completed: "Completed", declined: "Declined", cancelled: "Cancelled", failed: "Failed", unknown: "Stop & inspect", clarification: "No action", occupied: "Another page", blocked: "Stop & inspect" }[phase] || "Unavailable";
  $("action-badge").dataset.tone = ["unknown", "failed", "blocked"].includes(phase) ? "danger" : phase === "completed" ? "success" : "normal";
  if (phase === "plan" && state.pending) {
    content.append(node("p", "review-instruction", `${state.steps.length} explicit serial steps. Review every path, body and generated line of code. Nothing has executed yet.`), planList(), safety("Reviewing this plan is not approval. Each write and script run needs its own fresh exact confirmation. Failure, decline or cancellation stops later steps; earlier effects remain."));
    const controls = node("div", "approval-controls");
    const approvalId = state.pending.id;
    controls.append(button("Cancel request", "text-button", () => command("cancel")), button("Decline plan", "button button-secondary", () => command("decide", { approval_id: approvalId, approve: false })), button("Review steps", "button button-primary", () => command("continue", { approval_id: approvalId }), "arrow"));
    content.append(controls, node("p", "approval-deadline"));
  } else if (phase === "approval" && state.pending?.action) {
    const action = state.pending.action;
    const destructive = action.tool === "delete_file";
    if (state.steps.length > 1) content.append(node("p", "step-context", `STEP ${state.step} OF ${state.steps.length} · Earlier completed effects remain`));
    const identity = node("div", "action-identity"); const glyph = node("span", "action-glyph"); glyph.append(icon(glyphs[action.tool] || "info")); const title = node("div"); title.append(node("h3", "", names[action.tool] || "Workspace action"), node("p", "", "Exact local broker preview · not executed")); identity.append(glyph, title);
    content.append(identity, actionDetails(action));
    if (state.pending.source_sha256) content.append(node("p", "run-digest", `Captured source SHA-256: ${state.pending.source_sha256}`));
    const warning = destructive ? "Permanent deletion—not Trash. There is no undo. Approval removes only the exact single regular file shown above."
      : action.tool === "run_python_script" ? "Fresh run approval for this captured source. Compute-only OS sandbox: no user/workspace file I/O, network, child processes or third-party packages. No unrestricted fallback."
      : action.tool === "create_python_script" ? "Creation only—not execution. Review every line. A script run requires a separate explicit request or plan step and fresh approval."
      : action.tool === "copy_file" ? "Both paths must be correct. The original stays unchanged; the destination must be new. No overwrite."
      : "Review exact names, content and punctuation. A safe model proposal can still misunderstand you. New creation only; no overwrite.";
    content.append(safety(warning, destructive));
    const controls = node("div", "approval-controls"); const approvalId = state.pending.id;
    controls.append(button(state.steps.length > 1 ? "Cancel workflow" : "Cancel request", "text-button", () => command("cancel")), button("Decline", "button button-secondary", () => command("decide", { approval_id: approvalId, approve: false })), button(destructive ? "Delete file permanently" : action.tool === "run_python_script" ? "Approve this run" : action.tool === "copy_file" ? "Approve copy" : "Approve creation", `button ${destructive ? "button-danger" : "button-primary"}`, () => command("decide", { approval_id: approvalId, approve: true }), destructive ? "delete" : "check"));
    content.append(controls, node("p", "approval-deadline"));
    if (state.steps.length > 1) { const all = node("details", "all-steps"); all.append(node("summary", "", "View the full proposed plan"), planList()); content.append(all); }
  } else {
    const messages = {
      disconnected: ["Nothing moves without you.", "Connect after reviewing the privacy boundary. Recording and cloud submission each still need your deliberate choice."],
      starting: ["Getting your local engine ready.", "Checking the existing helper and loading pinned local Parakeet. The microphone is off. No model request has been sent."],
      idle: ["A thought. Then a possibility.", "Choose Record, stay quiet during preparation, and speak only after Listening. Pause naturally to finish."],
      preparing: ["Preparing—stay quiet for a moment.", "The native recorder is setting up and calibrating. Wait for the actual Listening cue before speaking."],
      recording: ["Listening. This moment is yours.", "Speak your request, then pause naturally. The native recorder stops after trailing quiet; the 30-second cap discards rather than truncates."],
      transcribing: ["Turning sound into words.", "The microphone has been released. Parakeet is transcribing locally; nothing goes to Groq until you choose Send."],
      review: ["Your words, before any action.", "Read the unchanged transcript. Add explicit corrections if needed. Send submits only this recording and its ordered notes."],
      planning: ["Finding the next clear step.", "Your submitted original and review notes are being planned by Groq. No workspace action has been approved or silently retried."],
      executing: ["One step at a time.", "The local broker is observing and finalizing this step. A later write cannot run without its own approval."],
      cancelling: ["Stopping future work.", "Waiting for owned capture, inference or execution cleanup. An action already started may finish; inspect the actual outcome below."],
      completed: ["A little progress. All yours.", "The broker reported completion. Inspect the observed per-step outcomes below. Completed files and audit metadata persist."],
      declined: ["Your no means no.", "The pending effect was declined. No later plan steps ran. Earlier completed effects remain visible below."],
      cancelled: ["Stopped, not rolled back.", state.working ? "Waiting for owned cleanup. Record stays locked until the previous request has fully stopped." : state.connected ? "Choose Record again or New request—no refresh or reconnect needed. Earlier completed effects remain until you clear this view." : "Future work stopped. Earlier completed effects remain. Connect deliberately if the session has closed."],
      failed: ["This request didn't complete.", state.working ? "Waiting for owned cleanup. Record and New request unlock only after safe cleanup." : state.connected && !state.blocked ? "Inspect the error and observed results, then choose Record or New request—no refresh or reconnect. Nothing is automatically retried or rolled back." : "No automatic retry or rollback. Inspect the exact error and observed results below before reconnecting."],
      unknown: ["Stop here. Inspect first.", "The effect or cleanup could not be confirmed. Do not retry. Inspect the workspace and audit, then deliberately restart the local server."],
      blocked: ["Inspection comes first.", "This server remains stopped after an uncertain effect, audit or cleanup fault. Check the workspace and audit before restarting it."],
      clarification: ["A question, not a guess.", state.explanation || "The planner returned no action."],
      occupied: ["One operator at a time.", "Another page owns the session. Its private request is not shown here. Close or disconnect that page, or wait for its connection lease to expire."],
    };
    content.append(empty(...(messages[phase] || ["Session unavailable", "Check the local app terminal."])));
    if (phase === "clarification") content.append(safety("Application help—not the model's diagnosis: make a new complete request with exact names and, for notes, nonblank text. Earlier requests and scenario references are not planner context."));
    if (state.error) { content.append(safety(`Status: ${state.error.code}. No automatic retry.`, true)); if (state.error.help) content.append(node("pre", "content-preview code-preview", state.error.help)); }
  }
  updateDeadline();
}

function renderResults() {
  const content = $("results-content"); content.replaceChildren();
  const completed = state.results.filter((result) => result.status === "completed").length;
  $("result-count").textContent = completed ? `${completed} ${completed === 1 ? "step" : "steps"} completed` : "No steps completed";
  if (!state.results.length) { const placeholder = node("div", "results-placeholder"); const copy = node("div"); copy.append(node("strong", "", "A clear trail, one step at a time"), node("p", "", "Only observed broker outcomes appear here. A proposal or a manual walkthrough verdict is not an executed action.")); placeholder.append(icon("terminal"), copy); content.append(placeholder); return; }
  for (const result of state.results) {
    const row = node("div", "outcome-row"); row.dataset.status = result.status;
    const copy = node("div", "outcome-body");
    copy.append(node("h3", "", `${result.index}. ${names[result.action.tool] || "Workspace action"} · ${result.status}`));
    const args = result.action.arguments;
    copy.append(node("p", "", args.destination ?? args.path ?? ""));
    if (args.source) copy.append(node("p", "", `From: ${args.source}`));
    if (result.code) copy.append(node("p", "", `Status: ${result.code}`));
    const details = result.details || {};
    if (details.entries) {
      const list = node("ul", "listing");
      for (const entry of details.entries) { const item = node("li"); item.append(icon(entry.type === "directory" ? "folder" : "note"), node("code", "", `${entry.name}${entry.type === "directory" ? "/" : ""}`)); list.append(item); } copy.append(list);
    }
    if (Object.hasOwn(details, "exit_code")) copy.append(node("p", "", `Exit code: ${details.exit_code} · Compute-only sandbox`));
    for (const stream of ["stdout", "stderr"]) if (details[stream]) { copy.append(node("p", "detail-label", stream.toUpperCase()), node("pre", "", details[stream])); }
    if (result.effect_completed && result.status !== "completed") copy.append(node("p", "", "An effect completed but the overall step did not finish cleanly. Inspect before further work."));
    row.append(icon(result.status === "completed" ? "check" : "info"), copy); content.append(row);
  }
}

function render() {
  $("connection-label").textContent = state.connected ? "Connected" : state.phase === "starting" ? "Starting" : "Not connected";
  $("connection-dot").dataset.state = state.connected ? "on" : state.phase === "starting" ? "busy" : "off";
  $("connection-button").textContent = state.connected || state.phase === "starting" ? "Disconnect" : "Connect";
  $("transcript").value = state.transcript || "";
  const listening = state.phase === "recording";
  $("capture-strip").classList.toggle("is-recording", listening);
  $("record-button").classList.toggle("is-cancel", capturing.has(state.phase));
  $("record-button").querySelector("use").setAttribute("href", capturing.has(state.phase) ? "#i-close" : "#i-mic");
  $("capture-strip").classList.toggle("live-progress", ["preparing", "transcribing"].includes(state.phase));
  $("capture-label").textContent = { preparing: "Preparing—stay quiet", recording: "Listening—speak now", transcribing: "Microphone off · transcribing locally", review: "Your transcript is ready", planning: "Microphone off · planning", approval: "Microphone off · review the exact effect", cancelling: "Stopping—wait for cleanup", cancelled: state.working ? "Stopping—wait for cleanup" : state.connected ? "Ready to record again" : "Microphone off", failed: state.working ? "Finishing cleanup" : state.connected && !state.blocked ? "Ready for a new recording" : "Microphone off" }[state.phase] || "A space for your next idea";
  $("capture-description").textContent = listening ? "Pause naturally to finish. Click the microphone button to cancel and discard." : state.phase === "preparing" ? "Do not speak yet. Click the microphone button to cancel and discard." : state.phase === "idle" ? "Click Record, then wait for Listening." : state.phase === "disconnected" ? "Connect first. Recording never starts automatically." : "A new recording always needs another deliberate click.";
  $("transcript-badge").textContent = state.transcript ? "Original · memory only" : "Memory only";
  renderCorrections();
  const stage = ["review"].includes(state.phase) ? 1 : ["planning", "plan", "approval", "executing"].includes(state.phase) ? 2 : terminal.has(state.phase) ? 3 : 0;
  for (const item of document.querySelectorAll("[data-stage]")) { const number = Number(item.dataset.stage); if (number === stage) item.setAttribute("aria-current", "step"); else item.removeAttribute("aria-current"); item.classList.toggle("is-past", number < stage); }
  renderAction(); renderResults();
  if (state.phase !== lastPhase) { announce($("action-badge").textContent); lastPhase = state.phase; }
}
function renderCorrections() {
  $("correction-count").textContent = `${corrections.length} / ${config?.max_corrections || 8} · ${corrections.reduce((sum, value) => sum + Array.from(value).length, 0)} / ${config?.max_correction_chars || 2048} characters`;
  $("correction-notes").replaceChildren(...corrections.map((text, index) => { const value = node("div", "correction-note"); value.append(node("span", "", String(index + 1)), node("p", "", text)); return value; }));
}

$("connection-button").addEventListener("click", () => { if (state.connected || state.phase === "starting") command("disconnect"); else $("connection-dialog").showModal(); });
$("cancel-connect").addEventListener("click", () => $("connection-dialog").close());
$("confirm-connect").addEventListener("click", () => { $("connection-dialog").close(); command("connect"); });
$("record-button").addEventListener("click", () => command(capturing.has(state.phase) ? "cancel" : "record"));
$("new-request").addEventListener("click", () => command("reset"));
$("cancel-request").addEventListener("click", () => command("cancel"));
function addDraftNote() {
  if (state.phase !== "review" || commandBusy || networkLost) return false;
  const text = $("correction-input").value;
  if (!text.trim() || corrections.length >= config.max_corrections || [...corrections, text].reduce((sum, value) => sum + Array.from(value).length, 0) > config.max_correction_chars) { announce("Note not added or sent. Use nonblank text within the eight-note and total character limits."); return false; }
  corrections.push(text); $("correction-input").value = ""; renderCorrections(); syncControls(); announce("Correction note added. Original transcript unchanged; nothing sent yet.");
  return true;
}
$("send-request").addEventListener("click", () => {
  // Sending includes an explicitly typed draft too; never silently drop a correction.
  if ($("correction-input").value.trim() && !addDraftNote()) return;
  command("send", { corrections: [...corrections] });
});
$("correction-form").addEventListener("submit", (event) => { event.preventDefault(); addDraftNote(); });

// This sidebar owns only reference strings and manual notes. It never calls
// command(), changes a request, supplies history, or interprets live outcomes.
let scenarioIndex = 0;
const verdicts = new Map();
const suffix = [...crypto.getRandomValues(new Uint8Array(2))].map((value) => value.toString(16).padStart(2, "0")).join("");
$("scenario-root").value = `Orbit Demo ${suffix}`;
$("scenario-count").textContent = String(SCENARIOS.length);
$("scenario-progress-bar").max = SCENARIOS.length;
let previousGroup, group;
SCENARIOS.forEach((scenario, index) => { if (scenario.group !== previousGroup) { group = node("optgroup"); group.label = scenario.group; $("scenario-picker").append(group); previousGroup = scenario.group; } const option = node("option", "", `${String(index + 1).padStart(2, "0")} · ${scenario.title}`); option.value = String(index); group.append(option); });
function reference(text) { const root = $("scenario-root").value || `Orbit Demo ${suffix}`; return text.replaceAll("Orbit Demo", root).replaceAll("Starlight Lab", `Starlight Lab ${suffix}`).replaceAll("Comet Notes", `Comet Notes ${suffix}`).replaceAll("Pause Lab", `Pause Lab ${suffix}`); }
function renderScenario() {
  const scenario = SCENARIOS[scenarioIndex];
  $("scenario-picker").value = String(scenarioIndex);
  $("scenario-position").textContent = `Scenario ${scenarioIndex + 1} of ${SCENARIOS.length}`;
  const reviewed = [...verdicts.values()].filter((value) => value !== "skipped").length;
  $("scenario-reviewed").textContent = `${reviewed} manually reviewed`; $("scenario-progress-bar").value = reviewed;
  $("previous-scenario").disabled = scenarioIndex === 0; $("next-scenario").disabled = scenarioIndex === SCENARIOS.length - 1;
  const content = $("scenario-body"); content.replaceChildren(node("p", "scenario-group", scenario.group), node("h3", "", scenario.title), node("p", "scenario-description", scenario.description));
  scenario.prompts.forEach((prompt, index) => { const block = node("div", "prompt-block"); block.append(node("p", "", scenario.prompts.length > 1 ? `READ ALOUD · SEPARATE REQUEST ${index + 1} OF ${scenario.prompts.length}` : "READ THIS ALOUD"), node("blockquote", "", reference(prompt))); content.append(block); });
  const prerequisite = node("div", "scenario-instruction"); prerequisite.append(node("h4", "", "Before you begin"), node("p", "", reference(scenario.prerequisites.replace("this preview has not checked it", "the app has not checked this suggestion"))));
  const checks = node("ul", "scenario-checks"); for (const check of scenario.checks) checks.append(node("li", "", check)); content.append(prerequisite, node("p", "checks-heading", "WHAT TO LOOK FOR"), checks);
  const verdict = verdicts.get(scenario.id); for (const control of document.querySelectorAll("[data-verdict]")) control.setAttribute("aria-pressed", String(control.dataset.verdict === verdict));
  $("verdict-status").textContent = { worked: "You marked Worked · not a broker result", failed: "You marked Didn't work · not a broker result", unsure: "You marked Unsure", skipped: "Skipped · return anytime" }[verdict] || "Not reviewed yet";
}
function setScenariosOpen(open) {
  const surface = $("main-surface");
  const scrollTop = $("scenario-panel").hidden ? window.scrollY : surface.scrollTop;
  $("scenario-panel").hidden = !open;
  $("workspace-layout").classList.toggle("has-scenarios", open);
  $("open-scenarios").setAttribute("aria-expanded", String(open));
  if (open) {
    renderScenario();
    window.scrollTo(0, 0);
    surface.scrollTop = scrollTop;
    $("close-scenarios").focus({ preventScroll: true });
  } else {
    window.scrollTo(0, scrollTop);
    $("open-scenarios").focus({ preventScroll: true });
  }
}
$("open-scenarios").addEventListener("click", () => setScenariosOpen($("scenario-panel").hidden));
$("close-scenarios").addEventListener("click", () => setScenariosOpen(false));
function navigateScenario(index) {
  scenarioIndex = index;
  renderScenario();
  $("scenario-scroll").scrollTop = Math.max(0, $("scenario-body").offsetTop - 12);
}
$("scenario-picker").addEventListener("change", (event) => navigateScenario(Number(event.target.value)));
$("scenario-root").addEventListener("input", renderScenario);
$("previous-scenario").addEventListener("click", () => navigateScenario(Math.max(0, scenarioIndex - 1)));
$("next-scenario").addEventListener("click", () => navigateScenario(Math.min(SCENARIOS.length - 1, scenarioIndex + 1)));
$("skip-scenario").addEventListener("click", () => { if (!verdicts.has(SCENARIOS[scenarioIndex].id)) verdicts.set(SCENARIOS[scenarioIndex].id, "skipped"); navigateScenario(Math.min(SCENARIOS.length - 1, scenarioIndex + 1)); });
for (const control of document.querySelectorAll("[data-verdict]")) control.addEventListener("click", () => { const id = SCENARIOS[scenarioIndex].id; if (verdicts.get(id) === control.dataset.verdict) verdicts.delete(id); else verdicts.set(id, control.dataset.verdict); renderScenario(); });

async function updatePresence(hidden) {
  backgrounded = hidden;
  if (!config || !consentGranted || suspended) { syncControls(); return; }
  const sequence = ++presenceSequence;
  presencePending++;
  syncControls();
  try {
    const observed = (await api(hidden ? "/api/pause" : "/api/resume", { sequence }, { keepalive: hidden })).state;
    apply(observed);
    if (!hidden && observed.reclaimed) notice = "Welcome back. The previous request stopped and its lease was released. Choose Record for a fresh request; no recording, submission or approval was resumed.";
  } catch (error) {
    if (!hidden && error.code === "not_connected") consentGranted = false;
    if (!hidden && error.code !== "busy") {
      networkLost = !error.code;
      notice = error.code ? errorText[error.code] : "Could not confirm the returning page's state. Controls stay paused until status is available; no work is retried.";
    }
  } finally { presencePending--; syncControls(); }
}
function leavePage(event) {
  if (event?.persisted) { updatePresence(true); return; }
  if (!config || !state.request_id || suspended || state.phase === "occupied") return;
  suspended = true;
  consentGranted = false;
  syncControls();
  // Best-effort only. The server also owns a bounded heartbeat lease.
  fetch("/api/disconnect", { method: "POST", headers: { "Content-Type": "application/json", "x-macintent-token": config.token, "x-macintent-client": config.client_id }, body: JSON.stringify({ request_id: state.request_id }), keepalive: true, credentials: "omit" }).catch(() => {});
}
window.addEventListener("pagehide", leavePage);
function resumePage() {
  if (!suspended) updatePresence(document.visibilityState === "hidden");
}
window.addEventListener("pageshow", resumePage);
document.addEventListener("visibilitychange", () => updatePresence(document.visibilityState === "hidden"));
document.addEventListener("keydown", (event) => {
  if (event.key !== "Escape") return;
  if (!$("scenario-panel").hidden) { setScenariosOpen(false); event.preventDefault(); }
  else if (!$("connection-dialog").open && !["INPUT", "TEXTAREA"].includes(document.activeElement?.tagName ?? "") && active.has(state.phase)) command("cancel");
});

render(); syncControls();
try {
  config = await api("/api/session");
  $("cloud-disclosure").textContent = config.disclosure;
  $("correction-input").maxLength = config.max_correction_chars;
  apply((await api("/api/state")).state);
  $("connection-dialog").showModal();
} catch { networkLost = true; notice = "Could not reach the MacIntent server. Start ./scripts/macintent and reload this page. No microphone or cloud request started."; }
syncControls();
poll();
