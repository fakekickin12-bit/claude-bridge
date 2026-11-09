"use strict";

// Runs the browser script with an isolated DOM and API. No model or SSH calls.
const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const web = path.join(__dirname, "..", "mobile_web");
const appSource = fs.readFileSync(path.join(web, "app.js"), "utf8");
const htmlSource = fs.readFileSync(path.join(web, "index.html"), "utf8");
const storageKey = "claude-bridge.mobile.v1";
const profileId = "OpenAI 赠送额度";
const originalReply = "A synthetic assistant reply.";
const chineseReply = "一条合成的助手回复。";
const sourceHash = crypto.createHash("sha256").update(originalReply).digest("hex");

function copy(value) {
  return JSON.parse(JSON.stringify(value));
}

function response(value, status = 200) {
  return {ok: status >= 200 && status < 300, status, json: async () => copy(value)};
}

class Element {
  constructor(id = "", tagName = "div") {
    this.id = id;
    this.tagName = tagName;
    this.value = "";
    this.textContent = "";
    this.hidden = false;
    this.disabled = false;
    this.checked = true;
    this.dataset = {};
    this.children = [];
    this.options = [];
    this.attributes = new Map();
    this.listeners = new Map();
    this.parentElement = {scrollHeight: 0, scrollTop: 0, clientHeight: 0};
    const classes = new Set();
    this.classList = {
      add: name => classes.add(name),
      remove: name => classes.delete(name),
      toggle(name, active) {
        if (active) classes.add(name);
        else classes.delete(name);
      }
    };
  }

  addEventListener(type, listener) {
    if (!this.listeners.has(type)) this.listeners.set(type, []);
    this.listeners.get(type).push(listener);
  }

  dispatch(type) {
    for (const listener of this.listeners.get(type) || []) listener({preventDefault() {}});
  }

  setAttribute(name, value) {
    this.attributes.set(name, value);
  }

  replaceChildren(...children) {
    this.children = children;
    if (this.id === "profile-select") this.options = children;
  }

  append(...children) {
    this.children.push(...children);
  }

  querySelectorAll() {
    return [];
  }

  focus() {}
}

class MockService {
  constructor(mode = "immediate") {
    this.mode = mode;
    this.jobs = new Map();
    this.cache = new Map();
    this.posts = [];
    this.jobReads = [];
    this.paidCalls = 0;
    this.activeJob = null;
    this.postRelease = null;
    this.profileLabel = "OpenAI-api";
    this.translationRevision = "";
    this.effort = "low";
    this.translationText = chineseReply;
  }

  state() {
    return {
      csrf: "synthetic-csrf", profiles: [{id: profileId, label: this.profileLabel, model: "synthetic-model", kind: "api",
        ...(this.translationRevision ? {translation_revision: this.translationRevision, reasoning_effort: this.effort} : {})}],
      default_profile: profileId,
      target: {ready: true, mode: "Code", title: "Synthetic task", context: "a".repeat(64)},
      reply: {text: originalReply, source_hash: sourceHash, mode: "Code", generating: false, updated_at: 1},
      worker: {online: true, busy: Boolean(this.activeJob)}, active_job: this.activeJob
    };
  }

  complete(payload, revision = this.translationRevision, text = this.translationText) {
    const key = `${payload.profile}|${revision ? `${revision}|` : ""}${payload.source_hash}`;
    const cached = this.cache.has(key);
    if (!cached) this.paidCalls += 1;
    this.cache.set(key, text);
    const job = {
      id: payload.id, action: "translate_reply", profile: payload.profile,
      state: "translated", result: {text, source_hash: payload.source_hash, cached,
        ...(revision ? {translation_revision: revision} : {})}
    };
    this.jobs.set(job.id, job);
    this.activeJob = null;
    return job;
  }

  async fetch(url, options) {
    if (url === "/api/state") return response(this.state());
    if (url === "/api/history") return response({jobs: []});
    if (url === "/api/jobs" && options.method === "POST") {
      const payload = JSON.parse(options.body);
      assert.equal(payload.action, "translate_reply");
      assert.equal(payload.profile, profileId, "Use profile ID, not its display label");
      assert.equal(payload.source_hash, sourceHash);
      this.posts.push(copy(payload));
      const queued = {id: payload.id, action: payload.action, profile: payload.profile, state: "queued"};
      this.jobs.set(payload.id, queued);
      if (this.mode === "deferred") {
        return new Promise(resolve => {
          this.postRelease = () => {
            this.complete(payload);
            resolve(response(queued));
          };
        });
      }
      if (this.mode === "queued") return response(queued);
      return response(this.complete(payload));
    }
    if (url.startsWith("/api/jobs/")) {
      const id = url.split("/").pop();
      this.jobReads.push(id);
      return this.jobs.has(id) ? response(this.jobs.get(id)) : response({detail: "Missing synthetic job"}, 404);
    }
    throw new Error(`Unexpected synthetic request ${options.method} ${url}`);
  }
}

function browser(service, storage = new Map()) {
  const elements = new Map();
  for (const match of htmlSource.matchAll(/\bid="([^"]+)"/g)) elements.set(match[1], new Element(match[1]));
  const tabs = ["compose", "reply", "history"].map(tab => {
    const element = new Element();
    element.dataset.tab = tab;
    return element;
  });
  const intervals = new Map();
  const workspace = new Element();
  const document = {
    hidden: false,
    getElementById(id) {
      assert(elements.has(id), `Missing DOM element ${id}`);
      return elements.get(id);
    },
    querySelector: () => workspace,
    querySelectorAll: selector => selector === "[data-tab]" ? tabs : [],
    addEventListener() {},
    createElement: tagName => new Element("", tagName),
    createElementNS: (_, tagName) => new Element("", tagName),
    createTextNode: value => value
  };
  let intervalId = 0;
  const context = {
    console, Headers, FormData, URLSearchParams, AbortController, Intl, Date,
    crypto: crypto.webcrypto, setTimeout, clearTimeout,
    setInterval(callback, milliseconds) {
      intervals.set(++intervalId, {callback, milliseconds});
      return intervalId;
    },
    clearInterval: id => intervals.delete(id),
    localStorage: {getItem: key => storage.get(key) || null, setItem: (key, value) => storage.set(key, value)},
    document,
    window: {location: {hash: "", pathname: "/", search: ""}, history: {replaceState() {}}, addEventListener() {}},
    fetch: (url, options) => service.fetch(url, options)
  };
  vm.createContext(context);
  vm.runInContext(appSource, context, {filename: "mobile_web/app.js"});
  return {
    elements, storage,
    click: id => elements.get(id).dispatch("click"),
    poll(milliseconds) {
      for (const interval of intervals.values()) if (interval.milliseconds === milliseconds) interval.callback();
    },
    draft: () => JSON.parse(storage.get(storageKey) || "{}")
  };
}

async function settle(predicate, label) {
  for (let attempt = 0; attempt < 50; attempt += 1) {
    await new Promise(resolve => setImmediate(resolve));
    if (predicate()) return;
  }
  throw new Error(`UI did not settle: ${label}`);
}

async function slowPostDoesNotStopPolling() {
  const service = new MockService("deferred");
  const page = browser(service);
  await settle(() => page.elements.get("profile-select").value === profileId, "initial state");
  page.click("translate-reply-button");
  await settle(() => Boolean(service.postRelease), "pending POST");
  assert.equal(page.elements.get("reply-status").textContent, "正在翻译回复…");
  page.poll(1000);
  page.poll(1000);
  assert.equal(service.jobReads.length, 0, "Do not query a request while its POST is in flight");
  service.postRelease();
  await settle(() => service.jobs.get(service.posts[0].id)?.state === "translated", "server completion");
  page.poll(1000);
  await settle(() => page.elements.get("reply-text").textContent === chineseReply, "reply poll resumes after POST");
  assert.equal(page.draft().pendingReplyJob, null);
  assert.equal(page.elements.get("reply-status").textContent, "");
  assert.equal(service.paidCalls, 1);
  page.click("translate-reply-button");
  assert.equal(page.elements.get("reply-text").textContent, originalReply);
  page.click("translate-reply-button");
  assert.equal(page.elements.get("reply-text").textContent, chineseReply);
  assert.equal(service.posts.length, 1, "Toggle should reuse the completed result");
  service.profileLabel = "OpenAI-api renamed";
  page.poll(5000);
  await settle(() => page.elements.get("profile-select").options[0].textContent === service.profileLabel, "label refresh");
  assert.equal(page.elements.get("reply-text").textContent, chineseReply);
  assert.equal(service.posts.length, 1, "Changing label must preserve the ID-keyed translation");
}

async function immediateCachedResultRenders() {
  const service = new MockService();
  service.cache.set(`${profileId}|${sourceHash}`, chineseReply);
  const storage = new Map([[storageKey, JSON.stringify({profile: profileId, replyTranslated: true})]]);
  const page = browser(service, storage);
  await settle(() => page.elements.get("reply-text").textContent === chineseReply, "immediate cached POST result");
  assert.equal(page.draft().pendingReplyJob, null);
  assert.equal(service.jobReads.length, 0);
  assert.equal(service.paidCalls, 0);
  assert.equal(page.elements.get("translate-reply-button").attributes.get("aria-pressed"), "true");
}

async function reloadResumesExistingJob() {
  const service = new MockService("queued");
  const first = browser(service);
  await settle(() => first.elements.get("profile-select").value === profileId, "reload fixture state");
  first.click("translate-reply-button");
  await settle(() => service.posts.length === 1, "queued reply translation");
  const pendingId = first.draft().pendingReplyJob.id;
  service.complete(service.posts[0]);
  const reloaded = browser(service, first.storage);
  await settle(() => reloaded.elements.get("reply-text").textContent === chineseReply, "restored pending job");
  assert(service.jobReads.includes(pendingId));
  assert.equal(service.posts.length, 1, "Reload must query the saved job ID without posting another task");
  assert.equal(reloaded.draft().pendingReplyJob, null);
  assert.equal(service.paidCalls, 1);

  // Completed results may be fetched again after a full reload; the server cache must be reused.
  service.mode = "immediate";
  const finishedReload = browser(service, first.storage);
  await settle(() => finishedReload.elements.get("reply-text").textContent === chineseReply, "completed reload uses server cache");
  assert.equal(service.paidCalls, 1, "The same profile ID and source hash must reuse the server translation");
  finishedReload.click("translate-reply-button");
  assert.equal(finishedReload.elements.get("reply-text").textContent, originalReply);
}

async function changedEffortInvalidatesSameProfileReplyCache() {
  const service = new MockService();
  service.translationRevision = "1".repeat(64);
  service.translationText = "Low 的译文";
  const storage = new Map([[storageKey, JSON.stringify({profile: profileId, replyTranslated: true})]]);
  const page = browser(service, storage);
  await settle(() => page.elements.get("reply-text").textContent === "Low 的译文", "Low translation");
  assert(page.elements.get("profile-model").textContent.includes("推理 Low"));
  service.translationRevision = "2".repeat(64);
  service.effort = "high";
  service.translationText = "High 的译文";
  service.mode = "queued";
  page.poll(5000);
  await settle(() => service.posts.length === 2, "changed effort requests its own reply translation");
  assert(page.elements.get("profile-model").textContent.includes("推理 High"));
  assert.equal(page.elements.get("reply-text").textContent, originalReply, "Do not show the old Low cache as High");
  service.complete(service.posts[1]);
  page.poll(1000);
  await settle(() => page.elements.get("reply-text").textContent === "High 的译文", "High translation");
  assert(service.posts.every(payload => payload.action === "translate_reply"), "A settings change must never resubmit an instruction");
}

async function lateReplyRetainsItsActualSettingsRevision() {
  const service = new MockService("queued");
  const low = "1".repeat(64), high = "2".repeat(64);
  service.translationRevision = low;
  const storage = new Map([[storageKey, JSON.stringify({profile: profileId, replyTranslated: true})]]);
  const page = browser(service, storage);
  await settle(() => service.posts.length === 1, "pending Low translation");
  service.translationRevision = high;
  service.effort = "high";
  page.poll(5000);
  await settle(() => page.elements.get("profile-model").textContent.includes("推理 High"), "High settings while Low runs");
  service.complete(service.posts[0], low, "迟到的 Low 译文");
  page.poll(1000);
  await settle(() => service.posts.length === 2, "High waits for old job then translates once");
  assert.equal(page.elements.get("reply-text").textContent, originalReply);
  service.complete(service.posts[1], high, "当前 High 译文");
  page.poll(1000);
  await settle(() => page.elements.get("reply-text").textContent === "当前 High 译文", "Current result displays");
  assert.equal(service.posts.length, 2);
}

async function executionTimeRevisionCanSatisfyTheNewSettings() {
  const service = new MockService("queued");
  service.translationRevision = "1".repeat(64);
  const storage = new Map([[storageKey, JSON.stringify({profile: profileId, replyTranslated: true})]]);
  const page = browser(service, storage);
  await settle(() => service.posts.length === 1, "queued before configuration save");
  service.translationRevision = "2".repeat(64);
  service.effort = "high";
  page.poll(5000);
  await settle(() => page.elements.get("profile-model").textContent.includes("推理 High"), "New execution-time settings");
  service.complete(service.posts[0], service.translationRevision, "实际使用 High 的译文");
  page.poll(1000);
  await settle(() => page.elements.get("reply-text").textContent === "实际使用 High 的译文", "Use server-reported actual revision");
  assert.equal(service.posts.length, 1, "Do not repeat a translation that already used the current settings");
}

async function latestReadStatesAreExplicitWithoutPaidCalls() {
  class ReadService extends MockService {
    constructor(reply) { super(); this.readReply = reply; }
    state() { return {...super.state(), reply: this.readReply}; }
  }
  const context = "a".repeat(64);
  const empty = new ReadService({text: "", source_hash: "", mode: "Code", context, generating: false,
    updated_at: 1, loading: false, read_status: "empty", read_reason: "当前会话尚无 Claude 回复。"});
  const page = browser(empty, new Map([[storageKey, JSON.stringify({profile: profileId, replyTranslated: true})]]));
  await settle(() => page.elements.get("reply-status").textContent === "当前会话尚无 Claude 回复。", "Explicit empty latest reply");
  assert.equal(page.elements.get("reply-empty").textContent, "当前会话尚无 Claude 回复。");
  assert.equal(empty.posts.length, 0);

  const failure = new ReadService({text: "", source_hash: "", mode: "Code", context, generating: false,
    updated_at: 0, loading: false, read_status: "error", error: "最新回复读取超时，请刷新重试。"});
  const storage = new Map([[storageKey, JSON.stringify({profile: profileId, replyTranslated: true,
    replyNavigation: {id: "f".repeat(32), context, action: "match_current", show_reply: true, requires_fresh: true}})]]);
  const failed = browser(failure, storage);
  await settle(() => failed.elements.get("reply-status").textContent === "最新回复读取超时，请刷新重试。", "Matched task read failure is not endless loading");
  assert.equal(failed.elements.get("reply-empty").textContent, "最新回复读取超时，请刷新重试。");
  assert.equal(failure.posts.length, 0);
}

(async () => {
  await slowPostDoesNotStopPolling();
  await immediateCachedResultRenders();
  await reloadResumesExistingJob();
  await changedEffortInvalidatesSameProfileReplyCache();
  await lateReplyRetainsItsActualSettingsRevision();
  await executionTimeRevisionCanSatisfyTheNewSettings();
  await latestReadStatesAreExplicitWithoutPaidCalls();
  console.log("Mobile reply UI passed: latest empty/error states, delayed polling, cached/reloaded results, stable profile ID, effort revisions, late-result correlation, no instruction resubmission");
})().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
