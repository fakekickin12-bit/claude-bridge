"use strict";

// Isolated browser/API test: no desktop connection, input, screenshots or models.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const crypto = require("node:crypto");
const web = path.join(__dirname, "..", "mobile_web");
const source = fs.readFileSync(path.join(web, "desktop.js"), "utf8");
const html = fs.readFileSync(path.join(web, "desktop.html"), "utf8");
const storageKey = "claude-bridge.desktop.v1";
const copy = value => JSON.parse(JSON.stringify(value));
const response = (data, status = 200) => ({ok: status < 400, status, json: async () => copy(data)});
async function settle() { for (let i = 0; i < 12; i++) await Promise.resolve(); }

class Element {
  constructor(id = "") {
    this.id = id; this.value = ""; this.textContent = ""; this.hidden = false; this.disabled = false;
    this.style = {setProperty(key, value) { this[key] = value; }, removeProperty(key) { delete this[key]; }};
    this.dataset = {}; this.listeners = new Map(); this.attributes = new Map();
    this.naturalWidth = 0; this.naturalHeight = 0; this.srcAssignments = 0;
    this.classes = new Set();
    this.classList = {toggle: (key, yes) => yes ? this.classes.add(key) : this.classes.delete(key)};
  }
  addEventListener(type, fn) { const list = this.listeners.get(type) || []; list.push(fn); this.listeners.set(type, list); }
  async dispatch(type, values = {}) {
    await Promise.all((this.listeners.get(type) || []).map(fn => fn({preventDefault() {}, ...values})));
    await settle();
  }
  setAttribute(key, value) { this.attributes.set(key, value); }
  getAttribute(key) { return this.attributes.get(key) ?? null; }
  getBoundingClientRect() {
    return {left: 0, top: 0, width: Math.max(1000, parseFloat(this.style.width) || 1000), height: Math.max(600, parseFloat(this.style.height) || 600)};
  }
  setPointerCapture() {}
  blur() { this.blurred = true; }
  requestSubmit() { return this.dispatch("submit"); }
  set textContent(value) { this._textContent = value; this.textWrites = (this.textWrites || 0) + 1; }
  get textContent() { return this._textContent; }
  set src(value) {
    this._src = value; this.srcAssignments++;
    const complete = () => { this.naturalWidth = 1600; this.naturalHeight = 900; this.onload?.(); };
    if (this.holdDecode) this.completeDecode = complete;
    else queueMicrotask(complete);
  }
  get src() { return this._src; }
}

async function harness(options = {}) {
  const elements = new Map([...html.matchAll(/id="([^"]+)"/g)].map(match => [match[1], new Element(match[1])]));
  for (const id of ["login-required", "screen", "pointer-mark", "query-input"]) elements.get(id).hidden = true;
  elements.get("pointer-mode").value = "click"; elements.get("zoom").value = "fit"; elements.get("extra-key").value = "Control_L+a";
  const buttons = [...html.matchAll(/data-key="([^"]+)"/g)].map(match => { const value = new Element(); value.dataset.key = match[1]; return value; });
  const storage = new Map(options.storage || []), timers = new Map(), windowEvents = new Map(), documentEvents = new Map();
  let timerId = 0, clock = Date.now(), frameIndex = 0;
  const calls = [], service = {authenticated: options.authenticated !== false, session: "a".repeat(32), hash: "f".repeat(64), inputMode: "applied", statusMode: "applied", heldInput: null, heldFrame: null};
  const viewportEvents = new Map(), fullscreenRequests = [];
  const visualViewport = {height: 1180, width: 820, offsetTop: 0, offsetLeft: 0, scale: 1, addEventListener: (type, fn) => viewportEvents.set(type, fn)};
  const document = {hidden: false, getElementById: id => elements.get(id), querySelectorAll: () => buttons,
    documentElement: new Element(), activeElement: null, addEventListener: (type, fn) => documentEvents.set(type, fn)};
  elements.get("remote-text").blur = () => { elements.get("remote-text").blurred = true; document.activeElement = null; };
  document.documentElement.requestFullscreen = () => { throw new Error("Whole-document fullscreen is forbidden"); };
  if (options.fullscreen) {
    elements.get("viewport").requestFullscreen = async () => {
      fullscreenRequests.push(elements.get("viewport"));
      if (options.fullscreen === "reject") throw new Error("Synthetic fullscreen rejection");
      document.fullscreenElement = elements.get("viewport");
      documentEvents.get("fullscreenchange")?.();
    };
    document.exitFullscreen = async () => { document.fullscreenElement = null; documentEvents.get("fullscreenchange")?.(); };
  }
  class Clock extends Date { static now() { return clock; } }
  const fetch = async (url, init = {}) => {
    const body = init.body ? JSON.parse(init.body) : undefined;
    calls.push({url, body, init});
    if (url === "/api/state") return service.authenticated ? response({csrf: "synthetic-csrf"}) : response({error: "unregistered"}, 401);
    assert.equal(init.credentials, "same-origin");
    assert.equal(init.headers["X-CSRF-Token"], "synthetic-csrf");
    if (url === "/api/desktop/frame") {
      const data = {session_id: service.session, frame_id: (++frameIndex).toString(16).padStart(32, "0"),
        captured_at: clock / 1000, expires_at: clock / 1000 + 45, mime: "image/jpeg", width: 1600, height: 900,
        desktop_width: 1920, desktop_height: 1080, image_base64: "/9j/AA==", image_hash: service.hash, unchanged: false};
      if (body.image_hash === data.image_hash) { data.unchanged = true; data.image_base64 = ""; }
      if (service.heldFrame) return new Promise(resolve => { service.heldFrame = () => resolve(response(data)); });
      return response(data);
    }
    if (url === "/api/desktop/input") {
      assert.match(body.viewer_id, /^[a-f0-9]{32}$/); assert.match(body.request_id, /^[a-f0-9]{32}$/);
      assert.ok(["click", "drag", "scroll", "text", "key"].includes(body.event.kind));
      const data = {request_id: body.request_id, fingerprint: "c".repeat(64), state: service.inputMode, matched: true, completed: true, refresh_required: true};
      if (service.inputMode === "network") throw new Error("Synthetic network failure");
      if (service.inputMode === "refused") return response({error: "Synthetic stale frame"}, 409);
      if (service.inputMode === "held") return new Promise(resolve => { service.heldInput = () => resolve(response({...data, state: "applied"})); });
      return response(data);
    }
    if (url === "/api/desktop/status") return response({request_id: body.request_id, fingerprint: "c".repeat(64),
      state: service.statusMode, matched: true, completed: true, refresh_required: true});
    if (url === "/api/desktop/close") return response({closed: true});
    throw new Error("Unexpected API: " + url);
  };
  const context = {document, fetch, crypto: crypto.webcrypto, AbortController, structuredClone, console, Date: Clock,
    sessionStorage: {getItem: key => storage.get(key) || null, setItem: (key, value) => storage.set(key, value)},
    setTimeout: (fn, delay) => { timers.set(++timerId, {fn, delay}); return timerId; }, clearTimeout: id => timers.delete(id),
    setInterval: () => ++timerId, window: {innerHeight: 1180, innerWidth: 820, visualViewport, addEventListener: (type, fn) => windowEvents.set(type, fn)}};
  context.globalThis = context;
  vm.createContext(context);
  const instrumented = source.replace(/\}\)\(\);\s*$/, "globalThis.test={readFrame,sendInput,queryInput,point,imageRect,validFrame,pointerDown,pointerUp,pointerMove,controls,get state(){return {pending,frame,frameLoaded,frameBusy,inputBusy,authenticated,scale,viewerId,displayedImageHash,pureMode,manualFrame}}};})();");
  vm.runInContext(instrumented, context);
  await settle();
  return {ui: Object.fromEntries(elements), calls, storage, timers, service, test: context.test, document, windowEvents, documentEvents, visualViewport, viewportEvents, fullscreenRequests,
    async read() { await context.test.readFrame(); await settle(); },
    async runTimers(delay) { const selected = [...timers].filter(([, value]) => value.delay === delay); for (const [id, value] of selected) { timers.delete(id); value.fn(); } await settle(); },
    advance(ms) { clock += ms; context.test.controls(); }};
}

(async () => {
  const app = await harness();
  await app.read();
  assert.equal(app.test.state.authenticated, true);
  assert.equal(app.test.state.frameLoaded, true);
  // Safari can shrink only its visual viewport while leaving 100dvh unchanged.
  app.ui["remote-text"].value = "键盘打开时保留";
  app.document.activeElement = app.ui["remote-text"];
  app.visualViewport.height = 500; app.visualViewport.offsetTop = 45;
  app.viewportEvents.get("resize")();
  assert.equal(app.document.documentElement.style["--app-height"], "500px");
  assert.equal(app.document.documentElement.style["--app-top"], "45px");
  assert.ok(app.document.documentElement.classes.has("keyboard-open"));
  app.visualViewport.height = 180; app.viewportEvents.get("resize")();
  assert.ok(app.document.documentElement.classes.has("keyboard-compact"));
  app.visualViewport.scale = 2; app.viewportEvents.get("resize")();
  assert.equal(app.document.documentElement.style["--app-height"], undefined, "Pinch zoom must not feed back into document sizing");
  assert.ok(!app.document.documentElement.classes.has("keyboard-open"));
  app.document.activeElement = null; app.visualViewport.scale = 1; app.visualViewport.height = 1180; app.visualViewport.offsetTop = 0;
  app.viewportEvents.get("scroll")();
  assert.equal(app.document.documentElement.style["--app-height"], "1180px");
  assert.ok(!app.document.documentElement.classes.has("keyboard-compact"));
  assert.equal(app.ui["remote-text"].value, "键盘打开时保留");
  assert.equal(app.ui.screen.hidden, false);
  assert.equal(app.ui["send-key"].disabled, false);
  const centre = app.test.point(500, 300);
  assert.equal(centre.x, .5); assert.equal(centre.y, .5);
  assert.equal(app.test.point(500, 1), null, "Letterboxing must not become a desktop click");
  const firstId = app.test.state.frame.frame_id;
  await app.read();
  assert.equal(app.ui.screen.srcAssignments, 1, "Unchanged frames reuse only a successfully decoded image");
  assert.notEqual(app.test.state.frame.frame_id, firstId, "Even unchanged pictures receive a fresh input grant");
  assert.equal(app.calls.filter(c => c.url.endsWith("/frame")).at(-1).body.image_hash, "f".repeat(64));
  const quietWrites = {notice: app.ui.notice.textWrites, connection: app.ui.connection.textWrites, refresh: app.ui.refresh.textWrites};
  for (let index = 0; index < 3; index++) await app.read();
  assert.deepEqual({notice: app.ui.notice.textWrites, connection: app.ui.connection.textWrites, refresh: app.ui.refresh.textWrites}, quietWrites,
    "Quiet same-image reads must not rewrite status, refresh label or the displayed image");
  assert.equal(app.ui.screen.srcAssignments, 1);
  app.service.heldFrame = true;
  const quietRead = app.test.readFrame(); await settle();
  assert.equal(app.test.state.frameBusy, true);
  assert.equal(app.ui.refresh.disabled, false, "Background reads must not gray/flash the refresh button");
  assert.equal(app.ui.refresh.textContent, "刷新");
  const manualCount = app.calls.filter(c => c.url.endsWith("/frame")).length;
  await app.ui.refresh.dispatch("click");
  assert.equal(app.ui.refresh.disabled, true);
  assert.equal(app.ui.refresh.textContent, "刷新中…");
  assert.equal(app.ui.refresh.getAttribute("aria-busy"), "true");
  await app.ui.refresh.dispatch("click");
  assert.equal(app.calls.filter(c => c.url.endsWith("/frame")).length, manualCount,
    "Manual refresh reuses an in-flight read without queuing another request");
  app.service.heldFrame(); app.service.heldFrame = null; await quietRead; await settle();
  assert.equal(app.ui.refresh.disabled, false);
  assert.equal(app.ui.refresh.textContent, "刷新");
  assert.equal(app.ui.refresh.getAttribute("aria-busy"), "false");
  // A failed image decode cannot claim its hash as a reusable displayed image.
  app.service.hash = "e".repeat(64); await app.read();
  app.ui.screen.onerror();
  assert.equal(app.test.state.displayedImageHash, "");
  await app.read();
  assert.equal(app.calls.filter(c => c.url.endsWith("/frame")).at(-1).body.image_hash, "");
  assert.equal(app.test.state.frameLoaded, true);

  // A changed JPEG is accepted with its input grant only after decoding; the
  // previous visible frame remains ready without a transient gray/reading UI.
  app.service.hash = "d".repeat(64); app.ui.screen.holdDecode = true;
  const previousDecodedFrame = app.test.state.frame.frame_id;
  const previousConnectionWrites = app.ui.connection.textWrites;
  await app.test.readFrame();
  assert.equal(app.test.state.frameLoaded, true);
  assert.equal(app.test.state.frame.frame_id, previousDecodedFrame);
  assert.equal(app.ui.connection.textContent, "已连接");
  assert.equal(app.ui.connection.textWrites, previousConnectionWrites);
  assert.equal(app.ui["send-key"].disabled, false);
  app.ui.screen.holdDecode = false; app.ui.screen.completeDecode(); await settle();
  assert.notEqual(app.test.state.frame.frame_id, previousDecodedFrame);
  assert.equal(app.ui.connection.textWrites, previousConnectionWrites);

  // An older frame completing after an input cannot re-enable stale controls.
  app.service.heldFrame = true;
  const oldFrameRead = app.test.readFrame();
  await settle();
  const frameCallsWhileHeld = app.calls.filter(c => c.url.endsWith("/frame")).length;
  await app.test.readFrame();
  assert.equal(app.calls.filter(c => c.url.endsWith("/frame")).length, frameCallsWhileHeld, "Frames are serial");
  await app.test.sendInput({kind: "key", key: "Return"});
  app.service.heldFrame(); app.service.heldFrame = null;
  await oldFrameRead; await settle();
  assert.equal(app.test.state.frameLoaded, false, "Late pre-input frame is rejected");
  await app.read();

  // Preserve text edited after submission; only the exact applied snapshot clears.
  app.ui["remote-text"].value = "原来的输入";
  app.service.inputMode = "held";
  const inFlight = app.test.sendInput({kind: "text", text: "原来的输入"}, app.test.state.frame, "原来的输入");
  await settle();
  assert.equal(app.ui["send-key"].disabled, true);
  app.ui["remote-text"].value = "发送后继续输入";
  app.service.heldInput(); await inFlight; await settle();
  assert.equal(app.ui["remote-text"].value, "发送后继续输入");
  await app.read();
  app.service.inputMode = "applied";
  await app.test.sendInput({kind: "text", text: "发送后继续输入"}, app.test.state.frame, "发送后继续输入");
  assert.equal(app.ui["remote-text"].value, "");

  // Unknown result: retain original identity, never input again; status-only recovery.
  await app.read(); app.service.inputMode = "network";
  app.ui["remote-text"].value = "未知结果要保留";
  await app.test.sendInput({kind: "text", text: "未知结果要保留"}, app.test.state.frame, "未知结果要保留");
  const original = copy(app.test.state.pending);
  assert.equal(original.unknown, true);
  const beforeRetry = app.calls.filter(c => c.url.endsWith("/input")).length;
  await app.test.sendInput({kind: "text", text: "未知结果要保留"});
  assert.equal(app.calls.filter(c => c.url.endsWith("/input")).length, beforeRetry);
  assert.equal(app.ui["remote-text"].value, "未知结果要保留");
  const pendingStorage = [...app.storage];
  await app.test.queryInput();
  assert.equal(app.calls.filter(c => c.url.endsWith("/status")).at(-1).body.request_id, original.request_id);
  assert.equal(app.test.state.pending, null); assert.equal(app.ui["remote-text"].value, "");
  const resumed = await harness({storage: pendingStorage});
  await settle();
  assert.equal(resumed.calls.filter(c => c.url.endsWith("/input")).length, 0);
  assert.equal(resumed.calls.filter(c => c.url.endsWith("/status")).at(-1).body.request_id, original.request_id);
  assert.notEqual(resumed.test.state.viewerId, original.viewer_id, "Reload receives a fresh frame lease even if the old viewer was closed");
  assert.equal(resumed.calls.filter(c => c.url.endsWith("/status")).at(-1).body.viewer_id, original.viewer_id,
    "Receipt recovery must retain the original viewer identity");
  await resumed.read();
  assert.equal(resumed.calls.filter(c => c.url.endsWith("/frame")).at(-1).body.viewer_id, resumed.test.state.viewerId);

  await app.read(); app.service.inputMode = "refused"; app.ui["remote-text"].value = "拒绝后保留";
  await app.test.sendInput({kind: "text", text: "拒绝后保留"}, app.test.state.frame, "拒绝后保留");
  assert.equal(app.test.state.pending, null); assert.equal(app.ui["remote-text"].value, "拒绝后保留");
  app.service.inputMode = "applied";
  await app.read(); app.advance(46000);
  const beforeExpired = app.calls.length;
  await app.test.sendInput({kind: "key", key: "Return"});
  assert.equal(app.calls.length, beforeExpired, "Expired frames cannot authorize input");
  await app.read(); const previousSession = copy(app.test.state.frame);
  app.service.session = "b".repeat(32); await app.read();
  await app.test.sendInput({kind: "click", x: .5, y: .5, button: 1, clicks: 1}, previousSession);
  assert.equal(app.calls.filter(c => c.url.endsWith("/input")).length, beforeRetry + 1);

  // Pointer modes use normalized image coordinates. Pinching never clicks Ubuntu.
  const event = (id, x, y, button = 0) => ({pointerId: id, clientX: x, clientY: y, button, preventDefault() {}});
  app.test.pointerDown(event(1, 500, 300)); app.test.pointerUp(event(1, 500, 300));
  app.test.pointerDown(event(1, 500, 300)); app.test.pointerUp(event(1, 500, 300)); await settle();
  let action = app.calls.filter(c => c.url.endsWith("/input")).at(-1).body.event;
  assert.equal(action.kind, "click"); assert.equal(action.clicks, 2); assert.equal(action.x, .5); assert.equal(action.y, .5);
  await app.read(); app.ui["pointer-mode"].value = "right";
  app.test.pointerDown(event(1, 500, 300)); app.test.pointerUp(event(1, 500, 300)); await settle();
  assert.equal(app.calls.filter(c => c.url.endsWith("/input")).at(-1).body.event.button, 3);
  await app.read(); app.ui["pointer-mode"].value = "scroll";
  app.test.pointerDown(event(1, 500, 300)); app.test.pointerUp(event(1, 500, 180)); await settle();
  action = app.calls.filter(c => c.url.endsWith("/input")).at(-1).body.event;
  assert.equal(action.kind, "scroll"); assert.equal(action.delta_y, 10);
  await app.read(); app.ui["pointer-mode"].value = "drag";
  app.test.pointerDown(event(1, 500, 300)); app.test.pointerUp(event(1, 600, 300)); await settle();
  action = app.calls.filter(c => c.url.endsWith("/input")).at(-1).body.event;
  assert.equal(action.kind, "drag"); assert.equal(action.to_x, .6);
  await app.read(); const beforePinch = app.calls.filter(c => c.url.endsWith("/input")).length;
  app.test.pointerDown(event(1, 400, 300)); app.test.pointerDown(event(2, 600, 300));
  app.test.pointerMove(event(2, 800, 300)); app.test.pointerUp(event(1, 400, 300)); app.test.pointerUp(event(2, 800, 300));
  await app.runTimers(300);
  assert.equal(app.calls.filter(c => c.url.endsWith("/input")).length, beforePinch);
  assert.ok(app.test.state.scale > 0);
  app.ui["pointer-mode"].value = "browse";
  app.test.pointerDown(event(1, 500, 300)); app.test.pointerUp(event(1, 500, 300)); await app.runTimers(300);
  assert.equal(app.calls.filter(c => c.url.endsWith("/input")).length, beforePinch);

  app.document.hidden = true; app.documentEvents.get("visibilitychange")();
  const beforeHidden = app.calls.length; await app.read();
  assert.equal(app.calls.length, beforeHidden, "Hidden tabs stop screenshot polling");
  app.document.hidden = false; app.documentEvents.get("visibilitychange")();
  await app.read(); app.windowEvents.get("pagehide")({persisted: false}); await settle();
  assert.equal(app.calls.at(-1).url, "/api/desktop/close");
  const unpaired = await harness({authenticated: false}); await unpaired.read();
  assert.equal(unpaired.calls.filter(c => c.url.endsWith("/frame")).length, 0);
  assert.equal(unpaired.ui["login-required"].hidden, false);
  assert.equal(unpaired.ui.viewport.hidden, true);

  // Fullscreen contains only the image viewport; no whole-document request.
  const native = await harness({fullscreen: "native"});
  await native.ui.fullscreen.dispatch("click");
  assert.equal(native.test.state.pureMode, false, "No blank-screen lock before a valid frame");
  await native.read();
  native.ui["remote-text"].value = "全屏前的草稿";
  native.ui.zoom.value = "1"; await native.ui.zoom.dispatch("change");
  await native.ui.fullscreen.dispatch("click");
  assert.deepEqual(native.fullscreenRequests, [native.ui.viewport]);
  assert.equal(native.document.fullscreenElement, native.ui.viewport);
  assert.ok(native.document.documentElement.classes.has("pure-screen"));
  assert.equal(native.ui["remote-text"].blurred, true);
  assert.equal(native.test.state.scale, 0);
  native.document.fullscreenElement = null; native.documentEvents.get("fullscreenchange")();
  assert.equal(native.test.state.pureMode, false);
  assert.equal(native.test.state.scale, 1);
  assert.equal(native.ui["remote-text"].value, "全屏前的草稿");
  await native.ui.fullscreen.dispatch("click");
  native.documentEvents.get("keydown")({key: "Escape", preventDefault() {}}); await settle();
  assert.equal(native.test.state.pureMode, false);
  assert.equal(native.calls.filter(c => c.url.endsWith("/input")).length, 0);

  // Safari fallback/rejected API hides all website UI and exits with a
  // three-finger light tap, even when the displayed frame becomes stale.
  for (const mode of [undefined, "reject", "native"]) {
    const pure = await harness({fullscreen: mode}); await pure.read();
    await pure.ui.fullscreen.dispatch("click");
    assert.ok(pure.document.documentElement.classes.has("pure-screen"));
    pure.advance(46000);
    for (const [id, x] of [[1, 450], [2, 500], [3, 550]]) pure.test.pointerDown(event(id, x, 300));
    for (const [id, x] of [[1, 450], [2, 500], [3, 550]]) pure.test.pointerUp(event(id, x, 300));
    await settle(); await pure.runTimers(300);
    assert.equal(pure.test.state.pureMode, false);
    assert.ok(!pure.document.documentElement.classes.has("pure-screen"));
    assert.equal(pure.calls.filter(c => c.url.endsWith("/input")).length, 0, "Exit gesture must never click Ubuntu");
  }
  const css = fs.readFileSync(path.join(web, "desktop.css"), "utf8");
  for (const item of ["desktop-header", "desktop-tools", "notice", "input-panel", "pointer-mark", "empty-frame"]) {
    assert.ok(css.includes(`.pure-screen .${item}`), `Pure screen must hide ${item}`);
  }
  assert.ok(css.includes(".viewport:fullscreen") && css.includes(".viewport:-webkit-full-screen"));

  console.log("Mobile desktop UI: paired CSRF, fresh frames/hash reuse, letterbox coordinates, touch modes/pinch, original-id recovery, draft preservation, stale/hidden guards and lease close OK");
})().catch(error => { console.error(error); process.exitCode = 1; });
