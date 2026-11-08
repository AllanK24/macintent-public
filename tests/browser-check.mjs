// Browser wiring test only: visit the live server's PUBLIC STATIC ASSETS only.
// A pre-document fetch stub intercepts every application API call. Deliberately
// invalid real-server credentials fail closed if the stub is ever bypassed.
// No microphone, Groq, live workspace, broker or real Python effect is used.
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { createHash } from "node:crypto";
import { mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { once } from "node:events";
import { SCENARIOS } from "../web/scenarios.mjs";

assert.equal(process.env.MACINTENT_LAUNCHER_ACTIVE, "1", "Use scripts/dev");
assert.equal(process.cwd(), process.env.MACINTENT_ROOT);
const root = process.cwd();
const sidebarOnly = process.argv.includes("--sidebar-only");
const cancelOnly = process.argv.includes("--cancel-only");
const recoveryOnly = process.argv.includes("--recovery-only");
const offline = process.argv.includes("--offline");
const portArgument = process.argv.find((value) => /^--port=\d+$/.test(value));
const port = portArgument ? Number(portArgument.slice(7)) : 8093;
assert.ok(Number.isInteger(port) && port >= 1024 && port <= 65535);
const profile = mkdtempSync(join(root, ".cache/tmp/macintent-browser-"));
const screenshots = mkdtempSync(join(root, ".local/reports/macintent-ui-"));
let networkRequests = 0, leakedAPIRequests = 0;
const networkFailures = [];
const origin = `http://127.0.0.1:${port}`;
const browser = spawn("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", [
  "--headless", "--disable-gpu", "--window-size=1440,1100", "--remote-debugging-pipe",
  `--user-data-dir=${profile}`, "--no-first-run", "--no-default-browser-check", "--no-proxy-server",
  "--disable-background-networking", "--disable-component-update", "--disable-sync",
  "--disable-default-apps", "--disable-domain-reliability", "--disable-breakpad", "--disable-extensions",
  "--disable-features=MediaRouter,OptimizationHints,AutofillServerCommunication,Translate",
  "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1", "about:blank",
], { detached: true, stdio: ["ignore", "ignore", "ignore", "pipe", "pipe"] });

let sequence = 0, buffer = "", session;
const pending = new Map(), listeners = new Map();
browser.stdio[4].setEncoding("utf8");
browser.stdio[4].on("data", (chunk) => {
  buffer += chunk;
  let boundary;
  while ((boundary = buffer.indexOf("\0")) !== -1) {
    const message = JSON.parse(buffer.slice(0, boundary)); buffer = buffer.slice(boundary + 1);
    if (pending.has(message.id)) {
      const entry = pending.get(message.id); clearTimeout(entry.timer); pending.delete(message.id);
      if (message.error) entry.reject(new Error("Browser protocol failed")); else entry.resolve(message.result);
    } else if (message.sessionId === session && listeners.has(message.method)) listeners.get(message.method)(message.params);
  }
});
function call(method, params = {}, sessionId) {
  return new Promise((resolve, reject) => {
    const id = ++sequence;
    const timer = setTimeout(() => { pending.delete(id); reject(new Error(`Browser command timeout: ${method}`)); }, 15000);
    pending.set(id, { resolve, reject, timer });
    browser.stdio[3].write(JSON.stringify({ id, method, params, ...(sessionId ? { sessionId } : {}) }) + "\0");
  });
}
const page = (method, params) => call(method, params, session);
async function evaluate(expression) { const value = await page("Runtime.evaluate", { expression, returnByValue: true, awaitPromise: true }); assert.ok(!value.exceptionDetails, "No browser JavaScript exception"); return value.result.value; }
async function waitFor(expression) { await evaluate(`new Promise((resolve,reject)=>{const deadline=Date.now()+5000;function check(){if(${expression})resolve(true);else if(Date.now()>deadline)reject(new Error('Expected UI state not reached'));else setTimeout(check,30)}check()})`); }
async function click(selector) { await waitFor(`document.querySelector(${JSON.stringify(selector)}) && !document.querySelector(${JSON.stringify(selector)}).disabled`); await evaluate(`document.querySelector(${JSON.stringify(selector)}).click()`); }
async function pointerClick(selector) {
  await waitFor(`document.querySelector(${JSON.stringify(selector)}) && !document.querySelector(${JSON.stringify(selector)}).disabled`);
  await evaluate(`document.querySelector(${JSON.stringify(selector)}).scrollIntoView({block:'center',inline:'nearest'})`);
  const point = await evaluate(`(() => {const e=document.querySelector(${JSON.stringify(selector)}),r=e.getBoundingClientRect(),x=r.x+r.width/2,y=r.y+r.height/2;return {x,y,reachable:e.contains(document.elementFromPoint(x,y))}})()`);
  assert.ok(point.reachable, `Visible pointer target: ${selector}`);
  await page("Input.dispatchMouseEvent", { type: "mousePressed", x: point.x, y: point.y, button: "left", clickCount: 1 });
  await page("Input.dispatchMouseEvent", { type: "mouseReleased", x: point.x, y: point.y, button: "left", clickCount: 1 });
}
async function screenshot(name) { await evaluate("new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(()=>r(true))))"); const result = await page("Page.captureScreenshot", { format: "png", captureBeyondViewport: false }); writeFileSync(join(screenshots, name), Buffer.from(result.data, "base64"), { mode: 0o600, flag: "wx" }); }

const original = "A distinct synthetic browser recording.";
const code = "print(15)\n";
const sourceDigest = createHash("sha256").update(code).digest("hex");
const actions = [
  { tool: "create_directory", arguments: { path: "demo/workspace/Synthetic Orbit" } },
  { tool: "create_note", arguments: { path: "demo/workspace/Synthetic Orbit/idea.txt", content: "Literal <b>idea</b>." } },
];
let mode = "plan", nonce = 0, revision = 0, request = 0, captureFlow = "instant", consent = false, presence = 0;
let model = { phase: "disconnected", connected: false, blocked: false, backgrounded: false, working: false, request_id: "", transcript: "", steps: [], results: [], pending: null, error: null, explanation: "", step: 0 };
const apiCalls = [], errors = [], fixtureErrors = [];
const snapshot = () => ({ ...structuredClone(model), version: ++revision });
function approval(action, kind = "action") {
  return { id: `fixture-decision-${++nonce}`, kind, action, expires_at: Math.floor(Date.now() / 1000) + 60,
    ...(action?.tool === "run_python_script" ? { source_sha256: sourceDigest } : {}) };
}
function fakeAPI(path, method, body) {
  apiCalls.push({ path, method, body });
  if (path === "/api/session") return { token: "NOT-A-REAL-SERVER-TOKEN", client_id: "a".repeat(32), disclosure: "Mocked browser fixture only. Submitted text goes to Groq in the real application; no connection is made by this check.", model: "qwen/qwen3.8-27b", max_command: 2048, max_corrections: 8, max_correction_chars: 2048 };
  if (path === "/api/state") return { ok: true, state: snapshot() };
  if (path === "/api/connect") { assert.deepEqual(body, { consent: true }); consent = true; presence = 0; model = { ...model, phase: "idle", connected: true, backgrounded: false, request_id: `fixture-${++request}` }; }
  else if (path === "/api/pause" || path === "/api/resume") {
    assert.deepEqual(Object.keys(body), ["sequence"]);
    assert.equal(Number.isSafeInteger(body.sequence) && body.sequence > 0, true);
    if (model.blocked) return { ok: false, error: "session_blocked" };
    if (!consent) return { ok: false, error: "not_connected" };
    let reclaimed = false;
    if (body.sequence > presence) {
      presence = body.sequence;
      if (path === "/api/pause") {
        model = { ...model, backgrounded: true, ...(model.working ? { phase: "cancelling", pending: null, transcript: "" } : {}) };
      } else {
        if (!model.connected && !model.working) {
          reclaimed = true;
          model = { ...model, phase: "idle", connected: true, request_id: `fixture-${++request}`, transcript: "", steps: [], results: [], error: null };
        }
        model = { ...model, backgrounded: false };
      }
    }
    return { ok: true, state: { ...snapshot(), reclaimed } };
  }
  else if (path === "/api/reset") {
    assert.equal(model.connected && !model.working && !model.blocked, true);
    model = { ...model, phase: "idle", request_id: `fixture-${++request}`, transcript: "", steps: [], results: [], pending: null, step: 0, error: null };
  } else if (path === "/api/record") {
    assert.equal(model.connected && !model.working && !model.blocked && !model.backgrounded && ["idle", "cancelled", "failed"].includes(model.phase), true);
    if (["cancelled", "failed"].includes(model.phase)) model = { ...model, request_id: `fixture-${++request}`, steps: [], results: [], error: null };
    model = { ...model, phase: captureFlow === "staged" ? "preparing" : "review", working: captureFlow === "staged", transcript: captureFlow === "staged" ? "" : original };
  }
  else if (path === "/api/send") {
    assert.deepEqual(Object.keys(body).sort(), ["corrections", "request_id"]);
    assert.equal(model.phase, "review");
    if (mode === "plan") model = { ...model, phase: "plan", working: true, steps: structuredClone(actions), pending: approval(null, "plan") };
    else { const action = mode === "run" ? { tool: "run_python_script", arguments: { path: "demo/workspace/calc.py", code } } : { tool: "delete_file", arguments: { path: "demo/workspace/disposable.txt" } }; model = { ...model, phase: "approval", working: true, steps: [action], step: 1, pending: approval(action) }; }
  } else if (path === "/api/continue") { assert.equal(body.approval_id, model.pending.id); assert.equal(model.pending.kind, "plan"); model = { ...model, phase: "approval", step: 1, pending: approval(actions[0]) }; }
  else if (path === "/api/decide") {
    assert.deepEqual(Object.keys(body).sort(), ["approval_id", "approve", "request_id"]);
    assert.equal(body.approval_id, model.pending.id);
    const status = mode === "unknown" ? "unknown" : body.approve ? "completed" : "declined";
    const action = structuredClone(model.pending.action); delete action.arguments.code;
    const details = mode === "run" ? { path: action.arguments.path, exit_code: 0, stdout: "15\n<img src=x onerror=alert(1)>", stderr: "" } : {};
    model.results.push({ index: model.step, action, status, details, code: status === "unknown" ? "outcome_unknown" : null, effect_completed: body.approve && status !== "unknown" });
    if (mode === "plan" && body.approve && model.step === 1) model = { ...model, step: 2, pending: approval(actions[1]) };
    else model = { ...model, phase: status, transcript: "", pending: null, steps: [], working: false, blocked: status === "unknown", connected: status !== "unknown" };
  } else if (path === "/api/cancel") model = { ...model, phase: "cancelling", pending: null, working: true, transcript: "" };
  else if (path === "/api/disconnect") { consent = false; model = { ...model, phase: "disconnected", connected: false, backgrounded: false, pending: null, working: false, transcript: "", request_id: "" }; }
  else throw new Error("Unexpected API route");
  return { ok: true, state: snapshot() };
}
listeners.set("Runtime.exceptionThrown", () => errors.push("javascript_exception"));
listeners.set("Log.entryAdded", ({ entry }) => { if (entry.level === "error") errors.push("browser_error"); });
listeners.set("Network.requestWillBeSent", ({ request }) => { networkRequests++; if (new URL(request.url).pathname.startsWith("/api/")) leakedAPIRequests++; });
listeners.set("Network.loadingFailed", ({ errorText }) => networkFailures.push(/^net::ERR_[A-Z_]+$/.test(errorText) ? errorText : "network_failed"));
const calls = () => evaluate("globalThis.__macintentFixture.calls()");
const fixtureSource = `(() => {
  const assert = {equal(a,b){if(a!==b)throw new Error('Fixture mismatch')},deepEqual(a,b){if(JSON.stringify(a)!==JSON.stringify(b))throw new Error('Fixture shape mismatch')}};
  const original=${JSON.stringify(original)}, code=${JSON.stringify(code)}, sourceDigest=${JSON.stringify(sourceDigest)}, actions=${JSON.stringify(actions)};
  let mode='plan', nonce=0, revision=0, request=0, captureFlow='instant', consent=false, presence=0, hidden=false, model=${JSON.stringify(model)};
  Object.defineProperty(document,'visibilityState',{configurable:true,get:()=>hidden?'hidden':'visible'});
  const apiCalls=[],fixtureErrors=[];
  const snapshot=${snapshot.toString()};
  ${approval.toString()}
  ${fakeAPI.toString()}
  globalThis.__macintentFixture={
    calls:()=>structuredClone(apiCalls),errors:()=>[...fixtureErrors],
    setMode(value){if(!['plan','run','unknown'].includes(value))throw new Error('Fixture mode');mode=value},
    stagedCapture(){captureFlow='staged'},
    visibility(value){hidden=value;document.dispatchEvent(new Event('visibilitychange'))},
    expireLease(){model={...model,phase:'disconnected',connected:false,working:false,backgrounded:false,pending:null,transcript:'',request_id:'expired-fixture'}},
    failRequest(code){model={...model,phase:'failed',working:true,pending:null,transcript:'',error:{code,help:null}}},
    finishFailure(fault=false){model={...model,phase:fault?'unknown':'failed',working:false,connected:!fault,blocked:fault,error:fault?{code:'cleanup_failed',help:null}:model.error}},
    finishCancel(fault=false){
      if(model.phase!=='cancelling')throw new Error('Fixture cleanup phase');
      model={...model,phase:fault?'unknown':'cancelled',connected:!fault,blocked:!!fault,working:false,steps:[],error:fault?{code:'cleanup_failed',help:null}:null};
    },
    captureStage(value){
      if(!['preparing','recording','transcribing'].includes(model.phase)||!['recording','transcribing','review','limit'].includes(value))throw new Error('Fixture capture phase');
      model={...model,phase:value==='limit'?'failed':value,working:['recording','transcribing'].includes(value),transcript:value==='review'?original:'',error:value==='limit'?{code:'recording_limit',help:null}:null};
    }
  };
  globalThis.fetch=async(input,init={})=>{
    try {
      const url=new URL(input,${JSON.stringify(origin)});
      if(url.origin!==${JSON.stringify(origin)}||!url.pathname.startsWith('/api/'))throw new Error('Fixture forbids this fetch');
      const headers=new Headers(init.headers);
      if(url.pathname!=='/api/session'){assert.equal(headers.get('x-macintent-token'),'NOT-A-REAL-SERVER-TOKEN');assert.equal(headers.get('x-macintent-client'),'${"a".repeat(32)}')}
      const data=fakeAPI(url.pathname,init.method||'GET',init.body?JSON.parse(init.body):undefined);
      return new Response(JSON.stringify(data),{status:200,headers:{'Content-Type':'application/json'}});
    } catch {fixtureErrors.push('fixture_contract_error');throw new Error('Mocked API fixture failed')}
  };
})();`;

async function sidebarCheck() {
  await evaluate("globalThis.__macintentFixture.stagedCapture()");
  const before = (await calls()).filter((value) => value.method === "POST").length;
  await pointerClick("#open-scenarios");
  assert.equal(await evaluate("document.getElementById('open-scenarios').getAttribute('aria-expanded')"), "true");
  assert.equal((await calls()).filter((value) => value.method === "POST").length, before);
  await page("Emulation.setDeviceMetricsOverride", { width: 1200, height: 900, deviceScaleFactor: 1, mobile: false });
  assert.equal(await evaluate("(() => {const a=document.getElementById('main-surface').getBoundingClientRect(),b=document.getElementById('scenario-panel').getBoundingClientRect();return a.right<=b.left+1 && b.right<=innerWidth+1 && document.documentElement.scrollWidth<=innerWidth})()"), true);
  await pointerClick("#record-button");
  await waitFor("document.getElementById('sidebar-capture-label').textContent==='Preparing—stay quiet'");
  assert.equal(await evaluate("document.getElementById('transcript').value"), "");
  await evaluate("globalThis.__macintentFixture.captureStage('recording')");
  await waitFor("document.getElementById('sidebar-capture-label').textContent==='Listening—speak now'");
  assert.equal(await evaluate("document.getElementById('record-button').disabled"), false);
  assert.equal(await evaluate("document.getElementById('record-button').getAttribute('aria-label')"), "Cancel recording and discard audio");
  const recordingPosts = (await calls()).filter((value) => value.method === "POST").length;
  const panelBefore = await evaluate("document.getElementById('scenario-panel').getBoundingClientRect().top");
  await evaluate("document.getElementById('main-surface').scrollTop+=150");
  assert.equal(await evaluate("document.getElementById('scenario-panel').getBoundingClientRect().top"), panelBefore);
  await pointerClick("#next-scenario");
  assert.equal((await calls()).filter((value) => value.method === "POST").length, recordingPosts);
  assert.equal(await evaluate("(() => {const cue=document.getElementById('sidebar-capture-status').getBoundingClientRect(),scroll=document.getElementById('scenario-scroll').getBoundingClientRect(),prompt=document.querySelector('#scenario-body .prompt-block').getBoundingClientRect();return cue.bottom<=scroll.top+1 && prompt.top>=scroll.top && prompt.top<scroll.bottom})()"), true);
  await screenshot("sidebar-listening.png");
  await evaluate("globalThis.__macintentFixture.captureStage('transcribing')");
  await waitFor("document.getElementById('sidebar-capture-label').textContent==='Microphone off · transcribing'");
  await evaluate("globalThis.__macintentFixture.captureStage('review')");
  await waitFor("document.getElementById('sidebar-capture-label').textContent==='Transcript ready · microphone off'");
  assert.equal(await evaluate("document.getElementById('transcript').value"), original);
  assert.equal(await evaluate("document.getElementById('scenario-panel').hidden"), false);
  assert.equal((await calls()).filter((value) => value.path === "/api/record").length, 1);
  assert.equal((await calls()).filter((value) => value.path === "/api/send").length, 0);

  await pointerClick("#new-request"); await pointerClick("#record-button");
  await waitFor("document.getElementById('sidebar-capture-label').textContent==='Preparing—stay quiet'");
  await evaluate("globalThis.__macintentFixture.captureStage('recording')");
  await waitFor("document.getElementById('sidebar-capture-label').textContent==='Listening—speak now'");
  await pointerClick("#record-button");
  await waitFor("document.getElementById('action-badge').textContent==='Stopping'");
  assert.equal(await evaluate("document.getElementById('record-button').disabled && document.getElementById('new-request').disabled"), true);
  await evaluate("globalThis.__macintentFixture.finishCancel()");
  await waitFor("document.getElementById('action-badge').textContent==='Cancelled'");
  assert.equal((await calls()).filter((value) => value.path === "/api/cancel").length, 1);
  assert.equal((await calls()).filter((value) => value.path === "/api/record").length, 2);

  assert.equal(await evaluate("document.getElementById('new-request').disabled"), false);
  await pointerClick("#record-button");
  assert.equal((await calls()).filter((value) => value.path === "/api/connect").length, 1);
  await waitFor("document.getElementById('sidebar-capture-label').textContent==='Preparing—stay quiet'");
  await evaluate("globalThis.__macintentFixture.captureStage('recording')");
  await waitFor("document.getElementById('sidebar-capture-label').textContent==='Listening—speak now'");
  await evaluate("globalThis.__macintentFixture.captureStage('limit')");
  await waitFor("document.getElementById('sidebar-capture-label').textContent==='Recording discarded at the limit'");
  assert.equal(await evaluate("document.getElementById('capture-strip').classList.contains('is-recording')"), false);
  assert.equal((await calls()).filter((value) => value.path === "/api/send").length, 0);
  await screenshot("sidebar-limit.png");
  await page("Emulation.setDeviceMetricsOverride", { width: 390, height: 844, deviceScaleFactor: 1, mobile: false });
  assert.equal(await evaluate("(() => {const a=document.getElementById('main-surface').getBoundingClientRect(),b=document.getElementById('scenario-panel').getBoundingClientRect();return a.bottom<=b.top+1 && b.bottom<=innerHeight+1 && document.documentElement.scrollWidth<=innerWidth})()"), true);
  await screenshot("sidebar-mobile.png");
  await page("Input.dispatchKeyEvent", { type: "keyDown", key: "Escape", code: "Escape", windowsVirtualKeyCode: 27 });
  await page("Input.dispatchKeyEvent", { type: "keyUp", key: "Escape", code: "Escape", windowsVirtualKeyCode: 27 });
  await waitFor("document.getElementById('scenario-panel').hidden");
  assert.equal(await evaluate("document.getElementById('open-scenarios').getAttribute('aria-expanded')"), "false");
}

async function cancelCheck() {
  await evaluate("globalThis.__macintentFixture.stagedCapture()");
  async function finishCancel() {
    await waitFor("document.getElementById('action-badge').textContent==='Stopping'");
    assert.equal(await evaluate("document.getElementById('record-button').disabled && document.getElementById('new-request').disabled && document.getElementById('send-request').disabled"), true);
    assert.equal(await evaluate("document.getElementById('transcript').value"), "");
    await evaluate("globalThis.__macintentFixture.finishCancel()");
    await waitFor("document.getElementById('capture-label').textContent==='Ready to record again'");
    assert.equal(await evaluate("document.getElementById('record-button').disabled || document.getElementById('new-request').disabled"), false);
    assert.match(await evaluate("document.getElementById('action-content').textContent"), /no refresh or reconnect/);
    assert.equal(await evaluate("document.getElementById('correction-input').value"), "");
  }
  for (const stage of ["preparing", "recording", "transcribing", "review"]) {
    await pointerClick("#record-button");
    await waitFor("document.getElementById('action-badge').textContent==='Mic preparing'");
    if (stage !== "preparing") {
      await evaluate(`globalThis.__macintentFixture.captureStage(${JSON.stringify(stage)})`);
      await waitFor(`document.getElementById('sidebar-capture-status').dataset.phase===${JSON.stringify(stage)}`);
    }
    if (stage === "review") await evaluate("document.getElementById('correction-input').value='Synthetic discarded note';document.getElementById('correction-form').requestSubmit()");
    await pointerClick(["preparing", "recording"].includes(stage) ? "#record-button" : "#cancel-request");
    await finishCancel();
  }
  await pointerClick("#new-request");
  for (const stage of ["plan", "approval"]) {
    await pointerClick("#record-button");
    await waitFor("document.getElementById('action-badge').textContent==='Mic preparing'");
    await evaluate("globalThis.__macintentFixture.captureStage('review')");
    await pointerClick("#send-request");
    await waitFor("document.getElementById('action-badge').textContent==='Review plan'");
    if (stage === "approval") {
      await pointerClick("#action-content .button-primary");
      await waitFor("document.getElementById('action-badge').textContent==='Awaiting approval'");
    }
    assert.match(await evaluate("document.querySelector('#action-content .text-button').textContent"), /Cancel (request|workflow)/);
    await pointerClick("#action-content .text-button");
    await finishCancel();
    assert.equal(await evaluate("document.querySelector('#action-content .approval-controls')"), null);
  }
  assert.equal((await calls()).filter((value) => value.path === "/api/connect").length, 1);
  assert.equal((await calls()).filter((value) => value.path === "/api/session").length, 1);
  assert.equal((await calls()).filter((value) => value.path === "/api/decide").length, 0);
  // Disconnect remains separate and cannot leave Record enabled.
  await pointerClick("#connection-button");
  await waitFor("document.getElementById('connection-label').textContent==='Not connected'");
  assert.equal(await evaluate("document.getElementById('record-button').disabled && document.getElementById('new-request').disabled"), true);
  await pointerClick("#connection-button"); await click("#confirm-connect");
  await pointerClick("#record-button");
  await waitFor("document.getElementById('action-badge').textContent==='Mic preparing'");
  await pointerClick("#record-button");
  await waitFor("document.getElementById('action-badge').textContent==='Stopping'");
  await evaluate("globalThis.__macintentFixture.finishCancel(true)");
  await waitFor("document.getElementById('action-badge').textContent==='Stop & inspect'");
  assert.equal(await evaluate("document.getElementById('record-button').disabled && document.getElementById('new-request').disabled && document.getElementById('connection-button').disabled"), true);
}

async function recoveryCheck() {
  const posts = async (path) => (await calls()).filter((call) => call.method === "POST" && (!path || call.path === path));
  await evaluate("globalThis.__macintentFixture.failRequest('invalid_output')");
  await waitFor("document.getElementById('capture-label').textContent==='Finishing cleanup'");
  assert.equal(await evaluate("document.getElementById('record-button').disabled && document.getElementById('new-request').disabled"), true);
  await evaluate("globalThis.__macintentFixture.finishFailure()");
  await waitFor("!document.getElementById('record-button').disabled && !document.getElementById('new-request').disabled");
  await pointerClick("#record-button");
  await waitFor("document.getElementById('transcript').value.length>0");
  await evaluate("globalThis.__macintentFixture.failRequest('transcription_failed');globalThis.__macintentFixture.finishFailure()");
  await waitFor("!document.getElementById('new-request').disabled");
  await pointerClick("#new-request");
  const beforeSwitch = (await posts()).length;
  await evaluate("window.dispatchEvent(new Event('blur'));window.dispatchEvent(new Event('focus'))");
  assert.equal((await posts()).length, beforeSwitch, "App focus changes do not disconnect");
  await evaluate("globalThis.__macintentFixture.visibility(true)");
  await waitFor("globalThis.__macintentFixture.calls().some(c=>c.path==='/api/pause')");
  await evaluate("globalThis.__macintentFixture.visibility(false)");
  await waitFor("!document.getElementById('record-button').disabled");
  assert.equal((await posts("/api/disconnect")).length, 0);
  assert.equal((await posts("/api/connect")).length, 1);

  // Long background suspension releases runtime ownership, but return only
  // reclaims this page's consent; it must not Record/Send/approve automatically.
  const records = (await posts("/api/record")).length;
  await evaluate("globalThis.__macintentFixture.visibility(true)");
  await waitFor("document.getElementById('record-button').disabled");
  await evaluate("globalThis.__macintentFixture.expireLease()");
  assert.equal(await evaluate("document.getElementById('connection-label').textContent"), "Connected", "Sleeping UI has not observed server lease expiry");
  await evaluate("globalThis.__macintentFixture.visibility(false)");
  await waitFor("!document.getElementById('record-button').disabled");
  await waitFor("document.getElementById('session-notice').textContent.includes('lease was released')");
  assert.equal((await posts("/api/record")).length, records);
  await pointerClick("#record-button");
  await waitFor("document.getElementById('transcript').value.length>0");
  const transcript = await evaluate("document.getElementById('transcript').value");
  await evaluate("globalThis.__macintentFixture.visibility(true);globalThis.__macintentFixture.visibility(false);globalThis.__macintentFixture.visibility(true);globalThis.__macintentFixture.visibility(false)");
  await waitFor("!document.getElementById('new-request').disabled");
  assert.equal(await evaluate("document.getElementById('transcript').value"), transcript);
  await pointerClick("#send-request");
  await waitFor("document.getElementById('action-badge').textContent==='Review plan'");
  await evaluate("globalThis.__macintentFixture.visibility(true)");
  await waitFor("document.getElementById('action-badge').textContent==='Stopping'");
  await evaluate("globalThis.__macintentFixture.finishCancel();globalThis.__macintentFixture.visibility(false)");
  await waitFor("!document.getElementById('record-button').disabled");
  assert.equal(await evaluate("document.querySelector('#action-content .approval-controls')"), null);
  assert.equal((await posts("/api/connect")).length, 1);
  assert.equal((await posts("/api/disconnect")).length, 0);
  assert.equal((await posts("/api/decide")).length, 0);
  await pointerClick("#record-button");
  await evaluate("globalThis.__macintentFixture.failRequest('invalid_output');globalThis.__macintentFixture.finishFailure(true)");
  await waitFor("document.getElementById('action-badge').textContent==='Stop & inspect'");
  await evaluate("globalThis.__macintentFixture.visibility(true);globalThis.__macintentFixture.visibility(false)");
  await waitFor("document.getElementById('record-button').disabled && document.getElementById('new-request').disabled && document.getElementById('connection-button').disabled");
}

try {
  const target = await call("Target.createTarget", { url: "about:blank" });
  session = (await call("Target.attachToTarget", { targetId: target.targetId, flatten: true })).sessionId;
  await page("Page.enable"); await page("Runtime.enable"); await page("Log.enable"); await page("Network.enable");
  await page("Emulation.setDeviceMetricsOverride", { width: 1440, height: 900, deviceScaleFactor: 1, mobile: false });
  if (offline) {
    // Layout/control regression only, not a server/CSP acceptance test. Load the
    // actual public DOM/CSS/JS in a fresh blank frame without HTTP. Inline asset
    // substitution is confined to this test; production files/CSP are untouched.
    const stars = readFileSync(new URL("../web/stars.svg", import.meta.url), "utf8");
    const css = readFileSync(new URL("../web/styles.css", import.meta.url), "utf8").replace('url("/stars.svg")', `url("data:image/svg+xml,${encodeURIComponent(stars)}")`) + readFileSync(new URL("../web/live.css", import.meta.url), "utf8");
    const html = readFileSync(new URL("../web/index.html", import.meta.url), "utf8")
      .replace(/<meta http-equiv="Content-Security-Policy"[^>]*>/, "")
      .replace(/<link\b[^>]*>/g, "")
      .replace(/<script type="module" src="\/app.mjs"><\/script>/, "")
      .replace("</head>", `<style>${css}</style></head>`);
    const frame = (await page("Page.getFrameTree")).frameTree.frame.id;
    await page("Page.setDocumentContent", { frameId: frame, html });
    await evaluate(fixtureSource);
    const app = readFileSync(new URL("../web/app.mjs", import.meta.url), "utf8").replace(/^import \{ SCENARIOS \} from "\/scenarios.mjs";\s*/, "");
    await evaluate(`(async()=>{const SCENARIOS=${JSON.stringify(SCENARIOS)};\n${app}\n})()`);
  } else {
    await page("Page.addScriptToEvaluateOnNewDocument", { source: fixtureSource });
    await page("Page.navigate", { url: `${origin}/` });
  }
  await waitFor("document.getElementById('connection-dialog')?.open");
  assert.equal(await evaluate("document.title"), "MacIntent");
  assert.equal((await calls()).filter((value) => value.method === "POST").length, 0);
  await click("#confirm-connect");
  if (recoveryOnly) {
    await recoveryCheck();
  } else if (cancelOnly) {
    await cancelCheck();
  } else if (sidebarOnly) {
    await sidebarCheck();
  } else {
  await click("#record-button");
  await waitFor("document.getElementById('transcript').value.length > 0");
  assert.equal(await evaluate("document.getElementById('transcript').readOnly"), true);
  await evaluate("document.getElementById('correction-input').value='<b>Synthetic correction</b>';document.getElementById('correction-form').requestSubmit()");
  assert.equal(await evaluate("document.querySelectorAll('#correction-notes b').length"), 0);
  assert.equal(await evaluate("document.getElementById('transcript').value"), original);
  await click("#send-request");
  await waitFor("document.querySelectorAll('.plan-step').length===2");
  const sent = (await calls()).find((value) => value.path === "/api/send");
  assert.deepEqual(sent.body.corrections, ["<b>Synthetic correction</b>"]);
  assert.equal(await evaluate("document.querySelectorAll('#action-content b').length"), 0);
  const mainBefore = await evaluate("document.getElementById('action-content').textContent");
  const postsBefore = (await calls()).filter((value) => value.method === "POST").length;
  await click("#open-scenarios");
  await click("[data-verdict='worked']"); await click("#next-scenario");
  await evaluate("document.getElementById('scenario-root').value='Changed reference only';document.getElementById('scenario-root').dispatchEvent(new Event('input'))");
  assert.equal((await calls()).filter((value) => value.method === "POST").length, postsBefore);
  assert.equal(await evaluate("document.getElementById('action-content').textContent"), mainBefore);
  await screenshot("scenario-drawer.png");
  await click("#close-scenarios");
  await waitFor("document.getElementById('scenario-panel').hidden");
  await click("#action-content .approval-controls .button-primary");
  await waitFor("document.getElementById('action-badge').textContent==='Awaiting approval'");
  await screenshot("approval-desktop.png");
  await click("#action-content .approval-controls .button-primary");
  await waitFor("document.querySelector('.step-context')?.textContent.includes('STEP 2')");
  await click("#action-content .approval-controls .button-secondary");
  await waitFor("document.getElementById('action-badge').textContent==='Declined'");
  assert.equal(await evaluate("document.querySelectorAll('.outcome-row').length"), 2);
  assert.equal((await calls()).filter((value) => value.path === "/api/decide").length, 2);

  await evaluate("globalThis.__macintentFixture.setMode('run')");
  await click("#new-request"); await click("#record-button");
  await evaluate("document.getElementById('correction-input').value='Synthetic draft correction'");
  await click("#send-request");
  assert.deepEqual((await calls()).filter((value) => value.path === "/api/send").at(-1).body.corrections, ["Synthetic draft correction"]);
  await waitFor("document.querySelector('.run-digest')?.textContent.includes('SHA-256')");
  assert.equal(await evaluate("document.querySelector('#action-content .code-preview').textContent"), code);
  await click("#action-content .approval-controls .button-primary");
  await waitFor("document.getElementById('action-badge').textContent==='Completed'");
  assert.match(await evaluate("document.querySelector('.outcome-body pre').textContent"), /15\n<img/);
  assert.equal(await evaluate("document.querySelectorAll('#results-content img').length"), 0);

  await evaluate("globalThis.__macintentFixture.setMode('unknown')");
  await click("#new-request"); await click("#record-button"); await click("#send-request");
  await waitFor("document.querySelector('.button-danger') !== null");
  assert.match(await evaluate("document.querySelector('.safety-note').textContent"), /Permanent deletion.*no undo/);
  await click(".button-danger");
  await waitFor("document.getElementById('action-badge').textContent==='Stop & inspect'");
  assert.equal(await evaluate("document.getElementById('connection-button').disabled && document.getElementById('record-button').disabled && document.getElementById('new-request').disabled"), true);
  const decisions = (await calls()).filter((value) => value.path === "/api/decide");
  assert.equal(new Set(decisions.map((value) => value.body.approval_id)).size, decisions.length);
  await page("Emulation.setDeviceMetricsOverride", { width: 390, height: 844, deviceScaleFactor: 1, mobile: false });
  assert.equal(await evaluate("document.documentElement.scrollWidth <= innerWidth"), true);
  await screenshot("mobile.png");
  }
  assert.deepEqual(await evaluate("globalThis.__macintentFixture.errors()"), []); assert.deepEqual(errors, []);
  assert.equal(leakedAPIRequests, 0, "No production API request was allowed onto the network");
  console.log(recoveryOnly ? "Mocked error/visibility regression passed: error cleanup gates Record/New request, fresh requests without reconnect, focus/tab return and lease expiry recover without auto-record/send/approval, review preserved, hidden plan cancelled, safety faults stay blocked." : cancelOnly ? "Mocked cancellation regression passed: cleanup gates Record/New request; preparing/listening/ASR/review/plan/approval cancel->record on the same page without reconnect; New request; discarded notes; no approval retry; Disconnect and cleanup faults stay stopped." : sidebarOnly ? `Sidebar/capture regression passed (${offline ? "offline DOM fixture; no server/CSP acceptance" : "public server assets"}): pointer-accessible docked layout, independent scrolling, readiness->listening->ASR->review with sidebar open, explicit cancel, recording-limit result, no automatic submission, desktop/mobile, Escape.` : "Mocked browser wiring passed: explicit connect/record/send, original+notes, inert scenarios, distinct approvals/decline, source digest/output escaping, permanent deletion warning, unknown stop, desktop/mobile.");
  console.log(`Synthetic-only screenshots: ${screenshots}`);
} catch (error) {
  console.log(`Browser fixture diagnostic: ${networkRequests} static network starts, ${leakedAPIRequests} API leaks; failures=${networkFailures.join(",") || "none"}; ${errors.length} browser errors.`);
  throw error;
} finally {
  if (browser.exitCode === null && browser.signalCode === null) {
    const exit = once(browser, "exit");
    const kill = setTimeout(() => { try { process.kill(-browser.pid, "SIGKILL"); } catch {} }, 5000);
    try { await call("Browser.close"); } catch { browser.kill("SIGTERM"); }
    await exit; clearTimeout(kill);
  }
  for (const value of pending.values()) clearTimeout(value.timer);
}
