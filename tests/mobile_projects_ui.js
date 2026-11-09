"use strict";

// Uses a fake browser and API. No real Ubuntu or model calls.
const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const web = path.join(__dirname, "..", "mobile_web");
const html = fs.readFileSync(path.join(web, "index.html"), "utf8");
const script = fs.readFileSync(path.join(web, "app.js"), "utf8");
const storageKey = "claude-bridge.mobile.v1";
const projectOne = "a".repeat(64);
const projectTwo = "b".repeat(64);
const sessionOne = "e".repeat(64);
const sessionTwo = "f".repeat(64);
const duplicateSession = "1".repeat(64);

function clone(value) { return JSON.parse(JSON.stringify(value)); }
function response(value) { return {ok: true, status: 200, json: async () => clone(value)}; }

class Element {
  constructor(id = "") {
    Object.assign(this, {id, value: "", textContent: "", hidden: false, disabled: false, checked: true,
      dataset: {}, children: [], options: [], files: [], clicks: 0});
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
  click() { this.clicks += 1; this.dispatch("click"); }
  setAttribute(name, value) { this.attributes.set(name, value); }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) {
    this.children = children;
    if (["profile-select", "project-select", "session-select"].includes(this.id)) this.options = children;
  }
  querySelectorAll() { return []; }
  focus() {}
}

class ProjectService {
  constructor() {
    this.catalogReads = 0;
    this.catalogUrls = [];
    this.loadingReads = 0;
    this.nativeAttachments = undefined;
    this.posts = [];
    this.jobs = new Map();
    this.context = "c".repeat(64);
    this.mode = "Code";
    this.deferPost = false;
    this.postRelease = null;
    this.uploadRelease = null;
    this.ready = false;
    this.delayReply = false;
    this.nextJobState = "submitted";
    this.deferState = false;
    this.stateRelease = null;
    this.jobReads = 0;
    this.sessionReads = [];
    this.currentTitle = "Synthetic original task";
    this.matchContext = "7".repeat(64);
    this.projectTitle = "";
    this.reply = {context: this.context, text: "Synthetic original reply", source_hash: "original-reply", updated_at: 1, mode: "Code"};
    this.sessions = {
      [projectOne]: [
        {id: "3".repeat(64), title: "Synthetic latest project-one task", active: false, selectable: true},
        {id: "5".repeat(64), title: "Synthetic older project-one task", active: false, selectable: true}
      ],
      [projectTwo]: [
        {id: sessionOne, title: "Synthetic task one", active: false, selectable: true},
        {id: sessionTwo, title: "Synthetic current task", active: true, selectable: true},
        {id: duplicateSession, title: "Duplicate task", active: false, selectable: false, reason: "同名会话，请在 Ubuntu 确认"}
      ]
    };
    this.catalog = {
      context: this.context, catalog_id: "d".repeat(64), mode: "Code",
      scope: "projects", selected_project_id: "",
      active_project_id: projectTwo, active_session_id: sessionTwo,
      projects: [
        {id: projectOne, title: "project-one", expanded: false, selectable: true, active: false, sessions_loaded: false, sessions: []},
        {id: projectTwo, title: "project-two", expanded: true, selectable: true, active: true, sessions_loaded: false, sessions: []},
        {id: "2".repeat(64), title: "Duplicate project", expanded: false, selectable: false, reason: "项目名称重复", sessions_loaded: false, sessions: []}
      ]
    };
  }

  state() {
    return {csrf: "synthetic-csrf", profiles: [{id: "synthetic", label: "Synthetic API", kind: "api", model: "synthetic"}],
      default_profile: "synthetic", target: {mode: this.mode, ready: this.ready, busy: true, reason: "Synthetic inline question", context: this.context, title: this.currentTitle, project_title: this.projectTitle,
        capabilities: {native_attachments: this.nativeAttachments}},
      worker: {online: true, busy: false}, reply: clone(this.reply), active_job: null};
  }

  publishReply() {
    this.reply = {context: this.context, text: `Latest reply for ${this.currentTitle}`, source_hash: `reply-${this.context}`, updated_at: 100, mode: "Code", generating: false};
  }

  complete(payload) {
    if (payload.action === "match_current") {
      this.context = this.matchContext;
      this.currentTitle = "Synthetic native current task";
      this.projectTitle = "sample-project";
      if (!this.delayReply) this.publishReply();
      const state = this.nextJobState;
      this.nextJobState = "submitted";
      const job = {id: payload.id, action: payload.action, state, phase: "已匹配当前对话",
        error: state === "unknown" ? "Synthetic readonly SSH timeout" : "",
        result: state === "submitted" ? {matched: true, context: this.context,
          target: {...this.state().target, project_title: "sample-project"}, session_title: this.currentTitle} : {}};
      this.jobs.set(job.id, job);
      return job;
    }
    if (payload.action === "new") {
      this.context = "6".repeat(64);
      this.currentTitle = "Synthetic new task";
      this.publishReply();
      const job = {id: payload.id, action: payload.action, state: "submitted", result: {target: this.state().target}};
      this.jobs.set(job.id, job);
      return job;
    }
    const project = this.catalog.projects.find(item => item.id === payload.project_id);
    assert(project?.selectable);
    const sessions = this.sessions[project.id];
    if (payload.action === "open_project") this.sessionReads.push(project.id);
    const session = payload.action === "open_project" ? sessions.find(item => item.active && item.selectable) || sessions.find(item => item.selectable) :
      sessions.find(item => item.id === payload.session_id && item.selectable);
    assert(session);
    this.catalog.active_project_id = project.id;
    this.catalog.active_session_id = session.id;
    this.context = crypto.createHash("sha256").update(session.id).digest("hex");
    this.catalog.context = this.context;
    this.currentTitle = session.title;
    this.catalog.catalog_id = crypto.createHash("sha256").update(this.context + "headers").digest("hex");
    this.sessionCatalogId = crypto.createHash("sha256").update(this.context + "sessions").digest("hex");
    if (!this.delayReply) this.publishReply();
    const state = this.nextJobState;
    this.nextJobState = "submitted";
    const job = {id: payload.id, action: payload.action, state, error: state === "unknown" ? "Synthetic SSH timeout" : "", result: state === "submitted" ? {
      ...clone(this.catalog), scope: "sessions", selected_project_id: project.id, session_catalog_id: this.sessionCatalogId,
      projects: this.catalog.projects.map(item => ({...clone(item), sessions_loaded: item.id === project.id,
        sessions: item.id === project.id ? clone(sessions).map(entry => ({...entry, active: entry.id === session.id})) : []})),
      expanded: true, switched: true, target: this.state().target
    } : {}};
    this.jobs.set(job.id, job);
    return job;
  }

  async fetch(url, options) {
    if (url === "/api/state") {
      const snapshot = this.state();
      if (this.deferState) {
        this.deferState = false;
        return new Promise(resolve => { this.stateRelease = () => resolve(response(snapshot)); });
      }
      return response(snapshot);
    }
    if (url === "/api/history") return response({jobs: []});
    if (url.startsWith("/api/projects")) {
      this.catalogReads += 1;
      this.catalogUrls.push(url);
      if (url === "/api/projects?refresh=true") this.loadingReads = 2;
      if (this.loadingReads > 0) {
        this.loadingReads -= 1;
        return response({projects: [], loading: true, reason: "正在读取 Ubuntu 项目…"});
      }
      return response({...this.catalog, loading: false});
    }
    if (url.startsWith("/api/jobs/")) {
      this.jobReads += 1;
      return response(this.jobs.get(url.split("/").pop()));
    }
    if (url === "/api/jobs") {
      const payload = JSON.parse(options.body);
      assert(["new", "open_project", "switch_project", "match_current"].includes(payload.action));
      if (payload.action === "match_current") {
        assert.deepEqual(Object.keys(payload).sort(), ["action", "id"], "Matching must be a read-only request independent of the stale Bridge target or draft");
      } else {
        assert.equal(payload.expected_context, this.catalog.context);
        if (payload.action !== "new") assert.equal(payload.catalog_id, this.catalog.catalog_id);
      }
      for (const field of ["source", "english", "attachment_ids", "profile", "auto_enter"]) {
        assert.equal(Object.hasOwn(payload, field), false, `Navigation must omit ${field}`);
      }
      if (payload.action === "open_project") assert.equal(Object.hasOwn(payload, "session_id"), false);
      if (payload.action === "switch_project") assert.equal(payload.session_catalog_id, this.sessionCatalogId);
      this.posts.push(payload);
      if (this.deferPost) return new Promise(resolve => { this.postRelease = () => resolve(response(this.complete(payload))); });
      return response(this.complete(payload));
    }
    if (url === "/api/uploads") return new Promise(resolve => {
      this.uploadRelease = () => resolve(response({id: "4".repeat(32), name: "synthetic.txt", kind: "file", size: 9}));
    });
    throw new Error(`Unexpected mock request ${url}`);
  }
}

function browser(service, options = {}) {
  const elements = new Map();
  for (const match of html.matchAll(/<([a-z][a-z0-9-]*)\b([^>]*\bid="([^"]+)"[^>]*)>/g)) {
    const element = new Element(match[3]);
    element.hidden = /\bhidden\b/.test(match[2]);
    elements.set(element.id, element);
  }
  const initial = {source: "合成待发送草稿", english: "Synthetic unsent draft", boundSource: "合成待发送草稿", revision: 1, ...options.initial};
  const storage = options.storage || new Map([[storageKey, JSON.stringify(initial)]]);
  const intervals = new Map();
  const projectTimeouts = new Map();
  let timeoutId = 100000;
  const documentListeners = new Map();
  const openedPages = [];
  const tabs = ["compose", "reply", "history"].map(tab => { const item = new Element(); item.dataset.tab = tab; return item; });
  const workspace = new Element();
  workspace.dataset.activeTab = "compose";
  const context = {
    console, Headers, FormData, URLSearchParams, AbortController, Intl, Date, crypto: crypto.webcrypto,
    setTimeout(callback, milliseconds) {
      if (milliseconds === 1000) {
        projectTimeouts.set(++timeoutId, callback);
        return timeoutId;
      }
      return setTimeout(callback, milliseconds);
    },
    clearTimeout(id) { if (!projectTimeouts.delete(id)) clearTimeout(id); },
    setInterval(callback, milliseconds) { intervals.set(milliseconds, callback); return milliseconds; },
    clearInterval: id => intervals.delete(id),
    localStorage: {getItem: key => storage.get(key) || null, setItem: (key, value) => storage.set(key, value)},
    document: {
      hidden: false, getElementById: id => elements.get(id) || null, querySelector: () => workspace,
      querySelectorAll: selector => selector === "[data-tab]" ? tabs : [],
      addEventListener(type, callback) {
        if (!documentListeners.has(type)) documentListeners.set(type, []);
        documentListeners.get(type).push(callback);
      },
      createElement: () => new Element(), createElementNS: () => new Element(), createTextNode: text => text
    },
    window: {location: {hash: "", pathname: "/", search: ""}, history: {replaceState() {}}, addEventListener() {},
      open: (...args) => openedPages.push(args)},
    fetch: (url, options) => service.fetch(url, options)
  };
  vm.createContext(context);
  vm.runInContext(script, context);
  return {
    elements, initial, storage, workspace, openedPages, draft: () => JSON.parse(storage.get(storageKey)),
    click: id => elements.get(id).click(),
    choose(id, value) { elements.get(id).value = value; elements.get(id).dispatch("change"); },
    poll(milliseconds) { intervals.get(milliseconds)?.(); },
    pollProjects() {
      for (const [id, callback] of [...projectTimeouts]) {
        projectTimeouts.delete(id);
        callback();
      }
    },
    visibility(hidden) {
      context.document.hidden = hidden;
      for (const callback of documentListeners.get("visibilitychange") || []) callback();
    }
  };
}

async function settle(predicate, label) {
  for (let attempt = 0; attempt < 60; attempt += 1) {
    await new Promise(resolve => setImmediate(resolve));
    if (predicate()) return;
  }
  throw new Error(`Project UI did not settle: ${label}`);
}

async function explicitProjectAndSessionNavigation() {
  const service = new ProjectService();
  const attachment = {id: "7".repeat(32), name: "synthetic-retained.txt", kind: "file", size: 9};
  const page = browser(service, {initial: {attachments: [attachment]}});
  await settle(() => page.elements.get("profile-select").value === "synthetic", "initial state");
  assert.equal(page.elements.get("projects-button").disabled, false, "Code generation/questions must not disable browsing projects");
  assert.equal(service.catalogReads, 0, "Read a project catalog only after the user opens it");
  page.click("projects-button");
  await settle(() => page.elements.get("project-select").options.length === 4, "catalog rendered");
  assert.equal(page.elements.get("project-select").value, "", "Opening the directory must not select or open the active project");
  assert.equal(page.elements.get("session-select").value, "");
  assert.equal(page.elements.get("session-picker").hidden, true, "The first catalog contains project names only");
  assert.equal(page.elements.get("enter-session-button").disabled, true);
  assert.deepEqual(service.sessionReads, [], "Reading project headers must not inspect any project's sessions");
  assert.equal(service.posts.length, 0);
  assert(page.elements.get("project-select").options.some(option => option.disabled && /项目名称重复/.test(option.textContent)));
  assert.equal(page.elements.get("session-select").options.length, 1);

  page.choose("project-select", projectOne);
  await settle(() => service.posts.length === 1 && !page.draft().pendingJob && !page.elements.get("session-picker").hidden, "open selected project and its latest session");
  assert.equal(service.posts[0].action, "open_project");
  assert.equal(service.posts[0].project_id, projectOne);
  assert.equal(Object.hasOwn(service.posts[0], "session_id"), false);
  assert.equal(service.catalogReads, 1, "Use the selected-project job result; do not overwrite its sessions with a header-only GET");
  assert.deepEqual(service.sessionReads, [projectOne]);
  assert.equal(page.elements.get("session-select").value, "3".repeat(64), "The helper opens the active or latest session");
  assert.equal(page.elements.get("session-select").options.length, 3, "Only the selected project's sessions are present");
  assert.equal(page.workspace.dataset.activeTab, "reply");
  assert.equal(page.elements.get("reply-text").textContent, "Latest reply for Synthetic latest project-one task");
  assert.equal(page.draft().source, page.initial.source);
  assert.equal(page.draft().english, page.initial.english);
  assert.deepEqual(page.draft().attachments, [attachment]);

  page.choose("project-select", projectTwo);
  await settle(() => service.posts.length === 2 && !page.draft().pendingJob && page.elements.get("session-select").value === sessionTwo, "open another selected project");
  assert.deepEqual(service.sessionReads, [projectOne, projectTwo]);
  assert.equal(service.posts[1].action, "open_project", "Even an expanded native group must be explicitly opened once");
  assert.equal(service.catalogReads, 1);
  assert(page.elements.get("session-select").options.some(option => option.disabled && /同名会话/.test(option.textContent)));
  page.choose("session-select", duplicateSession);
  assert.equal(page.elements.get("enter-session-button").disabled, true);
  page.click("enter-session-button");
  assert.equal(service.posts.length, 2, "Invalid/duplicate choices must not enqueue navigation");
  page.choose("session-select", sessionOne);
  assert.equal(page.elements.get("enter-session-button").disabled, false);
  assert.equal(service.posts.length, 2, "Selecting a session still requires the Enter button");
  const sessionCatalogId = service.sessionCatalogId;
  page.click("enter-session-button");
  await settle(() => service.posts.length === 3 && !page.draft().pendingJob, "explicit session navigation");
  assert.equal(service.posts[2].project_id, projectTwo);
  assert.equal(service.posts[2].session_id, sessionOne);
  assert.equal(service.posts[2].session_catalog_id, sessionCatalogId);
  assert.equal(service.posts[2].action, "switch_project");
  assert.equal(page.draft().source, page.initial.source);
  assert.equal(page.draft().english, page.initial.english);
  assert.equal(page.elements.get("source").value, page.initial.source);
  assert.equal(page.elements.get("english").value, page.initial.english);
  assert.deepEqual(page.draft().attachments, [attachment]);
  page.click("projects-refresh-button");
  await settle(() => service.catalogReads === 2, "refresh headers after selected project");
  page.pollProjects();
  await settle(() => service.catalogReads === 3, "first refreshed header poll");
  page.pollProjects();
  await settle(() => service.catalogReads === 4 && !page.elements.get("project-select").disabled, "refreshed headers");
  assert.equal(page.elements.get("project-select").value, "");
  assert.equal(page.elements.get("session-picker").hidden, true);
  assert.equal(service.posts.length, 3, "Filling selections or refreshing headers must not trigger another open");
}

async function pendingBlocksNavigationAndCodeExplainsAttachments() {
  const service = new ProjectService();
  const page = browser(service);
  await settle(() => page.elements.get("profile-select").value === "synthetic", "attachment fixture state");
  assert.equal(page.elements.get("attach-button").disabled, false);
  page.click("attach-button");
  assert.match(page.elements.get("notice").textContent, /Code.*附件/);
  assert.equal(page.elements.get("file-input").clicks, 0, "An unsupported Code attachment must show a reason instead of opening an unusable picker");
  page.click("projects-button");
  await settle(() => page.elements.get("project-select").options.length === 4, "navigation fixture catalog");
  service.deferPost = true;
  page.choose("project-select", projectOne);
  await settle(() => Boolean(service.postRelease), "navigation POST in flight");
  for (const id of ["projects-button", "project-select", "session-select", "enter-session-button"]) {
    assert.equal(page.elements.get(id).disabled, true, `${id} must be disabled during a pending navigation`);
  }
  page.choose("project-select", projectTwo);
  assert.equal(service.posts.length, 1);
  service.postRelease();
  await settle(() => !page.draft().pendingJob && !page.elements.get("projects-button").disabled, "navigation finished");

  service.mode = "Chat";
  page.poll(5000);
  await settle(() => page.elements.get("attach-button").title === "载入附件", "Chat attachment support");
  page.click("attach-button");
  assert.equal(page.elements.get("file-input").clicks, 1);
  const file = new Blob(["synthetic"], {type: "text/plain"});
  file.name = "synthetic.txt";
  page.elements.get("file-input").files = [file];
  page.elements.get("file-input").dispatch("change");
  await settle(() => Boolean(service.uploadRelease), "Chat upload pending");
  assert.equal(page.elements.get("projects-button").disabled, true, "Uploading must block navigation");
  service.uploadRelease();
  await settle(() => page.draft().attachments?.length === 1, "Chat upload completion");
  assert.equal(page.draft().source, page.initial.source);
}

async function unknownIgnoresInflightOldState() {
  const service = new ProjectService();
  service.nextJobState = "unknown";
  const page = browser(service);
  await settle(() => page.elements.get("reply-text").textContent === "Synthetic original reply", "old-state fixture");
  page.click("projects-button");
  await settle(() => page.elements.get("project-select").options.length === 4, "old-state headers");
  service.deferState = true;
  page.poll(5000);
  await settle(() => Boolean(service.stateRelease), "state request began before navigation");
  page.choose("project-select", projectOne);
  await settle(() => service.posts.length === 1 && !page.draft().pendingJob, "unknown with old state in flight");
  service.stateRelease();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(page.elements.get("reply-text").textContent, "", "A state request from before the unknown outcome must not reveal the old reply");
  assert.equal(page.workspace.dataset.activeTab, "compose");
  page.poll(5000);
  await settle(() => page.elements.get("reply-text").textContent === "Latest reply for Synthetic latest project-one task", "fresh state confirms actual conversation");
  assert.equal(page.workspace.dataset.activeTab, "reply");
  assert.equal(service.posts.length, 1);
  assert.equal(service.jobReads, 0);
}

async function asynchronousCatalogLoadingAndRefresh() {
  const service = new ProjectService();
  service.loadingReads = 2;
  const page = browser(service);
  await settle(() => page.elements.get("profile-select").value === "synthetic", "asynchronous catalog startup");
  page.click("projects-button");
  await settle(() => service.catalogReads === 1, "first loading catalog");
  assert.match(page.elements.get("projects-status").textContent, /正在读取/);
  assert.equal(page.elements.get("project-select").disabled, true);
  assert.equal(page.elements.get("projects-button").disabled, false, "The user can close a loading project panel");
  page.pollProjects();
  await settle(() => service.catalogReads === 2, "second loading catalog");
  page.pollProjects();
  await settle(() => page.elements.get("project-select").options.length === 4, "asynchronous catalog complete");
  assert.equal(service.catalogReads, 3);
  assert(service.catalogUrls.every(url => url === "/api/projects"), "Background loading polls must not force another refresh");
  assert.equal(page.elements.get("session-select").value, "");
  assert.equal(service.posts.length, 0);

  page.click("projects-refresh-button");
  await settle(() => service.catalogReads === 4, "explicit refresh catalog");
  page.pollProjects();
  await settle(() => service.catalogReads === 5, "ordinary poll after explicit refresh");
  page.pollProjects();
  await settle(() => service.catalogReads === 6 && !page.elements.get("project-select").disabled, "refresh completes");
  assert.equal(service.catalogUrls.filter(url => url.includes("refresh=true")).length, 1);
  assert.equal(service.catalogUrls[3], "/api/projects?refresh=true");
  assert.equal(page.draft().source, page.initial.source);

  page.click("projects-refresh-button");
  await settle(() => service.catalogReads === 7, "loading before panel close");
  page.click("projects-button");
  assert.equal(page.elements.get("projects-panel").hidden, true);
  page.pollProjects();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(service.catalogReads, 7, "Closing a loading panel cancels its next poll");
  page.click("projects-button");
  await settle(() => service.catalogReads === 8, "reopening uses ordinary cached read");
  page.visibility(true);
  page.pollProjects();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(service.catalogReads, 8, "Hidden pages pause project catalog polling");
  page.visibility(false);
  await settle(() => service.catalogReads === 9 && !page.elements.get("project-select").disabled, "visible page resumes the cached catalog");
  assert.equal(service.catalogUrls.filter(url => url.includes("refresh=true")).length, 2);
}

async function nativeCodeAttachmentCapabilityAndDirectGesture() {
  const service = new ProjectService();
  const page = browser(service);
  await settle(() => page.elements.get("profile-select").value === "synthetic", "native attachment startup");
  service.nativeAttachments = "true";
  page.poll(5000);
  await new Promise(resolve => setImmediate(resolve));
  page.click("attach-button");
  assert.equal(page.elements.get("file-input").clicks, 0, "Only boolean true may enable Code attachments");
  service.nativeAttachments = true;
  page.poll(5000);
  await settle(() => page.elements.get("attach-button").title === "载入附件", "verified native Code attachment capability");
  page.click("attach-button");
  assert.equal(page.elements.get("file-input").clicks, 1, "Open the file input synchronously within the user's click gesture");
  const file = new Blob(["synthetic"], {type: "text/plain"});
  file.name = "synthetic.txt";
  page.elements.get("file-input").files = [file];
  page.elements.get("file-input").dispatch("change");
  await settle(() => Boolean(service.uploadRelease), "native Code upload allowed");
  assert.equal(page.elements.get("projects-button").disabled, true);
  service.uploadRelease();
  await settle(() => page.draft().attachments?.length === 1, "native Code attachment staged");
  assert.equal(page.draft().source, page.initial.source);
  assert.equal(service.posts.length, 0, "Staging an attachment must not send the prompt");
}

async function replyContextWaitsAndSurvivesReload() {
  const service = new ProjectService();
  service.delayReply = true;
  service.deferPost = true;
  const page = browser(service);
  await settle(() => page.elements.get("reply-text").textContent === "Synthetic original reply", "original context reply");
  page.click("projects-button");
  await settle(() => page.elements.get("project-select").options.length === 4, "reply navigation catalog");
  page.choose("project-select", projectOne);
  await settle(() => Boolean(service.postRelease), "navigation waiting for POST");
  assert.equal(page.elements.get("reply-text").textContent, "", "Hide the old project's reply as soon as navigation begins");
  assert.equal(page.workspace.dataset.activeTab, "compose");
  page.elements.get("source").value = "导航期间继续编辑的合成草稿";
  page.elements.get("source").dispatch("input");
  page.elements.get("english").value = "Synthetic draft edited during navigation";
  page.elements.get("english").dispatch("input");
  service.postRelease();
  await settle(() => !page.draft().pendingJob, "navigation returned new context");
  assert.equal(page.elements.get("reply-text").textContent, "", "A stale reply must stay hidden when target already points to the new context");
  assert.equal(page.workspace.dataset.activeTab, "compose", "Wait for the selected conversation's reply before changing tabs");
  page.click("translate-reply-button");
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(service.posts.length, 1, "Never translate the previous project's reply while waiting for a matching context");
  page.click("translate-reply-button");
  assert(page.draft().replyNavigation?.context);

  const restored = browser(service, {storage: page.storage});
  await settle(() => restored.elements.get("profile-select").value === "synthetic", "reload while reply pending");
  assert.equal(restored.elements.get("reply-text").textContent, "");
  assert.equal(service.posts.length, 1, "Reload restores the navigation context without reposting");
  service.publishReply();
  restored.poll(5000);
  await settle(() => restored.elements.get("reply-text").textContent === "Latest reply for Synthetic latest project-one task", "matching new-context reply");
  assert.equal(restored.workspace.dataset.activeTab, "reply");
  assert.equal(restored.draft().replyNavigation, null);
  assert.equal(restored.draft().source, "导航期间继续编辑的合成草稿");
  assert.equal(restored.draft().english, "Synthetic draft edited during navigation");
  assert.equal(service.posts.length, 1);
}

async function unknownNavigationIsNotADeliveryReceipt() {
  const service = new ProjectService();
  service.nextJobState = "unknown";
  const attachment = {id: "8".repeat(32), name: "retained.txt", kind: "file", size: 9};
  const page = browser(service, {initial: {attachments: [attachment]}});
  await settle(() => page.elements.get("profile-select").value === "synthetic", "unknown navigation startup");
  page.click("projects-button");
  await settle(() => page.elements.get("project-select").options.length === 4, "unknown navigation headers");
  page.choose("project-select", projectOne);
  await settle(() => !page.draft().pendingJob && service.posts.length === 1 && service.catalogReads === 2 && !page.elements.get("project-select").disabled,
    "unknown navigation preserves editing and refreshes read-only headers");
  assert.deepEqual(page.draft().receiptJobs, []);
  assert.equal(page.draft().lastUncertain, null);
  assert.equal(page.draft().source, page.initial.source);
  assert.equal(page.draft().english, page.initial.english);
  assert.deepEqual(page.draft().attachments, [attachment]);
  assert.match(page.elements.get("notice").textContent, /核对 Ubuntu 当前会话/);
  assert.equal(page.elements.get("projects-button").disabled, false);
  page.poll(1000);
  page.poll(5000);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(service.jobReads, 0, "Navigation has no send receipt and must not poll one forever");
  await settle(() => page.elements.get("reply-text").textContent === "Latest reply for Synthetic latest project-one task", "actual state recovers reply after navigation uncertainty");
  page.choose("project-select", projectTwo);
  await settle(() => service.posts.length === 2 && !page.draft().pendingJob, "user may explicitly navigate again");
  assert.equal(service.posts[1].action, "open_project");
  assert.deepEqual(page.draft().attachments, [attachment]);
}

async function newConversationKeepsCompose() {
  const service = new ProjectService();
  service.ready = true;
  const attachment = {id: "9".repeat(32), name: "new-retained.txt", kind: "file", size: 9};
  const page = browser(service, {initial: {attachments: [attachment]}});
  await settle(() => !page.elements.get("new-button").disabled, "new conversation ready");
  page.click("new-button");
  await settle(() => service.posts.length === 1 && !page.draft().pendingJob, "new conversation completed");
  assert.equal(service.posts[0].action, "new");
  assert.equal(page.draft().source, page.initial.source);
  assert.equal(page.draft().english, page.initial.english);
  assert.deepEqual(page.draft().attachments, [attachment]);
  assert.equal(page.workspace.dataset.activeTab, "compose", "A new blank conversation keeps the user in their draft");
}

async function unknownSameContextNeedsFreshCapture() {
  const service = new ProjectService();
  service.nextJobState = "unknown";
  const finish = service.complete.bind(service);
  service.complete = payload => {
    const oldContext = service.context;
    const oldReply = clone(service.reply);
    const job = finish(payload);
    service.context = oldContext;
    service.catalog.context = oldContext;
    service.reply = oldReply;
    return job;
  };
  const page = browser(service);
  await settle(() => page.elements.get("reply-text").textContent === "Synthetic original reply", "unchanged cached reply fixture");
  page.click("projects-button");
  await settle(() => page.elements.get("project-select").options.length === 4, "cached-reply headers");
  page.choose("project-select", projectOne);
  await settle(() => service.posts.length === 1 && !page.draft().pendingJob && service.catalogReads === 2, "unknown unchanged cached state");
  page.poll(5000);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(page.elements.get("reply-text").textContent, "", "Do not treat an unchanged pre-navigation reply cache as confirmation");
  assert(page.draft().replyNavigation);
  service.reply.updated_at = 101;
  page.poll(5000);
  await settle(() => page.elements.get("reply-text").textContent === "Synthetic original reply", "fresh capture of actual same context");
  assert.equal(page.draft().replyNavigation, null);
  assert.equal(service.jobReads, 0);
  assert.equal(service.posts.length, 1);
}

async function matchCurrentIsReadOnlyAndPreservesDraft() {
  const service = new ProjectService();
  service.deferPost = true;
  const attachment = {id: "8".repeat(32), name: "match-retained.txt", kind: "file", size: 9};
  const page = browser(service, {initial: {attachments: [attachment]}});
  await settle(() => page.elements.get("reply-text").textContent === "Synthetic original reply", "match startup with stale Bridge reply");
  assert.equal(page.elements.get("new-button").disabled, true, "Native inline question blocks new tasks");
  assert.equal(page.elements.get("match-current-button").disabled, false, "Read-only matching remains available while the native task is busy or awaiting approval");
  page.click("match-current-button");
  await settle(() => Boolean(service.postRelease), "match request in flight");
  assert.equal(page.elements.get("reply-text").textContent, "", "Immediately hide the stale conversation reply");
  assert.match(page.elements.get("reply-status").textContent, /Ubuntu 当前对话/);
  assert.equal(page.elements.get("match-current-button").disabled, true);
  page.elements.get("source").value = "匹配期间继续写的草稿";
  page.elements.get("source").dispatch("input");
  page.elements.get("english").value = "Draft edited during matching";
  page.elements.get("english").dispatch("input");
  service.postRelease();
  await settle(() => !page.draft().pendingJob && page.elements.get("reply-text").textContent === "Latest reply for Synthetic native current task", "matched current task reply");
  assert.equal(service.catalogReads, 0, "Matching must not load project or history catalogs");
  assert.deepEqual(service.sessionReads, []);
  assert.equal(service.posts.length, 1, "Matching must not submit a prompt or translation");
  assert.equal(service.posts[0].action, "match_current");
  assert.match(page.elements.get("target-title").textContent, /sample-project \/ Synthetic native current task/);
  assert.match(page.elements.get("job-title").textContent, /匹配当前对话 · 已匹配/);
  assert.equal(page.workspace.dataset.activeTab, "reply");
  assert.equal(page.draft().source, "匹配期间继续写的草稿");
  assert.equal(page.draft().english, "Draft edited during matching");
  assert.deepEqual(page.draft().attachments, [attachment]);
  assert.deepEqual(page.draft().receiptJobs, []);
  assert.equal(page.draft().lastUncertain, null);
}

async function matchSameContextWaitsForFreshReply() {
  const service = new ProjectService();
  service.matchContext = service.context;
  service.delayReply = true;
  const page = browser(service);
  await settle(() => page.elements.get("reply-text").textContent === "Synthetic original reply", "same-context match startup");
  page.click("match-current-button");
  await settle(() => !page.draft().pendingJob && service.posts.length === 1, "match completed before fresh capture");
  assert.equal(page.elements.get("reply-text").textContent, "", "The old cached reply cannot confirm a same-context match");
  assert.equal(page.workspace.dataset.activeTab, "compose");
  const restored = browser(service, {storage: page.storage});
  await settle(() => restored.elements.get("profile-select").value === "synthetic", "restore pending match capture");
  assert.equal(restored.elements.get("reply-text").textContent, "");
  assert.equal(service.posts.length, 1, "Reload may query the result but must not repeat matching or send");
  service.publishReply();
  restored.poll(5000);
  await settle(() => restored.elements.get("reply-text").textContent === "Latest reply for Synthetic native current task", "fresh same-context capture");
  assert.equal(restored.workspace.dataset.activeTab, "reply");
  assert.equal(restored.draft().replyNavigation, null);
  assert.equal(restored.draft().source, page.initial.source);
  assert.equal(restored.draft().english, page.initial.english);
}

async function unknownMatchHasNoDeliveryReceipt() {
  const service = new ProjectService();
  service.nextJobState = "unknown";
  const page = browser(service);
  await settle(() => !page.elements.get("match-current-button").disabled, "unknown match startup");
  page.click("match-current-button");
  await settle(() => !page.draft().pendingJob && service.posts.length === 1, "unknown read-only match outcome");
  assert.deepEqual(page.draft().receiptJobs, []);
  assert.equal(page.draft().lastUncertain, null);
  assert.equal(page.elements.get("match-current-button").disabled, false, "Read-only matching may be requested again");
  page.poll(1000);
  page.poll(5000);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(service.jobReads, 0, "A read-only match has no send receipt to recover");
  assert.equal(service.catalogReads, 0);
  assert.equal(page.draft().source, page.initial.source);
  assert.equal(page.draft().english, page.initial.english);
}

(async () => {
  const desktopService = new ProjectService();
  const desktopPage = browser(desktopService);
  await settle(() => desktopPage.elements.get("profile-select").value === "synthetic", "Desktop menu fixture");
  const desktopDraft = desktopPage.draft();
  desktopPage.click("desktop-open-button");
  assert.deepEqual(desktopPage.openedPages, [["/desktop", "_blank", "noopener"]]);
  assert.equal(desktopService.posts.length, 0, "Opening a separate viewer must never submit a Claude task");
  assert.equal(desktopPage.draft().source, desktopDraft.source);
  assert.equal(desktopPage.draft().english, desktopDraft.english);
  await explicitProjectAndSessionNavigation();
  await pendingBlocksNavigationAndCodeExplainsAttachments();
  await asynchronousCatalogLoadingAndRefresh();
  await nativeCodeAttachmentCapabilityAndDirectGesture();
  await replyContextWaitsAndSurvivesReload();
  await unknownNavigationIsNotADeliveryReceipt();
  await unknownIgnoresInflightOldState();
  await unknownSameContextNeedsFreshCapture();
  await newConversationKeepsCompose();
  await matchCurrentIsReadOnlyAndPreservesDraft();
  await matchSameContextWaitsForFreshReply();
  await unknownMatchHasNoDeliveryReceipt();
  console.log("Mobile projects UI passed: one-click read-only current matching, fresh context-bound reply/reload, header-only catalogs, scoped navigation, retained drafts/media, unknown navigation without delivery receipts, async polling, and Code attachments");
})().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
