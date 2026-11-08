import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const html = readFileSync(new URL("../web/index.html", import.meta.url), "utf8");
const app = readFileSync(new URL("../web/app.mjs", import.meta.url), "utf8");

test("MacIntent live page preserves approved DOM and explicit connection boundary", () => {
  assert.match(html, /<title>MacIntent<\/title>/);
  assert.doesNotMatch(html, /Advanced|ASR Agent|DESIGN PREVIEW/);
  assert.match(html, /id="transcript"[^>]+readonly/);
  assert.match(html, /connect-src 'self'/);
  assert.match(html, /media-src 'none'/);
  const ids = [...html.matchAll(/\bid="([^"]+)"/g)].map((match) => match[1]);
  assert.equal(ids.length, new Set(ids).size);
  for (const match of app.matchAll(/\$\("([^"]+)"\)/g)) assert.ok(ids.includes(match[1]), match[1]);
  assert.match(html, /id="confirm-connect"[^>]+disabled/);
  assert.match(html, /id="record-button"[^>]+disabled/);
  assert.match(html, /<aside id="scenario-panel"[^>]+hidden/);
  assert.match(html, /aria-controls="scenario-panel" aria-expanded="false"/);
  assert.doesNotMatch(html, /<dialog id="scenario-/);
});

test("recording cancel is explicit and sidebar has no capture or approval commands", () => {
  assert.match(app, /command\(capturing\.has\(state\.phase\) \? "cancel" : "record"\)/);
  assert.match(app, /canRecord \|\| canCancelCapture/);
  assert.match(app, /Preparing—stay quiet/);
  assert.match(app, /Listening—speak now/);
  assert.doesNotMatch(app, /scenario-panel"\)\.(?:show|showModal|close)\(/);
  const css = readFileSync(new URL("../web/live.css", import.meta.url), "utf8");
  assert.match(css, /grid-template-columns: minmax\(0, 1fr\) var\(--sidebar-width\)/);
  assert.match(css, /\.has-scenarios \.main-surface[^}]*overflow-y: auto/);
});

test("browser has no microphone, credential, storage or unsafe rendering escape hatch", () => {
  assert.doesNotMatch(app, /getUserMedia|mediaDevices|MediaRecorder|localStorage|sessionStorage|indexedDB|innerHTML|insertAdjacentHTML|\beval\s*\(|api\.groq\.com|groq-api-key/);
  assert.match(app, /textContent = visible\(text\)/);
  assert.match(app, /next\.version < latestVersion/);
  assert.match(app, /"send", \{ corrections: \[\.\.\.corrections\] \}/);
  assert.match(app, /if \(config && !suspended && !backgrounded\)/);
  const start = app.indexOf('$("open-scenarios").addEventListener');
  const end = app.indexOf("async function updatePresence(");
  assert.ok(end > start && start >= 0);
  const handlers = app.slice(start, end);
  assert.doesNotMatch(handlers, /\bcommand\s*\(|\bstate\s*=/);
});
