"use strict";

(() => {
  const CLIENT_VERSION = '__BRIDGE_BUILD__';
  const storageKey = "claude-bridge.mobile.v1";
  const updateAttemptsKey = "claude-bridge.auto-update-targets.v1";
  const terminalStates = new Set(["translated", "pasted", "submitted", "failed", "unknown"]);
  const stateLabels = {
    queued: "等待处理", running: "正在处理", translated: "已翻译", pasted: "已载入草稿",
    submitted: "已提交", failed: "尚未发送", unknown: "结果未确认"
  };
  const actionLabels = {
    translate: "翻译指令", send: "发送英文稿", translate_send: "翻译并发送",
    new: "新建对话", translate_reply: "翻译回复", open_project: "打开项目", switch_project: "切换会话", match_current: "匹配当前对话"
  };
  const navigationActions = new Set(["new", "open_project", "switch_project", "match_current"]);
  const deliveryActions = new Set(["send", "translate_send"]);
  const byId = id => document.getElementById(id);
  const ui = {
    login: byId("login-view"), app: byId("app-view"), startup: byId("startup-state"),
    loginForm: byId("login-form"), deviceLabel: byId("device-label"), pairingCode: byId("pairing-code"),
    loginButton: byId("login-button"),
    loginError: byId("login-error"), logout: byId("logout-button"), connection: byId("connection-state"),
    mobileMenu: byId("mobile-menu-button"), connectionPanel: byId("connection-panel") || document.querySelector(".connection-strip"),
    updateBanner: byId("update-banner"), updateDescription: byId("update-description"),
    updatePage: byId("update-page-button"),
    profile: byId("profile-select"), model: byId("profile-model"), targetState: byId("target-state"),
    targetTitle: byId("target-title"), targetDetail: byId("target-detail"), newButton: byId("new-button"),
    matchCurrent: byId("match-current-button"), projectsButton: byId("projects-button"), projectsPanel: byId("projects-panel"),
    projectsStatus: byId("projects-status"), project: byId("project-select"),
    refreshProjects: byId("projects-refresh-button"),
    sessionPicker: byId("session-picker"), session: byId("session-select"),
    enterSession: byId("enter-session-button"),
    source: byId("source"), english: byId("english"), draftState: byId("draft-state"),
    attach: byId("attach-button"), fileInput: byId("file-input"), attachments: byId("attachment-list"),
    attachmentHint: byId("attachment-hint"), autoEnter: byId("auto-enter"),
    translate: byId("translate-button"), send: byId("send-button"), translateSend: byId("translate-send-button"),
    jobStatus: byId("job-status"), jobTitle: byId("job-title"), jobDetail: byId("job-detail"),
    jobSpinner: byId("job-spinner"), checkedUbuntu: byId("checked-ubuntu-button"),
    notice: byId("notice"), workspace: document.querySelector(".workspace"),
    recovery: byId("request-recovery"), recoveryDescription: byId("recovery-description"),
    retryRequest: byId("retry-request-button"), abandonRequest: byId("abandon-request-button"),
    replyHeading: byId("reply-heading"), replyToggle: byId("translate-reply-button"),
    refresh: byId("refresh-button"), replyStatus: byId("reply-status"), replyText: byId("reply-text"),
    replyEmpty: byId("reply-empty"), replyUpdated: byId("reply-updated"), replyNew: byId("reply-new"),
    history: byId("history-list"), historyEmpty: byId("history-empty"),
    historyRefresh: byId("history-refresh-button"), saved: byId("saved-state")
  };

  let authenticated = false;
  let hadAuthenticatedSession = false;
  let connected = false;
  let csrf = "";
  let serverState = null;
  let selectedProfile = "";
  let boundSource = null;
  let draftRevision = 0;
  let attachmentItems = [];
  let uploadCount = 0;
  let pendingJob = null;
  let pendingReplyJob = null;
  let lastUncertain = null;
  let posting = false;
  let stateRequest = null;
  let jobRequest = null;
  let historyRequest = null;
  let projectsRequest = null;
  let projectsLoading = false;
  let projectsTimer = null;
  let projectCatalog = null;
  let chosenProjectId = "";
  let chosenSessionId = "";
  let replyNavigation = null;
  let stateTimer = null;
  let jobTimer = null;
  let updateTimer = null;
  let versionRequest = null;
  let availableVersion = "";
  let reloading = false;
  let updateSaveFailed = false;
  let automaticUpdateBlocked = false;
  let mobileMenuExpanded = false;
  let replyTranslated = false;
  let activeTab = "compose";
  let lastReplyHash = "";
  const handledJobs = new Map();
  const receiptJobs = new Map();
  const receiptChecks = new Map();
  const translatedReplies = new Map();
  const replyFailures = new Map();
  const requestedReplies = new Set();
  const retryReplyKeys = new Set();
  const missingJobs = new Set();
  const postingIds = new Set();

  class ApiError extends Error {
    constructor(message, status = 0) {
      super(message);
      this.status = status;
    }
  }

  function readStored() {
    try {
      const stored = JSON.parse(localStorage.getItem(storageKey) || "{}");
      return stored && typeof stored === "object" ? stored : {};
    } catch (_) {
      return {};
    }
  }

  function readPairingCodeFromUrl() {
    const fragment = new URLSearchParams(window.location.hash.slice(1));
    if (!fragment.has("pair")) return;
    const code = fragment.get("pair");
    window.history.replaceState(null, "", window.location.pathname + window.location.search);
    if (code && code.length <= 200) ui.pairingCode.value = code;
  }

  function restoreDraft() {
    const stored = readStored();
    ui.source.value = typeof stored.source === "string" ? stored.source : "";
    ui.english.value = typeof stored.english === "string" ? stored.english : "";
    boundSource = typeof stored.boundSource === "string" ? stored.boundSource : null;
    draftRevision = Number.isSafeInteger(stored.revision) ? stored.revision : 0;
    selectedProfile = typeof stored.profile === "string" ? stored.profile : "";
    attachmentItems = Array.isArray(stored.attachments) ? stored.attachments.filter(item =>
      item && typeof item.id === "string" && typeof item.name === "string"
    ) : [];
    pendingJob = validPending(stored.pendingJob) ? stored.pendingJob : null;
    pendingReplyJob = validPending(stored.pendingReplyJob) ? stored.pendingReplyJob : null;
    lastUncertain = validPending(stored.lastUncertain) && deliveryActions.has(stored.lastUncertain.action) ? stored.lastUncertain : null;
    replyNavigation = stored.replyNavigation && /^[a-f0-9]{32}$/.test(stored.replyNavigation.id) ? stored.replyNavigation : null;
    if (!replyNavigation && pendingJob && navigationActions.has(pendingJob.action)) {
      replyNavigation = {id: pendingJob.id, context: "", show_reply: pendingJob.action !== "new", action: pendingJob.action};
    }
    for (const job of Array.isArray(stored.receiptJobs) ? stored.receiptJobs : []) {
      if (validPending(job) && deliveryActions.has(job.action)) receiptJobs.set(job.id, job);
    }
    if (lastUncertain && !receiptJobs.has(lastUncertain.id)) receiptJobs.set(lastUncertain.id, lastUncertain);
    ui.autoEnter.checked = stored.autoEnter !== false;
    replyTranslated = stored.replyTranslated === true;
    renderAttachments();
    renderDraftState();
    if (lastUncertain) renderJob({id: lastUncertain.id, action: lastUncertain.action, state: "unknown", error: "请先查看 Ubuntu，再确认是否需要继续发送。"});
  }

  function validPending(job) {
    return job && typeof job === "object" && /^[a-f0-9]{32}$/.test(job.id) && typeof job.action === "string";
  }

  function missingPending() {
    return [pendingJob, pendingReplyJob].find(job => job && missingJobs.has(job.id));
  }

  function hasOriginalRequest(job) {
    return job?.payload && job.payload.id === job.id && job.payload.action === job.action;
  }

  function renderRecovery() {
    const missing = missingPending();
    ui.recovery.hidden = !missing;
    if (!missing) return;
    ui.recoveryDescription.textContent = hasOriginalRequest(missing) ?
      "尚未查询到这次操作。重试会使用完全相同的编号和内容，已接收的操作会自动去重。" :
      "尚未查询到这次操作，浏览器中也没有完整的原始请求。请查看 Ubuntu 后结束跟踪。";
    ui.retryRequest.disabled = posting || !hasOriginalRequest(missing);
    ui.abandonRequest.disabled = posting;
  }

  function saveDraft() {
    try {
      localStorage.setItem(storageKey, JSON.stringify({
        source: ui.source.value, english: ui.english.value, boundSource, revision: draftRevision,
        profile: selectedProfile, attachments: attachmentItems, autoEnter: ui.autoEnter.checked,
        replyTranslated, pendingJob, pendingReplyJob, lastUncertain, receiptJobs: [...receiptJobs.values()], replyNavigation
      }));
      ui.saved.textContent = "草稿已保存在此浏览器";
      return true;
    } catch (_) {
      ui.saved.textContent = "浏览器未能保存草稿，请保留当前页面";
      return false;
    }
  }

  function draftChanged(englishEdited = false) {
    if (englishEdited) boundSource = ui.source.value;
    draftRevision += 1;
    saveDraft();
    renderDraftState();
    renderControls();
  }

  function renderDraftState() {
    const stale = Boolean(ui.english.value.trim()) && boundSource !== ui.source.value;
    ui.draftState.textContent = stale ? "中文已修改，请重新翻译" : "";
  }

  function showNotice(message, error = false) {
    ui.notice.textContent = message;
    ui.notice.hidden = !message;
    ui.notice.classList.toggle("error", error);
  }

  function showLogin(message = "") {
    authenticated = false;
    connected = false;
    csrf = "";
    stopPolling();
    stopProjectPolling();
    ui.startup.hidden = true;
    ui.app.hidden = true;
    ui.login.hidden = false;
    ui.loginError.textContent = message;
  }

  async function api(path, options = {}) {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), options.timeout || 20000);
    const headers = new Headers(options.headers || {});
    const method = options.method || "GET";
    if (method !== "GET" && csrf) headers.set("X-CSRF-Token", csrf);
    let body = options.body;
    if (body !== undefined && !(body instanceof FormData)) {
      headers.set("Content-Type", "application/json");
      body = JSON.stringify(body);
    }
    try {
      const response = await fetch(path, {
        method, body, headers, signal: controller.signal, credentials: "same-origin", cache: "no-store"
      });
      let data;
      try {
        data = await response.json();
      } catch (_) {
        throw new ApiError("服务未返回有效结果，请稍后重新查询。", response.status);
      }
      if (!response.ok) {
        const error = typeof data.error === "string" ? data.error :
          typeof data.detail === "string" ? data.detail : "请求未完成，请稍后重试。";
        if (response.status === 401 && path !== "/api/login") {
          showLogin(hadAuthenticatedSession ? "本设备凭证已失效，请用新的配对码重新登记。草稿已保留。" : "本浏览器尚未登记，请输入配对码。");
        }
        throw new ApiError(error, response.status);
      }
      return data;
    } catch (error) {
      if (error instanceof ApiError) throw error;
      throw new ApiError(error.name === "AbortError" ? "连接超时，正在保留本次操作记录。" : "连接中断，请检查网络。草稿已保留。");
    } finally {
      clearTimeout(timeout);
    }
  }

  async function login(event) {
    event.preventDefault();
    ui.loginButton.disabled = true;
    ui.loginError.textContent = "";
    try {
      const result = await api("/api/login", {method: "POST", body: {
        code: ui.pairingCode.value.trim(), label: ui.deviceLabel.value.trim()
      }});
      csrf = result.csrf || "";
      hadAuthenticatedSession = true;
      ui.pairingCode.value = "";
      await loadState(true);
    } catch (error) {
      ui.loginError.textContent = error.message;
    } finally {
      ui.loginButton.disabled = false;
    }
  }

  async function logout() {
    ui.logout.disabled = true;
    try {
      await api("/api/logout", {method: "POST", body: {}});
      saveDraft();
      hadAuthenticatedSession = false;
      showLogin("本浏览器的设备凭证已撤销。重新登记需要新的配对码。");
    } catch (error) {
      showNotice(error.message, true);
    } finally {
      ui.logout.disabled = false;
    }
  }

  function startPolling() {
    if (!stateTimer) stateTimer = setInterval(() => {
      if (!document.hidden) loadState();
    }, 5000);
    if (!jobTimer) jobTimer = setInterval(() => {
      if (!document.hidden) refreshJobs();
    }, 1000);
    if (/^[a-f0-9]{64}$/.test(CLIENT_VERSION) && !updateTimer) {
      updateTimer = setInterval(() => {
        if (!document.hidden) checkForUpdates();
      }, 30000);
      checkForUpdates();
    }
  }

  function stopPolling() {
    clearInterval(stateTimer);
    clearInterval(jobTimer);
    clearInterval(updateTimer);
    stateTimer = null;
    jobTimer = null;
    updateTimer = null;
  }

  function mutationInFlight() {
    return posting || postingIds.size > 0 || uploadCount > 0;
  }

  function canUpdateAutomatically() {
    const active = serverState?.active_job;
    return connected && !serverState?.worker?.busy && (!active || terminalStates.has(active.state)) &&
      !ui.source.value && !ui.english.value && !attachmentItems.length &&
      !pendingJob && !pendingReplyJob && !lastUncertain && !mutationInFlight();
  }

  function renderAvailableUpdate() {
    if (!ui.updateBanner || !ui.updateDescription) {
      if (availableVersion && authenticated) showNotice(updateSaveFailed ?
        "发现新版本。浏览器未能保存草稿，请保留页面内容后再更新。" : "发现新版本，请在当前操作完成后刷新页面。");
      return;
    }
    ui.updateBanner.hidden = !availableVersion || !authenticated;
    ui.updateDescription.textContent = updateSaveFailed ? "浏览器未能保存草稿，请保留页面内容后再更新。" :
      automaticUpdateBlocked ? "页面仍待更新，请点击“更新页面”重试。" :
      mutationInFlight() ? "正在提交或上传，请完成后再更新页面。" : "草稿和本次操作记录会保留在此浏览器。";
  }

  function recordAutomaticUpdateAttempt() {
    try {
      const stored = window.sessionStorage.getItem(updateAttemptsKey);
      const versions = stored ? JSON.parse(stored) : [];
      if (!Array.isArray(versions) || versions.some(version => typeof version !== "string" || !/^[a-f0-9]{64}$/.test(version))) return false;
      if (versions.includes(availableVersion)) return false;
      window.sessionStorage.setItem(updateAttemptsKey, JSON.stringify([...versions, availableVersion]));
      return true;
    } catch (_) {
      return false;
    }
  }

  function updatePage(manual = false) {
    if (!availableVersion || reloading || !authenticated) return;
    if (mutationInFlight() || (!manual && (automaticUpdateBlocked || !canUpdateAutomatically()))) {
      renderAvailableUpdate();
      return;
    }
    if (!saveDraft()) {
      updateSaveFailed = true;
      renderAvailableUpdate();
      return;
    }
    updateSaveFailed = false;
    if (!manual && !recordAutomaticUpdateAttempt()) {
      automaticUpdateBlocked = true;
      renderAvailableUpdate();
      return;
    }
    reloading = true;
    try {
      window.location.reload();
    } catch (_) {
      reloading = false;
      if (ui.updateDescription) ui.updateDescription.textContent = "页面更新未完成，请稍后再试。";
      else showNotice("页面更新未完成，请稍后再试。");
    }
  }

  async function checkForUpdates() {
    if (!authenticated || document.hidden || reloading || !/^[a-f0-9]{64}$/.test(CLIENT_VERSION)) return;
    if (versionRequest) return versionRequest;
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 10000);
    versionRequest = (async () => {
      try {
        const response = await fetch("/manifest.webmanifest", {
          credentials: "same-origin", cache: "no-store", signal: controller.signal
        });
        if (!response.ok) return;
        const manifest = await response.json();
        const version = manifest.bridge_version;
        if (typeof version !== "string" || !/^[a-f0-9]{64}$/.test(version)) return;
        const nextVersion = version !== CLIENT_VERSION ? version : "";
        if (nextVersion !== availableVersion) automaticUpdateBlocked = false;
        availableVersion = nextVersion;
        renderAvailableUpdate();
        if (availableVersion) {
          await loadState();
          updatePage();
        }
      } catch (_) {
        // Version metadata is optional; an update-check failure must not affect the bridge.
      } finally {
        clearTimeout(timeout);
      }
    })().finally(() => {
      versionRequest = null;
    });
    return versionRequest;
  }

  function toggleMobileMenu() {
    if (!ui.mobileMenu || !ui.connectionPanel) return;
    mobileMenuExpanded = !mobileMenuExpanded;
    ui.mobileMenu.setAttribute("aria-expanded", String(mobileMenuExpanded));
    ui.connectionPanel.classList.toggle("is-open", mobileMenuExpanded);
  }

  async function loadState(initial = false) {
    if (stateRequest) return stateRequest;
    const navigationAwaitId = replyNavigation?.await_state ? replyNavigation.id : "";
    stateRequest = (async () => {
      try {
        const state = await api("/api/state");
        csrf = state.csrf || csrf;
        serverState = state;
        if (replyNavigation?.await_state && replyNavigation.id === navigationAwaitId) {
          replyNavigation.context = state.target?.context || "";
          replyNavigation.await_state = false;
        }
        const firstLogin = !authenticated;
        authenticated = true;
        hadAuthenticatedSession = true;
        connected = true;
        ui.pairingCode.value = "";
        ui.startup.hidden = true;
        ui.login.hidden = true;
        ui.app.hidden = false;
        renderProfiles();
        renderTarget();
        renderReply();
        renderControls();
        if (state.active_job && !terminalStates.has(state.active_job.state)) renderJob(state.active_job);
        startPolling();
        if (firstLogin) {
          loadHistory();
          refreshJobs();
        }
        ensureReplyTranslation();
        updatePage();
      } catch (error) {
        if (error.status === 401) return;
        if (initial && !authenticated) {
          showLogin(error.message);
        } else {
          connected = false;
          renderConnection(false, "连接中断");
          renderControls();
          showNotice(error.message, true);
        }
      } finally {
        stateRequest = null;
      }
    })();
    return stateRequest;
  }

  function renderProfiles() {
    const profiles = Array.isArray(serverState.profiles) ? serverState.profiles : [];
    if (!profiles.some(profile => profile.id === selectedProfile)) {
      selectedProfile = serverState.default_profile || profiles[0]?.id || "";
    }
    const oldOptions = Array.from(ui.profile.options, option => `${option.value}|${option.textContent}`).join("\n");
    const newOptions = profiles.map(profile => `${profile.id}|${profile.label}`).join("\n");
    if (oldOptions !== newOptions) {
      ui.profile.replaceChildren(...profiles.map(profile => {
        const option = document.createElement("option");
        option.value = profile.id;
        option.textContent = profile.label;
        return option;
      }));
    }
    ui.profile.value = selectedProfile;
    const current = profiles.find(profile => profile.id === selectedProfile);
    const effortLabels = {none: "关闭", minimal: "Minimal", low: "Low", medium: "Medium", high: "High", xhigh: "Extra High", max: "Max"};
    const effort = current?.configuration_error ? " · 推理设置需检查" :
      current && Object.hasOwn(current, "reasoning_effort") ?
        ` · 推理 ${effortLabels[current.reasoning_effort] || current.reasoning_effort || "默认"}` : "";
    ui.model.textContent = current ? `${current.model || "默认模型"}${current.kind === "codex" ? " · Codex 额度" : ""}${effort}` : "尚未配置翻译连接";
  }

  function renderConnection(online, label) {
    ui.connection.className = `connection-state ${online ? "online" : "offline"}`;
    const dot = document.createElement("span");
    dot.className = "status-dot";
    ui.connection.replaceChildren(dot, document.createTextNode(label));
  }

  function renderTarget() {
    const target = serverState.target || {};
    const worker = serverState.worker || {};
    const online = Boolean(worker.online);
    const ready = online && target.ready;
    renderConnection(online, online ? "已连接" : "电脑离线");
    const mode = target.mode === "Code" ? "Claude Code" : target.mode === "Chat" ? "Claude Chat" : "Claude";
    ui.targetState.textContent = !online ? "电脑离线" : ready ? "可以发送" : target.busy ? "正在执行任务" : "等待就绪";
    ui.targetState.className = `status-pill ${ready ? "ready" : "blocked"}`;
    ui.targetTitle.textContent = target.title ? `${mode} · ${target.project_title ? `${target.project_title} / ` : ""}${target.title}` : mode;
    const detail = !online ? "请确认 Windows 桥接服务正在运行" : ready ? target.workspace || "沿用 Ubuntu 当前打开的会话" :
      target.reason || "请在 Ubuntu 打开目标会话";
    ui.targetDetail.textContent = detail;
    ui.attachmentHint.hidden = target.mode !== "Code";
    if (target.mode === "Code") ui.attachmentHint.textContent = supportsAttachments() ?
      "附件会随指令载入当前 Code 会话。" : "当前 Code 会话尚未开放附件载入，可点击回形针查看说明。";
  }

  function navigationBusy() {
    return Boolean(pendingJob || pendingReplyJob || mutationInFlight());
  }

  function renderProjectControls() {
    if (!ui.projectsButton || !ui.project || !ui.session || !ui.enterSession) return;
    const blocked = !authenticated || !connected || !serverState?.worker?.online || navigationBusy();
    const reading = projectsLoading || Boolean(projectsRequest);
    if (ui.matchCurrent) ui.matchCurrent.disabled = blocked;
    ui.projectsButton.disabled = blocked;
    if (ui.refreshProjects) ui.refreshProjects.disabled = blocked || reading;
    ui.project.disabled = blocked || reading || !projectCatalog?.projects?.length;
    ui.session.disabled = blocked || reading;
    const project = projectCatalog?.projects?.find(item => item.id === chosenProjectId);
    const session = project?.sessions?.find(item => item.id === chosenSessionId);
    ui.enterSession.disabled = blocked || reading || !project?.selectable || !session?.selectable || !projectCatalog?.session_catalog_id;
  }

  function makeChoice(value, title, selectable = true) {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = title;
    option.disabled = !selectable;
    return option;
  }

  function renderProjects() {
    if (!ui.project || !ui.session || !ui.sessionPicker) return;
    const projects = Array.isArray(projectCatalog?.projects) ? projectCatalog.projects : [];
    ui.project.replaceChildren(makeChoice("", "请选择项目"), ...projects.map(project =>
      makeChoice(project.id, `${project.title}${project.active ? "（当前）" : ""}${!project.selectable && project.reason ? ` · ${project.reason}` : ""}`, project.selectable)));
    chosenProjectId = projectCatalog?.scope === "sessions" && projects.some(project => project.id === projectCatalog.selected_project_id) ?
      projectCatalog.selected_project_id : "";
    ui.project.value = chosenProjectId;
    const project = projects.find(item => item.id === chosenProjectId);
    const loaded = projectCatalog?.scope === "sessions" && project?.sessions_loaded === true;
    ui.sessionPicker.hidden = !loaded;
    const sessions = loaded && Array.isArray(project.sessions) ? project.sessions : [];
    ui.session.replaceChildren(makeChoice("", "请选择要进入的会话"), ...sessions.map(session =>
      makeChoice(session.id, `${session.title}${session.active ? "（当前）" : ""}${!session.selectable && session.reason ? ` · ${session.reason}` : ""}`, session.selectable)));
    chosenSessionId = sessions.some(session => session.id === projectCatalog?.active_session_id) ? projectCatalog.active_session_id : "";
    ui.session.value = chosenSessionId;
    renderProjectControls();
  }

  function stopProjectPolling() {
    clearTimeout(projectsTimer);
    projectsTimer = null;
  }

  function pollProjectCatalog() {
    stopProjectPolling();
    if (!authenticated || document.hidden || ui.projectsPanel?.hidden || !projectsLoading) return;
    projectsTimer = setTimeout(() => {
      projectsTimer = null;
      loadProjects();
    }, 1000);
  }

  async function loadProjects(refresh = false) {
    if (!ui.projectsPanel || !ui.projectsStatus || !authenticated) return;
    if (projectsRequest) return projectsRequest;
    stopProjectPolling();
    if (ui.sessionPicker) ui.sessionPicker.hidden = true;
    ui.projectsStatus.textContent = "正在读取 Ubuntu 项目…";
    projectsRequest = (async () => {
      try {
        const catalog = await api(refresh ? "/api/projects?refresh=true" : "/api/projects");
        projectsLoading = catalog.loading === true;
        if (projectsLoading) {
          ui.projectsStatus.textContent = catalog.reason || "正在读取 Ubuntu 项目…";
          return;
        }
        projectCatalog = catalog;
        if (catalog.reason) {
          ui.projectsStatus.textContent = catalog.reason;
        } else if (catalog.mode !== "Code") {
          ui.projectsStatus.textContent = "请在 Ubuntu 打开 Claude Code，再读取项目。";
        } else {
          ui.projectsStatus.textContent = catalog.projects?.length ? "选择项目后，会打开该项目最近的会话。" : "当前没有可选择的项目。";
        }
        renderProjects();
      } catch (error) {
        projectsLoading = false;
        if (error.status !== 401) ui.projectsStatus.textContent = error.message;
      }
    })().finally(() => {
      projectsRequest = null;
      renderProjectControls();
      pollProjectCatalog();
    });
    renderProjectControls();
    return projectsRequest;
  }

  function showProjects() {
    if (!ui.projectsPanel || !ui.projectsButton || navigationBusy()) return;
    const expanded = ui.projectsPanel.hidden;
    ui.projectsPanel.hidden = !expanded;
    ui.projectsButton.setAttribute("aria-expanded", String(expanded));
    if (expanded) loadProjects();
    else stopProjectPolling();
  }

  function chooseProject() {
    if (navigationBusy() || projectsLoading || projectsRequest) return;
    chosenProjectId = ui.project.value;
    chosenSessionId = "";
    const project = projectCatalog?.projects?.find(item => item.id === chosenProjectId);
    if (!project?.selectable) {
      if (project?.reason) ui.projectsStatus.textContent = project.reason;
      renderProjects();
      return;
    }
    createJob("open_project", {catalog_id: projectCatalog.catalog_id, project_id: project.id,
      expected_context: projectCatalog.context});
  }

  function switchProject(projectId, sessionId) {
    if (navigationBusy() || projectsLoading || projectsRequest || projectCatalog?.scope !== "sessions") return;
    const project = projectCatalog.projects?.find(item => item.id === projectId);
    const session = sessionId ? project?.sessions?.find(item => item.id === sessionId) : null;
    if (!project?.selectable || !session?.selectable || !projectCatalog.session_catalog_id) return;
    createJob("switch_project", {catalog_id: projectCatalog.catalog_id, project_id: projectId,
      session_id: sessionId, session_catalog_id: projectCatalog.session_catalog_id, expected_context: projectCatalog.context});
  }

  function supportsAttachments() {
    const target = serverState?.target;
    return target?.mode !== "Code" || target?.capabilities?.native_attachments === true;
  }

  function chooseAttachments() {
    if (!supportsAttachments()) {
      showNotice("当前 Code 会话尚未支持从手机载入附件。请在 Ubuntu 的 Code 窗口内添加并发送。", true);
      return;
    }
    ui.fileInput.click();
  }

  function sameSnapshot(snapshot) {
    if (!snapshot) return false;
    return snapshot.source === ui.source.value && snapshot.english === ui.english.value &&
      JSON.stringify(snapshot.attachment_ids || []) === JSON.stringify(attachmentItems.map(item => item.id));
  }

  function composeSnapshot() {
    return {source: ui.source.value, english: ui.english.value, attachment_ids: attachmentItems.map(item => item.id)};
  }

  function uncertainDraftMatches() {
    return [...receiptJobs.values()].some(job => !job.acknowledged && sameSnapshot(job.snapshot));
  }

  function recoverDeliveryEnglish(job, tracked, unchanged) {
    const english = job.english || job.result?.english;
    if (!unchanged || !["unknown", "failed"].includes(job.state) || typeof english !== "string" || !english.trim()) return;
    if (ui.english.value === english && boundSource === tracked.snapshot.source) return;
    ui.english.value = english;
    boundSource = tracked.snapshot.source;
    draftRevision += 1;
    const snapshot = composeSnapshot();
    for (const pending of [tracked, pendingJob, lastUncertain, receiptJobs.get(job.id)]) {
      if (pending?.id === job.id) {
        pending.snapshot = snapshot;
        pending.revision = draftRevision;
      }
    }
  }

  function renderControls() {
    const target = serverState?.target || {};
    const online = Boolean(serverState?.worker?.online);
    const localBusy = Boolean(pendingJob || posting || uploadCount);
    const ready = authenticated && connected && online && target.ready && !localBusy;
    const profileReady = Boolean(selectedProfile);
    const uncertainDraft = uncertainDraftMatches();
    ui.translate.disabled = !authenticated || !connected || !online || !profileReady || localBusy || !ui.source.value.trim();
    ui.send.disabled = !ready || !profileReady || !ui.english.value.trim() || boundSource !== ui.source.value || Boolean(uncertainDraft);
    ui.translateSend.disabled = !ready || !profileReady || !ui.source.value.trim() || Boolean(uncertainDraft);
    ui.newButton.disabled = !ready;
    ui.attach.disabled = !authenticated || !connected || !online || Boolean(pendingJob || posting);
    ui.attach.title = supportsAttachments() ? "载入附件" : "查看 Code 会话的附件支持说明";
    ui.profile.disabled = Boolean(pendingJob || posting);
    ui.autoEnter.disabled = Boolean(pendingJob || posting);
    ui.replyToggle.setAttribute("aria-pressed", String(replyTranslated));
    ui.checkedUbuntu.hidden = !lastUncertain;
    for (const button of ui.attachments.querySelectorAll("button")) button.disabled = Boolean(pendingJob || posting);
    renderRecovery();
    renderAvailableUpdate();
    renderProjectControls();
  }

  function makeId() {
    if (typeof crypto.randomUUID === "function") return crypto.randomUUID().replaceAll("-", "");
    const bytes = crypto.getRandomValues(new Uint8Array(16));
    return Array.from(bytes, byte => byte.toString(16).padStart(2, "0")).join("");
  }

  function normaliseJob(result) {
    return result.job || result;
  }

  async function createJob(action, navigation = null) {
    const isNavigation = navigationActions.has(action);
    if (pendingJob || posting || uploadCount || (isNavigation && pendingReplyJob)) return;
    if (!connected || !serverState?.worker?.online) {
      showNotice("Windows 桥接服务离线，请连接后再操作。", true);
      return;
    }
    const sends = action === "send" || action === "translate_send";
    if (sends && uncertainDraftMatches()) {
      showNotice("这份草稿的发送结果仍未确认，正在查询 Ubuntu 回执，请先保留当前内容。", true);
      return;
    }
    if (action === "send" && boundSource !== ui.source.value) {
      showNotice("中文已修改，请重新翻译或编辑英文稿后再发送。", true);
      return;
    }
    if ((sends || action === "new") && !serverState?.target?.ready) {
      showNotice(serverState?.target?.reason || "请先在 Ubuntu 打开可接收指令的会话。", true);
      return;
    }
    const snapshot = composeSnapshot();
    const id = makeId();
    const payload = action === "match_current" ? {id, action} : isNavigation ? {id, action, expected_context: serverState?.target?.context || "", ...navigation} : {
      id, action, profile: selectedProfile, source: snapshot.source, english: snapshot.english,
      auto_enter: ui.autoEnter.checked, expected_context: serverState?.target?.context || "",
      attachment_ids: snapshot.attachment_ids
    };
    pendingJob = {id, action, revision: draftRevision, snapshot, payload};
    if (isNavigation) {
      replyNavigation = {id, context: "", show_reply: action !== "new", action, requires_fresh: action === "match_current",
        origin_context: serverState?.target?.context || "", origin_updated_at: serverState?.reply?.updated_at,
        origin_hash: serverState?.reply?.source_hash || ""};
      lastReplyHash = "";
      ui.replyNew.hidden = true;
      renderReply();
    }
    posting = true;
    postingIds.add(id);
    saveDraft();
    renderControls();
    showNotice("");
    renderJob({id, action, state: "queued", phase: "正在连接"});
    try {
      const result = await api("/api/jobs", {method: "POST", body: payload});
      handleJob(normaliseJob(result));
    } catch (error) {
      if (error.status >= 400 && error.status < 500 && error.status !== 408) {
        handleJob({id, action, state: "failed", error: error.message});
      } else {
        showNotice(`${error.message} 正在按原编号查询结果。`, true);
      }
    } finally {
      posting = false;
      postingIds.delete(id);
      saveDraft();
      renderControls();
    }
  }

  async function refreshJobs() {
    if (!authenticated || document.hidden || jobRequest) return jobRequest;
    const jobs = [pendingJob, pendingReplyJob].filter(Boolean);
    for (const receipt of receiptJobs.values()) {
      const checked = receiptChecks.get(receipt.id);
      if (!jobs.some(job => job.id === receipt.id) && (checked === undefined || Date.now() - checked >= 5000)) jobs.push(receipt);
    }
    if (!jobs.length) {
      const active = serverState?.active_job;
      if (active && !terminalStates.has(active.state)) jobs.push({id: active.id});
    }
    const readableJobs = jobs.filter(pending => !postingIds.has(pending.id));
    if (!readableJobs.length) return;
    jobRequest = (async () => {
      for (const pending of readableJobs) {
        if (receiptJobs.has(pending.id)) receiptChecks.set(pending.id, Date.now());
        try {
          const result = await api(`/api/jobs/${encodeURIComponent(pending.id)}`);
          handleJob(normaliseJob(result));
        } catch (error) {
          if (error.status === 401) break;
          if (error.status === 404) {
            if (receiptJobs.has(pending.id)) {
              showNotice("原发送记录暂时无法查询，正在保留原编号。请查看 Ubuntu，避免重复发送。", true);
            } else {
              missingJobs.add(pending.id);
              renderRecovery();
            }
          } else {
            showNotice(error.message, true);
          }
        }
      }
    })().finally(() => {
      jobRequest = null;
    });
    return jobRequest;
  }

  function renderJob(job) {
    if (!job || job.action === "translate_reply") return;
    ui.jobStatus.hidden = false;
    const label = job.action === "match_current" && job.state === "submitted" ? "已匹配" : stateLabels[job.state] || "正在处理";
    ui.jobTitle.textContent = `${actionLabels[job.action] || "任务"} · ${label}`;
    ui.jobDetail.textContent = job.error || job.phase || "";
    ui.jobSpinner.hidden = terminalStates.has(job.state);
  }

  function handleJob(job) {
    if (!job || !job.id) return;
    missingJobs.delete(job.id);
    renderRecovery();
    const isReplyJob = job.action === "translate_reply" || pendingReplyJob?.id === job.id;
    if (!isReplyJob && (!pendingJob || pendingJob.id === job.id)) {
      renderJob(job);
    }
    if (!terminalStates.has(job.state)) return;
    const english = job.english || job.result?.english || "";
    const handled = handledJobs.get(job.id);
    if (handled?.state === job.state && handled.english === english) return;
    handledJobs.set(job.id, {state: job.state, english});

    if (isReplyJob) {
      const tracked = pendingReplyJob?.id === job.id ? pendingReplyJob : null;
      // A settings save can happen while a translation is running. The result's
      // revision identifies what actually ran; never label it as the new effort.
      const resultRevision = job.result?.translation_revision;
      const key = job.result?.source_hash && job.profile && typeof resultRevision === "string" ?
        replyKey(job.result.source_hash, job.profile, resultRevision) :
        tracked?.key || (job.result?.source_hash && job.profile ? replyKey(job.result.source_hash, job.profile, "") : "");
      if (job.state === "translated" && job.result?.text && key) {
        translatedReplies.set(key, job.result.text);
      } else if (key) {
        replyFailures.set(key, job.error || "回复翻译未完成，当前显示原文。");
      }
      if (tracked) pendingReplyJob = null;
      saveDraft();
      renderReply();
      renderControls();
      ensureReplyTranslation();
      return;
    }

    const tracked = pendingJob?.id === job.id ? pendingJob : receiptJobs.get(job.id) ||
      (lastUncertain?.id === job.id ? lastUncertain : null);
    const receiptConfirmed = receiptJobs.has(job.id) && job.state !== "unknown";
    const unchanged = tracked && tracked.revision === draftRevision && sameSnapshot(tracked.snapshot);
    if (deliveryActions.has(job.action)) recoverDeliveryEnglish(job, tracked, unchanged);
    if (navigationActions.has(job.action)) {
      if (job.state === "submitted") {
        if (replyNavigation?.id === job.id) replyNavigation.context = job.result?.target?.context || job.result?.context || "";
        if (job.result?.target && serverState) serverState.target = {...serverState.target, ...job.result.target};
        if (job.result?.scope === "sessions") {
          projectCatalog = job.result;
          projectsLoading = false;
          renderProjects();
          if (ui.projectsStatus) ui.projectsStatus.textContent = "已打开项目会话，也可在下方选择其他会话。";
        }
        if (job.action === "match_current") {
          projectCatalog = null;
          renderProjects();
          if (ui.projectsPanel) ui.projectsPanel.hidden = true;
          ui.projectsButton?.setAttribute("aria-expanded", "false");
          stopProjectPolling();
        }
        showNotice(job.action === "match_current" ? "已匹配 Ubuntu 当前对话，正在读取最新回复。当前草稿和附件已保留。" :
          job.action === "new" ? "已打开新对话，当前草稿已保留。" : "已打开所选会话，正在读取最新回复。当前草稿已保留。");
      } else {
        if (replyNavigation?.id === job.id) {
          if (job.state === "unknown") replyNavigation = {...replyNavigation, await_state: true, uncertain: true};
          else replyNavigation = null;
        }
        showNotice(job.state === "unknown" ? `${job.error || (job.action === "match_current" ? "读取结果未确认。" : "切换结果未确认。")} 请核对 Ubuntu 当前会话，草稿已保留。` :
          job.error || (job.action === "match_current" ? "匹配未完成，草稿已保留。" : "切换未完成，草稿已保留。"), true);
      }
      renderTarget();
      renderReply();
    } else if (job.state === "translated" && job.action === "translate") {
      if (unchanged) {
        ui.english.value = job.english || job.result?.text || "";
        boundSource = tracked.snapshot.source;
        draftRevision += 1;
        showNotice("已翻译，英文稿可继续编辑。");
      } else {
        showNotice("翻译已完成。当前草稿已有修改，可从发送记录查看译文。");
      }
    } else if (job.state === "submitted") {
      if (unchanged) {
        ui.source.value = "";
        ui.english.value = "";
        boundSource = null;
        attachmentItems = [];
        draftRevision += 1;
        renderAttachments();
      }
      showNotice(receiptConfirmed ? unchanged ? "已通过 Ubuntu 回执确认提交，输入内容已清空。" :
        "已通过 Ubuntu 回执确认提交。你继续编辑的草稿已保留。" :
        unchanged ? "已提交到 Ubuntu 当前会话，输入内容已清空。" : "已提交到 Ubuntu。你继续编辑的草稿已保留。");
    } else if (job.state === "pasted") {
      if (unchanged && job.english) {
        ui.english.value = job.english;
        boundSource = tracked.snapshot.source;
        draftRevision += 1;
      }
      showNotice("已载入 Ubuntu 当前会话的草稿，请在客户端检查后发送。");
    } else if (job.state === "unknown" && deliveryActions.has(job.action)) {
      if (tracked) {
        receiptJobs.set(job.id, tracked);
        receiptChecks.set(job.id, Date.now());
        if (!tracked.acknowledged) lastUncertain = tracked;
      }
      showNotice((job.error || "发送结果未确认。") + " 正在按原编号查询 Ubuntu 回执，请避免重复发送。", true);
    } else if (job.state === "failed") {
      showNotice(job.error || "任务未完成，草稿已保留。", true);
    }
    if (pendingJob?.id === job.id) pendingJob = null;
    if (job.state !== "unknown" || !deliveryActions.has(job.action)) {
      receiptJobs.delete(job.id);
      receiptChecks.delete(job.id);
      if (lastUncertain?.id === job.id) lastUncertain = [...receiptJobs.values()].reverse().find(item => !item.acknowledged) || null;
    }
    saveDraft();
    renderDraftState();
    renderControls();
    loadState();
    loadHistory();
    if (job.state === "unknown" && ["open_project", "switch_project"].includes(job.action) && ui.projectsPanel && !ui.projectsPanel.hidden) loadProjects();
  }

  function currentReply() {
    const reply = serverState?.reply;
    const targetContext = serverState?.target?.context;
    if (replyNavigation) {
      if (replyNavigation.await_state || !replyNavigation.context || reply?.context !== replyNavigation.context || targetContext !== replyNavigation.context || !reply.updated_at) return null;
      if ((replyNavigation.uncertain || replyNavigation.requires_fresh) && targetContext === replyNavigation.origin_context &&
          reply.updated_at === replyNavigation.origin_updated_at && (reply.source_hash || "") === replyNavigation.origin_hash) return null;
      const showReply = replyNavigation.show_reply;
      replyNavigation = null;
      saveDraft();
      if (showReply) selectTab("reply");
    }
    if (reply?.context && targetContext && reply.context !== targetContext) return null;
    return reply;
  }

  function replyKey(hash = serverState?.reply?.source_hash, profile = selectedProfile, revision = undefined) {
    if (revision === undefined) revision = serverState?.profiles?.find(item => item.id === profile)?.translation_revision || "";
    return hash && profile ? `${profile}|${revision ? `${revision}|` : ""}${hash}` : "";
  }

  function renderReply() {
    const matched = currentReply();
    const reply = matched || {};
    const key = replyKey();
    const translated = translatedReplies.get(key);
    const original = typeof reply.text === "string" ? reply.text : "";
    let content = original;
    let status = matched ? "" : replyNavigation?.action === "match_current" ? "正在读取 Ubuntu 当前对话的最新回复…" : "正在读取所选会话的最新回复…";
    if (replyTranslated && original) {
      if (translated) content = translated;
      else if (!serverState?.worker?.online) status = "电脑连接恢复后翻译";
      else if (reply.generating) status = "回复完成后自动翻译";
      else if (replyFailures.has(key)) status = replyFailures.get(key);
      else status = pendingReplyJob?.key === key ? "正在翻译回复…" : "等待翻译回复";
    } else if (reply.generating) {
      status = "Claude 正在回复…";
    }
    ui.replyHeading.textContent = (reply.mode || serverState?.target?.mode) === "Code" ? "Claude Code 回复" : "Claude 回复";
    const replyBody = ui.replyText.parentElement;
    const atEnd = replyBody.scrollHeight - replyBody.scrollTop - replyBody.clientHeight < 40;
    if (ui.replyText.textContent !== content) {
      ui.replyText.textContent = content;
      if (atEnd) replyBody.scrollTop = replyBody.scrollHeight;
    }
    ui.replyEmpty.hidden = Boolean(content);
    ui.replyEmpty.textContent = matched ? "当前会话的最新回复会显示在这里。" : "正在确认所选会话的回复…";
    ui.replyText.hidden = !content;
    ui.replyStatus.textContent = status;
    ui.replyToggle.setAttribute("aria-pressed", String(replyTranslated));
    ui.replyUpdated.textContent = reply.updated_at ? `更新于 ${formatTime(reply.updated_at, false)}` : "";
    if (reply.source_hash && lastReplyHash && reply.source_hash !== lastReplyHash && activeTab !== "reply") ui.replyNew.hidden = false;
    lastReplyHash = reply.source_hash || lastReplyHash;
  }

  async function ensureReplyTranslation() {
    const reply = currentReply();
    const key = replyKey();
    if (!authenticated || !connected || !serverState?.worker?.online || document.hidden || !replyTranslated || !reply?.text || !key || reply.generating ||
        pendingReplyJob || pendingJob || posting || serverState?.worker?.busy ||
        translatedReplies.has(key) || replyFailures.has(key) || requestedReplies.has(key)) return;
    const id = makeId();
    const retry = retryReplyKeys.delete(key);
    const payload = {
      id, action: "translate_reply", profile: selectedProfile, source: "",
      source_hash: reply.source_hash, english: "", auto_enter: false, attachment_ids: [], retry
    };
    requestedReplies.add(key);
    pendingReplyJob = {id, action: "translate_reply", key, profile: selectedProfile, source_hash: reply.source_hash, payload};
    postingIds.add(id);
    saveDraft();
    renderReply();
    try {
      const result = await api("/api/jobs", {method: "POST", body: payload});
      handleJob(normaliseJob(result));
    } catch (error) {
      if (error.status >= 400 && error.status < 500 && error.status !== 408) {
        handleJob({id, action: "translate_reply", state: "failed", error: error.message});
      } else {
        showNotice(`${error.message} 正在查询本次回复翻译的结果。`, true);
      }
    } finally {
      postingIds.delete(id);
    }
  }

  async function retryOriginalRequest() {
    const pending = missingPending();
    if (!pending || !hasOriginalRequest(pending) || posting) return;
    posting = true;
    postingIds.add(pending.id);
    renderControls();
    try {
      const result = await api("/api/jobs", {method: "POST", body: pending.payload});
      const job = normaliseJob(result);
      handleJob(job);
      if (!terminalStates.has(job.state)) showNotice("已按原编号恢复查询。草稿已保留。");
    } catch (error) {
      if (error.status >= 400 && error.status < 500 && error.status !== 408) {
        handleJob({id: pending.id, action: pending.action, state: "failed", error: error.message});
      } else {
        showNotice(`${error.message} 本次操作继续保留原编号。`, true);
      }
    } finally {
      posting = false;
      postingIds.delete(pending.id);
      saveDraft();
      renderControls();
    }
  }

  function abandonMissingRequest() {
    const pending = missingPending();
    if (!pending || posting) return;
    missingJobs.delete(pending.id);
    if (pendingJob?.id === pending.id) {
      pendingJob = null;
      ui.jobStatus.hidden = true;
    }
    if (pendingReplyJob?.id === pending.id) {
      replyFailures.set(pending.key, "本次回复翻译已结束跟踪，当前显示原文。");
      pendingReplyJob = null;
    }
    saveDraft();
    renderControls();
    renderReply();
    showNotice("本次跟踪已结束，草稿已保留。再次发送前请核对 Ubuntu 的现有内容。");
  }

  function toggleReplyTranslation() {
    replyTranslated = !replyTranslated;
    if (replyTranslated) {
      const key = replyKey();
      replyFailures.delete(key);
      requestedReplies.delete(key);
      if (key) retryReplyKeys.add(key);
    }
    saveDraft();
    renderReply();
    ensureReplyTranslation();
  }

  function formatSize(bytes) {
    if (!Number.isFinite(bytes)) return "";
    if (bytes < 1024 * 1024) return `${Math.max(1, Math.round(bytes / 1024))} KB`;
    return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
  }

  function renderAttachments() {
    ui.attachments.replaceChildren(...attachmentItems.map(item => {
      const row = document.createElement("li");
      row.className = "attachment-item";
      const name = document.createElement("span");
      name.className = "attachment-name";
      name.textContent = item.name;
      const size = document.createElement("span");
      size.className = "attachment-size";
      size.textContent = formatSize(item.size);
      const remove = document.createElement("button");
      remove.type = "button";
      remove.className = "icon-button";
      remove.setAttribute("aria-label", `移除附件 ${item.name}`);
      const icon = document.createElementNS("http://www.w3.org/2000/svg", "svg");
      const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
      use.setAttribute("href", "#close-icon");
      icon.append(use);
      remove.append(icon);
      remove.disabled = Boolean(pendingJob || posting);
      remove.addEventListener("click", () => {
        attachmentItems = attachmentItems.filter(attachment => attachment.id !== item.id);
        renderAttachments();
        draftChanged();
      });
      row.append(name, size, remove);
      return row;
    }));
  }

  async function uploadFiles(files) {
    if (!files.length || pendingJob || posting) return;
    if (!connected || !serverState?.worker?.online) {
      showNotice("Windows 桥接服务离线，请连接后再载入附件。", true);
      return;
    }
    if (!supportsAttachments()) {
      showNotice("Code 会话请在 Ubuntu 内添加附件。", true);
      return;
    }
    if (!authenticated) return;
    uploadCount += 1;
    renderControls();
    let uploaded = 0;
    try {
      for (const file of files) {
        showNotice(`正在载入 ${file.name}`);
        const body = new FormData();
        body.append("file", file, file.name);
        const result = await api("/api/uploads", {method: "POST", body, timeout: 120000});
        attachmentItems.push({id: result.id, name: result.name, kind: result.kind, size: result.size});
        uploaded += 1;
        renderAttachments();
        draftChanged();
      }
      showNotice(uploaded ? `已载入 ${uploaded} 个附件。` : "");
    } catch (error) {
      showNotice(error.message, true);
    } finally {
      uploadCount -= 1;
      ui.fileInput.value = "";
      renderControls();
    }
  }

  function formatTime(value, includeDate = true) {
    const date = typeof value === "number" ? new Date(value < 1e12 ? value * 1000 : value) : new Date(value);
    if (Number.isNaN(date.getTime())) return "";
    const options = includeDate ? {month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false} :
      {hour: "2-digit", minute: "2-digit", hour12: false};
    return new Intl.DateTimeFormat("zh-CN", options).format(date);
  }

  async function loadHistory() {
    if (!authenticated || historyRequest) return historyRequest;
    historyRequest = (async () => {
      try {
        const result = await api("/api/history");
        const jobs = Array.isArray(result.jobs) ? result.jobs.filter(job => job.action !== "translate_reply") : [];
        ui.history.replaceChildren(...jobs.map(job => {
          const item = document.createElement("li");
          item.className = "history-item";
          const button = document.createElement("button");
          button.className = "history-open";
          button.type = "button";
          const meta = document.createElement("span");
          meta.className = "history-meta";
          const time = document.createElement("span");
          time.textContent = formatTime(job.created_at);
          const status = document.createElement("span");
          status.className = job.state;
          status.textContent = stateLabels[job.state] || job.state;
          meta.append(time, status);
          const snippet = document.createElement("span");
          snippet.className = "history-snippet";
          snippet.textContent = job.source || job.english || actionLabels[job.action] || "任务";
          button.append(meta, snippet);
          button.addEventListener("click", () => restoreHistory(job));
          item.append(button);
          return item;
        }));
        ui.historyEmpty.hidden = Boolean(jobs.length);
      } catch (error) {
        if (error.status !== 401) showNotice(error.message, true);
      } finally {
        historyRequest = null;
      }
    })();
    return historyRequest;
  }

  function restoreHistory(job) {
    if (navigationActions.has(job.action)) {
      renderJob(job);
      showNotice(job.action === "match_current" ? "这是匹配当前对话的记录。当前草稿已保留，可再次点击“匹配当前对话”读取 Ubuntu。" :
        "这是会话切换记录。当前草稿已保留，请从项目列表选择要打开的会话。");
      return;
    }
    ui.source.value = job.source || "";
    ui.english.value = job.english || (job.action === "translate" ? job.result?.text : "") || "";
    boundSource = ui.english.value ? ui.source.value : null;
    attachmentItems = [];
    draftChanged();
    renderAttachments();
    renderJob(job);
    selectTab("compose");
    if (job.state === "unknown" && deliveryActions.has(job.action)) {
      lastUncertain = {...receiptJobs.get(job.id), id: job.id, action: job.action, revision: draftRevision,
        snapshot: composeSnapshot(), acknowledged: false};
      receiptJobs.set(job.id, lastUncertain);
      saveDraft();
      renderControls();
      showNotice(job.error || "这条记录的结果未确认，请先查看 Ubuntu，避免重复发送。", true);
    } else {
      showNotice(job.attachment_ids?.length ? "已载入记录，附件请重新添加。" : "已载入记录，可继续编辑。");
    }
  }

  function selectTab(tab) {
    activeTab = tab;
    ui.workspace.dataset.activeTab = tab;
    for (const button of document.querySelectorAll("[data-tab]")) button.setAttribute("aria-selected", String(button.dataset.tab === tab));
    if (tab === "reply") ui.replyNew.hidden = true;
    if (tab === "history") loadHistory();
  }

  ui.loginForm.addEventListener("submit", login);
  if (ui.mobileMenu && ui.connectionPanel) {
    ui.connectionPanel.classList.add("menu-collapsible");
    ui.mobileMenu.addEventListener("click", toggleMobileMenu);
  }
  ui.updatePage?.addEventListener("click", () => updatePage(true));
  ui.logout.addEventListener("click", logout);
  ui.source.addEventListener("input", () => draftChanged());
  ui.english.addEventListener("input", () => draftChanged(true));
  ui.autoEnter.addEventListener("change", saveDraft);
  ui.profile.addEventListener("change", () => {
    selectedProfile = ui.profile.value;
    saveDraft();
    renderProfiles();
    renderReply();
    renderControls();
    ensureReplyTranslation();
  });
  ui.translate.addEventListener("click", () => createJob("translate"));
  ui.send.addEventListener("click", () => createJob("send"));
  ui.translateSend.addEventListener("click", () => createJob("translate_send"));
  ui.newButton.addEventListener("click", () => createJob("new"));
  ui.matchCurrent?.addEventListener("click", () => createJob("match_current"));
  ui.projectsButton?.addEventListener("click", showProjects);
  ui.refreshProjects?.addEventListener("click", () => {
    if (!navigationBusy()) loadProjects(true);
  });
  ui.project?.addEventListener("change", chooseProject);
  ui.session?.addEventListener("change", () => {
    chosenSessionId = ui.session.value;
    renderProjectControls();
  });
  ui.enterSession?.addEventListener("click", () => switchProject(chosenProjectId, chosenSessionId));
  ui.replyToggle.addEventListener("click", toggleReplyTranslation);
  ui.retryRequest.addEventListener("click", retryOriginalRequest);
  ui.abandonRequest.addEventListener("click", abandonMissingRequest);
  ui.checkedUbuntu.addEventListener("click", () => {
    if (lastUncertain) {
      lastUncertain.acknowledged = true;
      const receipt = receiptJobs.get(lastUncertain.id);
      if (receipt) receipt.acknowledged = true;
    }
    lastUncertain = [...receiptJobs.values()].reverse().find(item => !item.acknowledged) || null;
    saveDraft();
    renderControls();
    showNotice("已保留当前草稿，可继续编辑。再次发送前请核对 Ubuntu 的现有内容。");
  });
  ui.refresh.addEventListener("click", () => loadState());
  ui.historyRefresh.addEventListener("click", loadHistory);
  ui.attach.addEventListener("click", chooseAttachments);
  ui.fileInput.addEventListener("change", () => uploadFiles(Array.from(ui.fileInput.files)));
  const tabButtons = Array.from(document.querySelectorAll("[data-tab]"));
  for (const button of tabButtons) {
    button.addEventListener("click", () => selectTab(button.dataset.tab));
    button.addEventListener("keydown", event => {
      if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
      event.preventDefault();
      const current = tabButtons.indexOf(button);
      const next = event.key === "Home" ? 0 : event.key === "End" ? tabButtons.length - 1 :
        (current + (event.key === "ArrowRight" ? 1 : -1) + tabButtons.length) % tabButtons.length;
      selectTab(tabButtons[next].dataset.tab);
      tabButtons[next].focus();
    });
  }
  for (const editor of [ui.source, ui.english]) {
    editor.addEventListener("dragover", event => {
      if (event.dataTransfer.types.includes("Files")) {
        event.preventDefault();
        editor.classList.add("drag-over");
      }
    });
    editor.addEventListener("dragleave", () => editor.classList.remove("drag-over"));
    editor.addEventListener("drop", event => {
      editor.classList.remove("drag-over");
      if (event.dataTransfer.files.length) {
        event.preventDefault();
        uploadFiles(Array.from(event.dataTransfer.files));
      }
    });
    editor.addEventListener("paste", event => {
      const images = Array.from(event.clipboardData?.items || []).filter(item => item.kind === "file" && item.type.startsWith("image/"));
      if (images.length) {
        event.preventDefault();
        uploadFiles(images.map(item => item.getAsFile()).filter(Boolean));
      }
    });
  }
  document.addEventListener("visibilitychange", async () => {
    if (!document.hidden && authenticated) {
      await loadState();
      refreshJobs();
      checkForUpdates();
    }
  });
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) stopProjectPolling();
    else if (authenticated && projectsLoading && ui.projectsPanel && !ui.projectsPanel.hidden) loadProjects();
  });
  window.addEventListener("online", () => {
    if (authenticated) {
      loadState();
      refreshJobs();
    }
  });
  window.addEventListener("pagehide", saveDraft);
  document.addEventListener("keydown", event => {
    if ((event.ctrlKey || event.metaKey) && event.key === "Enter" && !ui.translateSend.disabled) {
      event.preventDefault();
      createJob("translate_send");
    }
  });

  readPairingCodeFromUrl();
  restoreDraft();
  renderControls();
  loadState(true);
})();
