"use strict";

// Runs only fake HTTP jobs and a fake clock. No SSH, translation, or delivery calls.
const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const web = path.join(__dirname, "..", "mobile_web");
const script = fs.readFileSync(path.join(web, "app.js"), "utf8");
const html = fs.readFileSync(path.join(web, "index.html"), "utf8");
const storageKey = "claude-bridge.mobile.v1";
const translatedEnglish = "Please inspect this synthetic task.";

function clone(value) { return JSON.parse(JSON.stringify(value)); }
function response(value) { return {ok: true, status: 200, json: async () => clone(value)}; }

class Element {
  constructor(id = "") {
    Object.assign(this, {id, value: "", textContent: "", hidden: false, disabled: false,
      checked: true, dataset: {}, children: [], options: [], files: []});
    this.attributes = new Map();
    this.listeners = new Map();
    this.parentElement = {scrollHeight: 0, scrollTop: 0, clientHeight: 0};
    this.classList = {add() {}, remove() {}, toggle() {}};
  }
  addEventListener(type, callback) {
    if (!this.listeners.has(type)) this.listeners.set(type, []);
    this.listeners.get(type).push(callback);
  }
  dispatch(type) { for (const callback of this.listeners.get(type) || []) callback({preventDefault() {}}); }
  setAttribute(name, value) { this.attributes.set(name, value); }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) {
    this.children = children;
    if (this.id === "profile-select") this.options = children;
  }
  querySelectorAll() { return []; }
  focus() {}
}

class DeliveryService {
  constructor() {
    this.clock = 0;
    this.confirmAt = 110000;
    this.initialState = "unknown";
    this.jobs = new Map();
    this.posts = [];
    this.reads = [];
    this.translationCalls = 0;
    this.deferNextPost = false;
    this.postRelease = null;
    this.target = {ready: true, mode: "Code", context: "a".repeat(64)};
  }

  job(id) {
    const payload = this.jobs.get(id);
    const state = this.initialState === "failed" ? "failed" : this.clock >= this.confirmAt ? "submitted" : "unknown";
    return {id, action: payload.action, source: payload.source, english: translatedEnglish, state,
      result: {english: translatedEnglish}, error: state === "unknown" ? "Synthetic SSH timeout" : ""};
  }

  async fetch(url, options) {
    if (url === "/api/state") return response({
      csrf: "synthetic-csrf", profiles: [{id: "synthetic", label: "Synthetic API", kind: "api", model: "synthetic"}],
      default_profile: "synthetic", target: clone(this.target),
      worker: {online: true, busy: false}, reply: {text: "", generating: this.target.busy === true}, active_job: null
    });
    if (url === "/api/history") return response({jobs: []});
    if (url === "/api/jobs") {
      const payload = JSON.parse(options.body);
      assert(["translate_send", "send"].includes(payload.action));
      this.posts.push(payload);
      this.jobs.set(payload.id, payload);
      if (payload.action === "translate_send") this.translationCalls += 1;
      const queued = {id: payload.id, action: payload.action, state: "queued"};
      if (this.deferNextPost) {
        this.deferNextPost = false;
        return new Promise(resolve => { this.postRelease = () => resolve(response(queued)); });
      }
      return response(queued);
    }
    if (url.startsWith("/api/jobs/")) {
      const id = url.split("/").pop();
      assert(this.jobs.has(id));
      this.reads.push({id, clock: this.clock});
      return response(this.job(id));
    }
    throw new Error(`Unexpected fake request ${url}`);
  }
}

function browser(service, storage = null) {
  const elements = new Map();
  for (const match of html.matchAll(/<([a-z][a-z0-9-]*)\b([^>]*\bid="([^"]+)"[^>]*)>/g)) {
    const element = new Element(match[3]);
    element.hidden = /\bhidden\b/.test(match[2]);
    elements.set(element.id, element);
  }
  storage ||= new Map([[storageKey, JSON.stringify({source: "检查合成任务", english: "", revision: 1})]]);
  const intervals = new Map();
  const listeners = new Map();
  const tabs = ["compose", "reply", "history"].map(tab => { const item = new Element(); item.dataset.tab = tab; return item; });
  class ClockDate extends Date { static now() { return service.clock; } }
  const document = {
    hidden: false, getElementById: id => elements.get(id) || null, querySelector: () => new Element(),
    querySelectorAll: selector => selector === "[data-tab]" ? tabs : [],
    addEventListener(type, callback) {
      if (!listeners.has(type)) listeners.set(type, []);
      listeners.get(type).push(callback);
    },
    createElement: () => new Element(), createElementNS: () => new Element(), createTextNode: text => text
  };
  const context = {
    console, Headers, FormData, URLSearchParams, AbortController, Intl, Date: ClockDate, crypto: crypto.webcrypto,
    setTimeout, clearTimeout,
    setInterval(callback, milliseconds) { intervals.set(milliseconds, callback); return milliseconds; },
    clearInterval: id => intervals.delete(id), document,
    localStorage: {getItem: key => storage.get(key) || null, setItem: (key, value) => storage.set(key, value)},
    window: {location: {hash: "", pathname: "/", search: ""}, history: {replaceState() {}}, addEventListener() {}},
    fetch: (url, options) => service.fetch(url, options)
  };
  vm.createContext(context);
  vm.runInContext(script, context);
  return {
    elements, storage, draft: () => JSON.parse(storage.get(storageKey)),
    click: id => elements.get(id).dispatch("click"),
    edit(id, value) { elements.get(id).value = value; elements.get(id).dispatch("input"); },
    poll: () => intervals.get(1000)?.(),
    pollState: () => intervals.get(5000)?.(),
    visibility(hidden) {
      document.hidden = hidden;
      for (const callback of listeners.get("visibilitychange") || []) callback();
    }
  };
}

async function flush() { await new Promise(resolve => setImmediate(resolve)); }
async function settle(predicate, label) {
  for (let attempt = 0; attempt < 60; attempt += 1) {
    await flush();
    if (predicate()) return;
  }
  throw new Error(`Delivery UI did not settle: ${label}`);
}

async function startUnknown(service, page) {
  await settle(() => page.elements.get("profile-select").value === "synthetic", "initial device state");
  page.click("translate-send-button");
  await settle(() => service.posts.length === 1, "queued translate and send");
  page.poll();
  await settle(() => Boolean(page.draft().lastUncertain), "unknown result");
  return service.posts[0].id;
}

async function slowReceiptConfirmsWithoutAnotherPost() {
  const service = new DeliveryService();
  const page = browser(service);
  const id = await startUnknown(service, page);
  assert.equal(page.elements.get("english").value, translatedEnglish);
  assert.equal(page.draft().boundSource, page.draft().source);
  assert.equal(page.draft().pendingJob, null);
  assert.equal(page.draft().lastUncertain.snapshot.english, translatedEnglish);
  assert.equal(page.draft().lastUncertain.payload.english, "", "Keep the exact original request intact");
  assert.equal(page.elements.get("send-button").disabled, true);
  assert.equal(page.elements.get("translate-send-button").disabled, true);
  const revision = page.draft().revision;
  for (service.clock = 1000; service.clock <= 110000; service.clock += 1000) {
    page.poll();
    await flush();
    if (service.clock < service.confirmAt) assert.equal(page.draft().revision, revision, "Duplicate unknown reads must not repeatedly rewrite the draft");
  }
  await settle(() => page.draft().receiptJobs.length === 0, "110-second receipt confirmation");
  assert.equal(page.elements.get("source").value, "");
  assert.equal(page.elements.get("english").value, "");
  assert.equal(page.draft().lastUncertain, null);
  assert.equal(page.draft().revision, revision + 1, "Only confirmed draft clearing adds a new edit revision");
  assert.match(page.elements.get("notice").textContent, /Ubuntu 回执确认提交/);
  assert.equal(service.posts.length, 1);
  assert.equal(service.translationCalls, 1);
  assert(service.reads.every(read => read.id === id));
  for (let index = 1; index < service.reads.length; index += 1) {
    assert(service.reads[index].clock - service.reads[index - 1].clock >= 5000, "Unknown receipts should be checked about every five seconds");
  }
}

async function editedDraftAndFailedTranslationRecovery() {
  const service = new DeliveryService();
  const page = browser(service);
  await settle(() => page.elements.get("profile-select").value === "synthetic", "edited draft startup");
  page.click("translate-send-button");
  await settle(() => service.posts.length === 1, "edited draft queued");
  page.edit("source", "新的合成草稿");
  page.edit("english", "A newly edited draft.");
  page.poll();
  await settle(() => Boolean(page.draft().lastUncertain), "edited draft unknown");
  assert.equal(page.elements.get("english").value, "A newly edited draft.");
  service.clock = 110000;
  page.poll();
  await settle(() => page.draft().receiptJobs.length === 0, "edited draft receipt confirmed");
  assert.equal(page.elements.get("source").value, "新的合成草稿");
  assert.equal(page.elements.get("english").value, "A newly edited draft.");
  assert.match(page.elements.get("notice").textContent, /草稿已保留/);

  const failedService = new DeliveryService();
  failedService.initialState = "failed";
  const failedPage = browser(failedService);
  await settle(() => failedPage.elements.get("profile-select").value === "synthetic", "failed delivery startup");
  failedPage.click("translate-send-button");
  await settle(() => failedService.posts.length === 1, "failed delivery queued");
  failedPage.poll();
  await settle(() => failedPage.elements.get("english").value === translatedEnglish, "known failed delivery retains its translation");
  assert.equal(failedPage.draft().lastUncertain, null);
  assert.equal(failedPage.draft().receiptJobs.length, 0);
  assert.equal(failedPage.elements.get("send-button").disabled, false);
}

async function reloadAndManualAcknowledgementRetainOriginalId() {
  const service = new DeliveryService();
  const first = browser(service);
  const id = await startUnknown(service, first);
  const restored = browser(service, first.storage);
  await settle(() => service.reads.length >= 2, "reload resumes receipt GET");
  assert.equal(restored.elements.get("english").value, translatedEnglish);
  assert.equal(restored.draft().lastUncertain.snapshot.english, translatedEnglish);
  assert.equal(service.posts.length, 1);
  restored.click("checked-ubuntu-button");
  assert.equal(restored.draft().lastUncertain, null);
  assert.equal(restored.draft().receiptJobs.length, 1, "Acknowledging the UI must not discard receipt tracking");
  restored.visibility(true);
  const readsBefore = service.reads.length;
  service.clock = 110000;
  restored.poll();
  await flush();
  assert.equal(service.reads.length, readsBefore);
  restored.visibility(false);
  await settle(() => restored.draft().receiptJobs.length === 0, "visible page resumes definitive receipt check");
  assert(service.reads.every(read => read.id === id));
  assert.equal(service.posts.length, 1);
  assert.equal(service.translationCalls, 1);
}

async function oldReceiptCannotClearANewerPendingJob() {
  const service = new DeliveryService();
  const page = browser(service);
  const firstId = await startUnknown(service, page);
  page.edit("source", "Another synthetic task");
  page.edit("english", "Another synthetic task in English.");
  service.deferNextPost = true;
  page.click("send-button");
  await settle(() => Boolean(service.postRelease), "new request POST in flight");
  const secondId = page.draft().pendingJob.id;
  service.clock = 110000;
  page.poll();
  await settle(() => !page.draft().receiptJobs.some(job => job.id === firstId), "older receipt reconciled");
  assert.equal(page.draft().pendingJob.id, secondId);
  assert.equal(page.elements.get("english").value, "Another synthetic task in English.");
  service.postRelease();
  await flush();
  assert.equal(service.posts.length, 2);
}

async function verifiedRunningComposerAllowsAppendButNotNew() {
  const service = new DeliveryService();
  service.target = {...service.target, ready: false, busy: true, send_ready: true,
    send_mode: "running_insert", send_reason: "Claude 正在运行，可以追加文字。"};
  const page = browser(service);
  await settle(() => page.elements.get("profile-select").value === "synthetic", "Running composer native grant");
  page.edit("english", "Append this to the current running task.");
  assert.equal(page.elements.get("send-button").disabled, false);
  assert.equal(page.elements.get("translate-send-button").disabled, false);
  assert.equal(page.elements.get("new-button").disabled, true);
  assert.equal(page.elements.get("target-state").textContent, "正在运行 · 可追加");
  page.click("new-button");
  assert.equal(service.posts.length, 0, "Send permission does not grant idle/new actions");
  page.click("send-button");
  await settle(() => service.posts.length === 1, "Explicit append submission");
  assert.equal(service.posts[0].action, "send");
  assert.equal(service.posts[0].english, "Append this to the current running task.");
  assert.equal(page.elements.get("send-button").disabled, true, "Pending original request still blocks another submission");
  assert.equal(service.translationCalls, 0);
}

async function blockedOrUnconfirmedRunningComposerNeverPosts() {
  const cases = [
    {send_ready: false}, {send_ready: "true"}, {}, {send_ready: true, awaiting_input: true},
    {send_ready: true, approval: true}, {send_ready: true, locked: true},
    {send_ready: true, draft_chars: 1}, {send_ready: true, window_visible: false}
  ];
  for (const change of cases) {
    const service = new DeliveryService();
    service.target = {...service.target, ready: false, busy: true, send_reason: "Native composer not available", ...change};
    const page = browser(service);
    await settle(() => page.elements.get("profile-select").value === "synthetic", "Blocked running composer");
    page.edit("english", "Keep this draft without sending.");
    assert.equal(page.elements.get("send-button").disabled, true);
    assert.equal(page.elements.get("translate-send-button").disabled, true);
    page.click("send-button"); page.click("translate-send-button");
    assert.equal(service.posts.length, 0, "A blocked click cannot bypass native send readiness");
    assert.equal(page.elements.get("english").value, "Keep this draft without sending.");
  }
}

async function runningAttachmentsStayStagedUntilIdle() {
  const service = new DeliveryService();
  service.target = {...service.target, ready: false, busy: true, send_ready: true,
    send_mode: "running_insert", capabilities: {native_attachments: true, running_attachments: false}};
  const attachments = [{id: "d".repeat(32), name: "staged.png", size: 660000, kind: "image"}];
  const source = "继续当前任务";
  const storage = new Map([[storageKey, JSON.stringify({source, english: "Append with the staged image.",
    boundSource: source, attachments, revision: 1})]]);
  const page = browser(service, storage);
  await settle(() => page.elements.get("profile-select").value === "synthetic", "Running staged attachment");
  assert.equal(page.elements.get("send-button").disabled, true);
  assert.equal(page.elements.get("translate-send-button").disabled, true);
  assert.equal(page.elements.get("attach-button").disabled, false, "Local file staging remains available");
  assert.match(page.elements.get("attachment-hint").textContent, /正在运行.*附件.*等任务空闲/);
  page.click("send-button"); page.click("translate-send-button");
  assert.match(page.elements.get("notice").textContent, /正在运行.*附件.*等任务空闲/);
  assert.equal(service.posts.length, 0);
  assert.deepEqual(page.draft().attachments, attachments);
  service.target = {...service.target, ready: true, busy: false, send_mode: "idle"};
  page.pollState();
  await settle(() => page.elements.get("send-button").disabled === false, "Idle restores ordinary attachment delivery");
  assert.equal(page.elements.get("translate-send-button").disabled, false);
  assert.deepEqual(page.draft().attachments, attachments);
  assert.equal(service.posts.length, 0, "Becoming idle never automatically submits a staged draft");
}

(async () => {
  await slowReceiptConfirmsWithoutAnotherPost();
  await editedDraftAndFailedTranslationRecovery();
  await reloadAndManualAcknowledgementRetainOriginalId();
  await oldReceiptCannotClearANewerPendingJob();
  await verifiedRunningComposerAllowsAppendButNotNew();
  await blockedOrUnconfirmedRunningComposerNeverPosts();
  await runningAttachmentsStayStagedUntilIdle();
  console.log("Mobile delivery UI passed: unknown/failed English recovery, matching-draft resend guard, 110-second receipt confirmation, edit preservation, reload/visibility original-ID queries, and newer-job preservation without repeated translation or submission");
})().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
