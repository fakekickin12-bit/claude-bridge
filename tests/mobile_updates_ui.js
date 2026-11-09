"use strict";

// Isolated browser-state checks. The fetch stub never contacts a real service.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const crypto = require("node:crypto");

const web = path.join(__dirname, "..", "mobile_web");
const script = fs.readFileSync(path.join(web, "app.js"), "utf8");
const html = fs.readFileSync(path.join(web, "index.html"), "utf8");
const storageKey = "claude-bridge.mobile.v1";
const currentVersion = "a".repeat(64);
const nextVersion = "b".repeat(64);

function response(value, status = 200) {
  return {ok: status >= 200 && status < 300, status, json: async () => JSON.parse(JSON.stringify(value))};
}

class Element {
  constructor(id = "", tagName = "div") {
    Object.assign(this, {id, tagName, value: "", textContent: "", hidden: false, disabled: false,
      checked: true, dataset: {}, options: [], children: [], files: []});
    this.attributes = new Map();
    this.listeners = new Map();
    this.parentElement = {scrollHeight: 0, scrollTop: 0, clientHeight: 0};
    this.classes = new Set();
    this.classList = {
      add: name => this.classes.add(name), remove: name => this.classes.delete(name),
      toggle: (name, active) => active ? this.classes.add(name) : this.classes.delete(name)
    };
  }

  addEventListener(type, callback) {
    if (!this.listeners.has(type)) this.listeners.set(type, []);
    this.listeners.get(type).push(callback);
  }

  dispatch(type) {
    for (const callback of this.listeners.get(type) || []) callback({preventDefault() {}});
  }

  setAttribute(name, value) { this.attributes.set(name, value); }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) {
    this.children = children;
    if (this.id === "profile-select") this.options = children;
  }
  querySelectorAll() { return []; }
  focus() {}
}

class Service {
  constructor() {
    this.version = currentVersion;
    this.manifestFailure = false;
    this.manifestReads = 0;
    this.posts = [];
    this.jobReads = [];
    this.jobs = new Map();
    this.workerBusy = false;
    this.activeJob = null;
    this.postRelease = null;
    this.uploadRelease = null;
    this.deferPost = false;
    this.deferUpload = false;
  }

  async fetch(url, options) {
    if (url === "/manifest.webmanifest") {
      this.manifestReads += 1;
      assert.equal(options.cache, "no-store");
      return this.manifestFailure ? response({}, 503) : response({bridge_version: this.version});
    }
    if (url === "/api/state") return response({
      csrf: "synthetic-csrf", profiles: [{id: "synthetic", label: "Synthetic API", model: "synthetic", kind: "api"}],
      default_profile: "synthetic", target: {ready: true, mode: "Chat", title: "Synthetic target", context: "c".repeat(64)},
      reply: {text: "", source_hash: "", generating: false},
      worker: {online: true, busy: this.workerBusy}, active_job: this.activeJob
    });
    if (url === "/api/history") return response({jobs: []});
    if (url.startsWith("/api/jobs/")) {
      const id = url.split("/").pop();
      this.jobReads.push(id);
      return response(this.jobs.get(id) || {id, action: "send", state: "queued"});
    }
    if (url === "/api/jobs") {
      const payload = JSON.parse(options.body);
      this.posts.push(payload);
      const job = {id: payload.id, action: payload.action, state: "queued"};
      this.jobs.set(job.id, job);
      if (this.deferPost) return new Promise(resolve => { this.postRelease = () => resolve(response(job)); });
      return response(job);
    }
    if (url === "/api/uploads") {
      const result = {id: "d".repeat(32), name: "synthetic.txt", size: 9, kind: "file"};
      if (this.deferUpload) return new Promise(resolve => { this.uploadRelease = () => resolve(response(result)); });
      return response(result);
    }
    throw new Error(`Unexpected mock request ${url}`);
  }
}

function browser(service, initialDraft = {}, release = true, options = {}) {
  const elements = new Map();
  const markup = options.oldHtml ? html.replace(/    <div class="mobile-menu-bar">.*?<\/div>\r?\n/, "")
    .replace('id="connection-panel" ', "")
    .replace(/    <section id="update-banner"[\s\S]*?<\/section>\r?\n/, "") : html;
  for (const match of markup.matchAll(/<([a-z][a-z0-9-]*)\b([^>]*\bid="([^"]+)"[^>]*)>/g)) {
    const element = new Element(match[3], match[1]);
    element.hidden = /\bhidden\b/.test(match[2]);
    for (const attribute of match[2].matchAll(/(aria-[\w-]+)="([^"]*)"/g)) element.setAttribute(attribute[1], attribute[2]);
    elements.set(element.id, element);
  }
  const storage = new Map([[storageKey, JSON.stringify(initialDraft)]]);
  const events = [];
  const intervals = new Map();
  const listeners = new Map();
  const tabs = ["compose", "reply", "history"].map(tab => {
    const element = new Element();
    element.dataset.tab = tab;
    return element;
  });
  const workspace = new Element();
  const connectionFallback = new Element();
  const document = {
    hidden: false,
    getElementById: id => elements.get(id) || null,
    querySelector: selector => selector === ".connection-strip" ? connectionFallback : workspace,
    querySelectorAll: selector => selector === "[data-tab]" ? tabs : [],
    createElement: tagName => new Element("", tagName),
    createElementNS: (_, tagName) => new Element("", tagName),
    createTextNode: value => value,
    addEventListener(type, callback) {
      if (!listeners.has(type)) listeners.set(type, []);
      listeners.get(type).push(callback);
    }
  };
  let intervalId = 0;
  const context = {
    console, Headers, FormData, URLSearchParams, AbortController, Intl, Date,
    crypto: crypto.webcrypto, setTimeout, clearTimeout,
    setInterval(callback, milliseconds) { intervals.set(++intervalId, {callback, milliseconds}); return intervalId; },
    clearInterval: id => intervals.delete(id),
    localStorage: {
      getItem: key => storage.get(key) || null,
      setItem(key, value) {
        if (options.storageFails) throw new Error("Synthetic storage failure");
        storage.set(key, value);
        events.push("save");
      }
    },
    document,
    window: {
      location: {hash: "", pathname: "/", search: "", reload() { events.push("reload"); }},
      history: {replaceState() {}}, addEventListener() {},
      sessionStorage: {
        getItem(key) {
          if (options.session === null) throw new Error("Synthetic session storage unavailable");
          return session.get(key) || null;
        },
        setItem(key, value) {
          if (options.session === null) throw new Error("Synthetic session storage unavailable");
          session.set(key, value);
        }
      }
    },
    fetch: (url, options) => service.fetch(url, options)
  };
  const session = options.session || new Map();
  vm.createContext(context);
  vm.runInContext(release ? script.replace("__BRIDGE_BUILD__", options.buildVersion || currentVersion) : script, context);
  return {
    elements, events, connectionFallback,
    draft: () => JSON.parse(storage.get(storageKey)),
    click: id => elements.get(id).dispatch("click"),
    poll(milliseconds) {
      for (const interval of intervals.values()) if (interval.milliseconds === milliseconds) interval.callback();
    },
    visibility(hidden) {
      document.hidden = hidden;
      for (const callback of listeners.get("visibilitychange") || []) callback();
    },
    reloads: () => events.filter(event => event === "reload").length
  };
}

async function settle(predicate, label) {
  for (let attempt = 0; attempt < 60; attempt += 1) {
    await new Promise(resolve => setImmediate(resolve));
    if (predicate()) return;
  }
  throw new Error(`Update UI did not settle: ${label}`);
}

async function unchangedFailureAndDevelopment() {
  const service = new Service();
  const page = browser(service);
  await settle(() => service.manifestReads === 1, "initial version check");
  assert.equal(page.reloads(), 0);
  service.manifestFailure = true;
  page.poll(30000);
  await settle(() => service.manifestReads === 2, "failed version check");
  assert.equal(page.reloads(), 0);
  assert.equal(page.elements.get("notice").hidden, true);
  assert.match(page.elements.get("connection-state").className, /online/);
  assert.equal(service.posts.length, 0);

  const development = new Service();
  development.version = nextVersion;
  const sourcePage = browser(development, {}, false);
  await settle(() => sourcePage.elements.get("profile-select").value === "synthetic", "development state");
  sourcePage.poll(30000);
  sourcePage.visibility(true);
  sourcePage.visibility(false);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(development.manifestReads, 0, "Unpublished source must skip version watching");
  assert.equal(sourcePage.reloads(), 0);
}

async function idleAndDraftUpdates() {
  const idle = new Service();
  idle.version = nextVersion;
  const page = browser(idle);
  await settle(() => page.reloads() === 1, "idle automatic update");
  assert.deepEqual(page.events.slice(-2), ["save", "reload"]);
  page.poll(30000);
  page.poll(5000);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(page.reloads(), 1, "One detected release must schedule one reload");
  assert.equal(idle.posts.length, 0);

  const draft = {source: "合成草稿", english: "Synthetic draft", boundSource: "合成草稿", revision: 4};
  const service = new Service();
  service.version = nextVersion;
  const editing = browser(service, draft);
  await settle(() => !editing.elements.get("update-banner").hidden, "draft update banner");
  assert.equal(editing.reloads(), 0);
  editing.click("update-page-button");
  assert.equal(editing.reloads(), 1);
  assert.equal(editing.draft().source, draft.source);
  assert.equal(editing.draft().english, draft.english);
  assert.deepEqual(editing.events.slice(-2), ["save", "reload"]);
}

async function pendingAndWorkerGuards() {
  const pending = {id: "e".repeat(32), action: "send", revision: 1,
    snapshot: {source: "", english: "", attachment_ids: []},
    payload: {id: "e".repeat(32), action: "send", english: "Synthetic queued text", profile: "synthetic"}};
  const cases = [
    {draft: {pendingJob: pending}},
    {draft: {pendingReplyJob: {...pending, action: "translate_reply", key: "synthetic|hash"}}},
    {draft: {lastUncertain: pending}},
    {draft: {attachments: [{id: "f".repeat(32), name: "synthetic.txt", size: 3}]}},
    {draft: {}, busy: true},
    {draft: {}, active: {id: "a".repeat(32), action: "send", state: "running"}}
  ];
  for (const fixture of cases) {
    const service = new Service();
    service.version = nextVersion;
    service.workerBusy = Boolean(fixture.busy);
    service.activeJob = fixture.active || null;
    const page = browser(service, fixture.draft);
    await settle(() => !page.elements.get("update-banner").hidden, "busy update banner");
    assert.equal(page.reloads(), 0, "Draft/queued/unknown/worker activity must block automatic reload");
    assert.equal(service.posts.length, 0, "Checking a version must never repost a saved task");
  }

  const service = new Service();
  service.version = nextVersion;
  const page = browser(service, {pendingJob: pending});
  await settle(() => !page.elements.get("update-banner").hidden, "queued task manual update");
  page.click("update-page-button");
  assert.equal(page.reloads(), 1);
  assert.equal(page.draft().pendingJob.id, pending.id);
  assert.deepEqual(page.draft().pendingJob.payload, pending.payload);
  assert(service.jobReads.includes(pending.id));
  assert.equal(service.posts.length, 0);
}

async function postingAndUploadGuards() {
  const service = new Service();
  service.deferPost = true;
  const page = browser(service, {source: "合成指令", english: "Synthetic instruction", boundSource: "合成指令"});
  await settle(() => service.manifestReads === 1, "posting fixture startup");
  page.click("send-button");
  await settle(() => Boolean(service.postRelease), "pending send POST");
  service.version = nextVersion;
  page.poll(30000);
  await settle(() => !page.elements.get("update-banner").hidden, "update during POST");
  page.click("update-page-button");
  assert.equal(page.reloads(), 0);
  assert.match(page.elements.get("update-description").textContent, /正在提交或上传/);
  assert.equal(service.posts.length, 1);
  const pendingId = page.draft().pendingJob.id;
  service.postRelease();
  await new Promise(resolve => setImmediate(resolve));
  page.click("update-page-button");
  assert.equal(page.reloads(), 1);
  assert.equal(page.draft().pendingJob.id, pendingId);
  assert.equal(service.posts.length, 1);

  const uploading = new Service();
  uploading.deferUpload = true;
  const uploadPage = browser(uploading);
  await settle(() => uploading.manifestReads === 1, "upload fixture startup");
  const file = new Blob(["synthetic"], {type: "text/plain"});
  file.name = "synthetic.txt";
  uploadPage.elements.get("file-input").files = [file];
  uploadPage.elements.get("file-input").dispatch("change");
  await settle(() => Boolean(uploading.uploadRelease), "upload request in flight");
  uploading.version = nextVersion;
  uploadPage.poll(30000);
  await settle(() => !uploadPage.elements.get("update-banner").hidden, "update during upload");
  uploadPage.click("update-page-button");
  assert.equal(uploadPage.reloads(), 0);
  uploading.uploadRelease();
  await new Promise(resolve => setImmediate(resolve));
  uploadPage.click("update-page-button");
  assert.equal(uploadPage.reloads(), 1);
  assert.equal(uploadPage.draft().attachments[0].name, "synthetic.txt");
  assert.equal(uploading.posts.length, 0);
}

async function visibilityAndMobileMenu() {
  const service = new Service();
  const page = browser(service, {source: "Synthetic retained draft"});
  await settle(() => service.manifestReads === 1, "visibility fixture startup");
  page.visibility(true);
  service.version = nextVersion;
  page.poll(30000);
  assert.equal(service.manifestReads, 1, "Hidden pages pause release checks");
  page.visibility(false);
  await settle(() => service.manifestReads === 2, "visible page release check");
  assert.equal(page.reloads(), 0);
  assert.equal(page.elements.get("update-banner").hidden, false);

  const button = page.elements.get("mobile-menu-button");
  const panel = page.elements.get("connection-panel");
  assert.equal(button.attributes.get("aria-controls"), "connection-panel");
  assert.equal(button.attributes.get("aria-expanded"), "false");
  assert.equal(panel.classes.has("is-open"), false);
  assert.equal(panel.classes.has("menu-collapsible"), true);
  page.click("mobile-menu-button");
  assert.equal(button.attributes.get("aria-expanded"), "true");
  assert.equal(panel.classes.has("is-open"), true);
  page.click("mobile-menu-button");
  assert.equal(button.attributes.get("aria-expanded"), "false");
  assert.equal(panel.classes.has("is-open"), false);
  assert.equal((html.match(/role="tab"/g) || []).length, 3);
}

async function staleReleaseDoesNotLoop() {
  const session = new Map();
  const service = new Service();
  service.version = currentVersion;
  // A partially published release has the new JS while its manifest still advertises the old build.
  const first = browser(service, {}, true, {session, buildVersion: nextVersion});
  await settle(() => first.reloads() === 1, "first mismatched manifest reload");
  const stale = browser(service, first.draft(), true, {session, buildVersion: nextVersion});
  await settle(() => !stale.elements.get("update-banner").hidden, "stale manifest manual update banner");
  await new Promise(resolve => setImmediate(resolve));
  stale.poll(30000);
  stale.poll(5000);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(stale.reloads(), 0, "The same target version may automatically reload only once per session");
  assert.match(stale.elements.get("update-description").textContent, /点击.*更新页面/);
  service.version = "c".repeat(64);
  stale.poll(30000);
  await settle(() => stale.reloads() === 1, "a different release permits one new automatic reload");

  const noSession = new Service();
  noSession.version = nextVersion;
  const unavailable = browser(noSession, {}, true, {session: null});
  await settle(() => !unavailable.elements.get("update-banner").hidden, "session storage unavailable");
  assert.equal(unavailable.reloads(), 0);
  unavailable.click("update-page-button");
  assert.equal(unavailable.reloads(), 1, "Explicit updates remain available without session storage");

  const savedAttempts = new Map();
  const cannotSave = new Service();
  cannotSave.version = nextVersion;
  const unsaved = browser(cannotSave, {}, true, {session: savedAttempts, storageFails: true});
  await settle(() => /未能保存草稿/.test(unsaved.elements.get("update-description").textContent), "draft storage failure");
  assert.equal(unsaved.reloads(), 0);
  assert.equal(savedAttempts.size, 0, "Do not record an automatic attempt if saving the draft failed");
}

async function olderHtmlRemainsUsable() {
  const service = new Service();
  const page = browser(service, {}, true, {oldHtml: true});
  assert.equal(page.elements.has("mobile-menu-button"), false);
  assert.equal(page.elements.has("connection-panel"), false);
  assert.equal(page.elements.has("update-banner"), false);
  await settle(() => page.elements.get("profile-select").value === "synthetic", "new JS with old HTML");
  assert.equal(page.elements.get("app-view").hidden, false);
  assert.equal(page.connectionFallback.classes.has("menu-collapsible"), false, "Without the menu control, keep the existing connection strip visible");
  page.elements.get("source").value = "Synthetic source";
  page.elements.get("source").dispatch("input");
  page.elements.get("english").value = "Synthetic English";
  page.elements.get("english").dispatch("input");
  page.click("send-button");
  await settle(() => service.posts.length === 1, "send remains bound with older HTML");
  assert.equal(service.posts[0].action, "send");
  service.version = nextVersion;
  page.poll(30000);
  await settle(() => /发现新版本/.test(page.elements.get("notice").textContent), "fallback update notice");
  assert.equal(page.reloads(), 0);
  assert.match(page.elements.get("connection-state").className, /online/);
  assert.equal(service.posts.length, 1);
}

(async () => {
  assert.equal((script.match(/__BRIDGE_BUILD__/g) || []).length, 1, "The publisher token must occur exactly once");
  await unchangedFailureAndDevelopment();
  await idleAndDraftUpdates();
  await pendingAndWorkerGuards();
  await postingAndUploadGuards();
  await visibilityAndMobileMenu();
  await staleReleaseDoesNotLoop();
  await olderHtmlRemainsUsable();
  console.log("Mobile updates UI passed: quiet manifest checks, one idle reload, draft/pending preservation, POST/upload guards, visibility checks, development skip, accessible mobile menu, stale-release loop protection, and mixed-version HTML compatibility");
})().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
