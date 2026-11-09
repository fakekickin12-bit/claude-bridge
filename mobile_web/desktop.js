(() => {
  "use strict";
  const $ = id => document.getElementById(id);
  const ui = Object.fromEntries(["connection", "notice", "login-required", "viewport", "stage", "screen", "empty-frame", "pointer-mark",
    "desktop-tools", "settings-toggle", "pointer-mode", "zoom", "refresh", "fullscreen", "input-panel", "text-form", "remote-text", "send-text", "extra-key", "send-key", "query-input"].map(id => [id, $(id)]));
  const keyButtons = [...document.querySelectorAll("[data-key]")];
  const storageKey = "claude-bridge.desktop.v1";
  const validId = value => typeof value === "string" && /^[a-f0-9]{32}$/.test(value);
  const validFingerprint = value => typeof value === "string" && /^[a-f0-9]{64}$/.test(value);
  const makeId = () => crypto.randomUUID().replaceAll("-", "");
  let restored = {};
  try { restored = JSON.parse(sessionStorage.getItem(storageKey) || "{}"); } catch (_) {}
  // A full reload may have closed the previous native lease in pagehide.
  // New frames get a new viewer; unresolved input keeps its ORIGINAL viewer.
  const viewerId = makeId();
  let pending = restored.pending && validId(restored.pending.request_id) && validId(restored.pending.viewer_id) ?
    {...restored.pending, unknown: true} : null;
  ui["remote-text"].value = typeof restored.text === "string" ? restored.text : "";
  let csrf = "", authenticated = false, stopped = false;
  let frame = null, frameLoaded = false, frameBusy = false, inputBusy = false;
  let manualFrame = false;
  let frameEpoch = 0, frameTimer = 0, imageGeneration = 0;
  let displayedImageHash = "", displayedWidth = 0, displayedHeight = 0;
  let scale = 0, tap = null, tapTimer = 0;
  let viewportBaseline = window.innerHeight || 0;
  let pureMode = false, pureNative = false, pureRestore = null, exitGesture = null;
  let pinch = null, gesture = false;
  const pointers = new Map();

  class ApiError extends Error {
    constructor(message, status = 0) { super(message); this.status = status; }
  }

  function save() {
    try { sessionStorage.setItem(storageKey, JSON.stringify({viewer_id: viewerId, text: ui["remote-text"].value, pending})); } catch (_) {}
  }

  function notice(message, error = false) {
    if (ui.notice.textContent !== message) ui.notice.textContent = message;
    ui.notice.classList.toggle("error", error);
  }

  function visible() { return !stopped && !document.hidden; }
  function fresh(value = frame) {
    return value && Number.isFinite(value.expires_at) && value.expires_at * 1000 > Date.now();
  }
  function canInput() { return visible() && authenticated && frameLoaded && fresh() && !inputBusy && !pending; }

  function controls() {
    const ready = canInput();
    ui.viewport.setAttribute("aria-disabled", String(!ready));
    ui["send-text"].disabled = !ready || !ui["remote-text"].value.trim();
    ui["send-key"].disabled = !ready;
    ui["extra-key"].disabled = !ready;
    for (const button of keyButtons) button.disabled = !ready;
    const refreshDisabled = !visible() || !authenticated || manualFrame;
    if (ui.refresh.disabled !== refreshDisabled) ui.refresh.disabled = refreshDisabled;
    const refreshText = manualFrame ? "刷新中…" : "刷新";
    if (ui.refresh.textContent !== refreshText) ui.refresh.textContent = refreshText;
    if (ui.refresh.getAttribute("aria-busy") !== String(manualFrame)) ui.refresh.setAttribute("aria-busy", String(manualFrame));
    ui["query-input"].hidden = !pending?.unknown;
    ui["query-input"].disabled = !visible() || !authenticated || inputBusy;
    const connectionText = authenticated ? pending ? "操作待确认" : fresh() && frameLoaded ? "已连接" : "读取画面" : "未连接";
    if (ui.connection.textContent !== connectionText) ui.connection.textContent = connectionText;
    ui.connection.classList.toggle("online", authenticated && frameLoaded && fresh());
    ui.connection.classList.toggle("offline", !authenticated);
  }

  function requireLogin(message) {
    authenticated = false;
    manualFrame = false;
    frameLoaded = false;
    clearTimeout(frameTimer);
    ui["login-required"].hidden = false;
    ui.viewport.hidden = true;
    ui["input-panel"].hidden = true;
    ui["desktop-tools"].hidden = true;
    notice(message || "此浏览器尚未登记，请返回 Claude Bridge 配对。", true);
    controls();
  }

  async function api(path, body, timeout = 28000) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeout);
    try {
      const response = await fetch(path, {method: body === undefined ? "GET" : "POST", credentials: "same-origin", cache: "no-store",
        headers: body === undefined ? {} : {"Content-Type": "application/json", "X-CSRF-Token": csrf},
        body: body === undefined ? undefined : JSON.stringify(body), signal: controller.signal});
      let value;
      try { value = await response.json(); } catch (_) { throw new ApiError("服务未返回有效结果。", response.status); }
      if (!response.ok) {
        const message = typeof value.error === "string" ? value.error : typeof value.detail === "string" ? value.detail : "请求未完成，请稍后重试。";
        if (response.status === 401) requireLogin("本设备尚未登记或凭证已失效，请返回 Claude Bridge 配对。文字已保留。");
        throw new ApiError(message, response.status);
      }
      return value;
    } catch (error) {
      if (error instanceof ApiError) throw error;
      throw new ApiError(error.name === "AbortError" ? "连接超时。" : "连接中断，请检查网络。");
    } finally { clearTimeout(timer); }
  }

  function scheduleFrame(delay = 1000) {
    clearTimeout(frameTimer);
    if (visible() && authenticated) frameTimer = setTimeout(readFrame, delay);
  }

  function validFrame(value) {
    return value && value.mime === "image/jpeg" && validId(value.session_id) && validId(value.frame_id) &&
      ["width", "height"].every(key => Number.isInteger(value[key]) && value[key] > 0 && value[key] <= 1600) &&
      ["desktop_width", "desktop_height"].every(key => Number.isInteger(value[key]) && value[key] > 0 && value[key] <= 32768) &&
      Number.isFinite(value.captured_at) && Number.isFinite(value.expires_at) && value.expires_at > value.captured_at &&
      value.expires_at - value.captured_at <= 46 && value.captured_at * 1000 <= Date.now() + 60000 && fresh(value) &&
      (value.unchanged === true ? validFingerprint(value.image_hash) && value.image_hash === displayedImageHash &&
        !ui.screen.hidden && value.width === displayedWidth && value.height === displayedHeight && value.image_base64 === "" :
        typeof value.image_base64 === "string" && value.image_base64.length <= 1400000 && /^\/9j\/[A-Za-z0-9+/]*={0,2}$/.test(value.image_base64));
  }

  function layoutImage() {
    if (!frame) return;
    if (!scale) {
      ui.stage.style.width = "100%";
      ui.stage.style.height = "100%";
    } else {
      ui.stage.style.width = `${Math.round(frame.width * scale)}px`;
      ui.stage.style.height = `${Math.round(frame.height * scale)}px`;
    }
  }

  function syncViewport() {
    const view = window.visualViewport;
    const root = document.documentElement;
    if (view && root.style) {
      // Safari's keyboard can resize only the visual viewport. Browser pinch
      // zoom is separate: do not resize the document in response to zooming.
      const zoomed = Number.isFinite(view.scale) && view.scale > 1.05;
      const focused = document.activeElement === ui["remote-text"];
      const height = Math.min(window.innerHeight || view.height, view.height);
      if (!zoomed && Number.isFinite(height) && height > 0) {
        if (!focused) viewportBaseline = window.innerHeight || height;
        root.style.setProperty("--app-height", `${Math.round(height)}px`);
        root.style.setProperty("--app-top", `${Math.round(Math.max(0, view.offsetTop || 0))}px`);
      } else {
        root.style.removeProperty("--app-height");
        root.style.removeProperty("--app-top");
      }
      const keyboard = !zoomed && focused && viewportBaseline - height > 100;
      root.classList.toggle("keyboard-open", keyboard);
      root.classList.toggle("keyboard-compact", keyboard && height < 300);
    }
    if (root.style && pureMode) {
      root.style.setProperty("--pure-height", `${Math.round(view?.height || window.innerHeight)}px`);
      root.style.setProperty("--pure-width", `${Math.round(view?.width || window.innerWidth)}px`);
      root.style.setProperty("--pure-top", `${Math.round(Math.max(0, view?.offsetTop || 0))}px`);
      root.style.setProperty("--pure-left", `${Math.round(Math.max(0, view?.offsetLeft || 0))}px`);
    }
    layoutImage();
  }

  function fullscreenElement() { return document.fullscreenElement || document.webkitFullscreenElement || null; }

  function clearTouches() {
    cancelTap(); pointers.clear(); pinch = null; gesture = false; exitGesture = null;
  }

  function finishPureScreen() {
    clearTouches();
    pureMode = pureNative = false;
    document.documentElement.classList.toggle("pure-screen", false);
    if (pureRestore) {
      scale = pureRestore.scale;
      ui.zoom.value = pureRestore.zoom;
      pureRestore = null;
    }
    syncViewport();
  }

  async function exitPureScreen() {
    clearTouches();
    if (fullscreenElement() === ui.viewport) {
      const exit = document.exitFullscreen || document.webkitExitFullscreen;
      if (exit) {
        try { await exit.call(document); } catch (_) { return; }
        if (fullscreenElement() === ui.viewport) return;
      } else return;
    }
    finishPureScreen();
  }

  async function enterPureScreen() {
    if (pureMode) { await exitPureScreen(); return; }
    if (!authenticated || !frameLoaded || !fresh()) {
      notice("请等桌面画面加载完成后再全屏。", true);
      return;
    }
    ui["remote-text"].blur();
    clearTouches();
    pureRestore = {scale, zoom: ui.zoom.value};
    scale = 0;
    pureMode = true;
    pureNative = false;
    document.documentElement.classList.toggle("pure-screen", true);
    syncViewport();
    // Fullscreen only this image area. Unsupported/rejected Safari requests
    // retain the pure-screen CSS view; browser chrome remains browser-owned.
    const request = ui.viewport.requestFullscreen || ui.viewport.webkitRequestFullscreen || ui.viewport.webkitRequestFullScreen;
    if (request) {
      try {
        await request.call(ui.viewport);
        pureNative = fullscreenElement() === ui.viewport;
      } catch (_) {}
    }
  }

  function fullscreenChanged() {
    if (fullscreenElement() === ui.viewport) {
      pureNative = true;
      syncViewport();
    } else if (pureMode && pureNative) finishPureScreen();
  }

  async function readFrame() {
    if (!visible() || !authenticated || frameBusy) return;
    if (inputBusy) { scheduleFrame(250); return; }
    const epoch = frameEpoch;
    frameBusy = true;
    controls();
    try {
      const value = await api("/api/desktop/frame", {viewer_id: viewerId, image_hash: displayedImageHash});
      if (!visible() || epoch !== frameEpoch) return;
      if (!validFrame(value)) throw new ApiError("画面已过期或格式无效，请刷新。");
      const generation = ++imageGeneration;
      const sameImage = value.unchanged === true || (validFingerprint(value.image_hash) && value.image_hash === displayedImageHash &&
        !ui.screen.hidden && value.width === displayedWidth && value.height === displayedHeight);
      if (sameImage) {
        frame = value;
        frameLoaded = true;
        if (!pending) notice("正在显示 Ubuntu 当前桌面。选择触控模式后操作。");
        controls();
        return;
      }
      ui.screen.onload = () => {
        if (generation !== imageGeneration || epoch !== frameEpoch || stopped) return;
        if (ui.screen.naturalWidth !== value.width || ui.screen.naturalHeight !== value.height) {
          frameLoaded = false;
          notice("桌面画面尺寸不匹配，请刷新。", true);
        } else {
          // Keep the last displayed frame usable while a changed JPEG decodes;
          // update pixels and their input grant together, without a gray flash.
          frame = value;
          frameLoaded = true;
          displayedImageHash = validFingerprint(value.image_hash) ? value.image_hash : "";
          displayedWidth = value.width;
          displayedHeight = value.height;
          ui.screen.hidden = false;
          ui["empty-frame"].hidden = true;
          layoutImage();
          if (!pending) notice("正在显示 Ubuntu 当前桌面。选择触控模式后操作。");
        }
        controls();
        scheduleFrame(1000);
      };
      ui.screen.onerror = () => {
        if (generation !== imageGeneration || epoch !== frameEpoch || stopped) return;
        frameLoaded = false;
        displayedImageHash = "";
        displayedWidth = displayedHeight = 0;
        ui.screen.hidden = true;
        ui["empty-frame"].hidden = false;
        notice("桌面画面无法解码，正在重新读取。", true);
        controls();
        scheduleFrame(2500);
      };
      ui.screen.src = `data:image/jpeg;base64,${value.image_base64}`;
    } catch (error) {
      if (epoch === frameEpoch && !stopped) {
        imageGeneration++;
        frameLoaded = false;
        if (ui.screen.complete === false) {
          displayedImageHash = "";
          displayedWidth = displayedHeight = 0;
          ui.screen.hidden = true;
          ui["empty-frame"].hidden = false;
        }
        if (authenticated) notice(`${error.message} 保留上次画面，恢复连接后可继续。`, true);
      }
    } finally {
      frameBusy = false;
      manualFrame = false;
      controls();
      scheduleFrame(epoch === frameEpoch ? frameLoaded ? 1000 : 2500 : 0);
    }
  }

  function refreshManually() {
    if (!visible() || !authenticated || manualFrame) return;
    cancelTap();
    manualFrame = true;
    controls();
    // A user refresh can reuse an already running read. It never queues a
    // duplicate screenshot request, nor affects the quiet background timer.
    if (frameBusy) return;
    clearTimeout(frameTimer);
    return readFrame();
  }

  function imageRect(bounds, width, height) {
    const factor = Math.min(bounds.width / width, bounds.height / height);
    const w = width * factor, h = height * factor;
    return {left: bounds.left + (bounds.width - w) / 2, top: bounds.top + (bounds.height - h) / 2, width: w, height: h};
  }

  function point(x, y, value = frame) {
    if (!value) return null;
    const rect = imageRect(ui.stage.getBoundingClientRect(), value.width, value.height);
    if (!rect.width || !rect.height || x < rect.left || y < rect.top || x > rect.left + rect.width || y > rect.top + rect.height) return null;
    return {x: Math.max(0, Math.min(1, (x - rect.left) / rect.width)), y: Math.max(0, Math.min(1, (y - rect.top) / rect.height))};
  }

  function mark(position) {
    const bounds = ui.stage.getBoundingClientRect();
    const rect = imageRect(bounds, frame.width, frame.height);
    ui["pointer-mark"].style.left = `${rect.left - bounds.left + position.x * rect.width}px`;
    ui["pointer-mark"].style.top = `${rect.top - bounds.top + position.y * rect.height}px`;
    ui["pointer-mark"].hidden = false;
    setTimeout(() => { ui["pointer-mark"].hidden = true; }, 600);
  }

  function acceptInputResult(value, original) {
    if (pending !== original) return;
    const matched = value && value.request_id === original.request_id && value.matched !== false &&
      validFingerprint(value.fingerprint) && (!original.fingerprint || original.fingerprint === value.fingerprint);
    if (!matched) {
      original.unknown = true;
      notice("操作回执未匹配原请求，文字已保留；请查询操作结果。", true);
    } else {
      original.fingerprint = value.fingerprint;
      if (value.state === "applied") {
        if (original.text_snapshot !== undefined && ui["remote-text"].value === original.text_snapshot) ui["remote-text"].value = "";
        pending = null;
        notice("操作已完成，正在刷新画面…");
      } else if (value.state === "not_applied") {
        pending = null;
        notice(value.reason || "操作未执行，刷新后可重新操作；文字已保留。", true);
      } else {
        original.unknown = true;
        notice("操作结果未确认，请查询原请求；没有重复执行。", true);
      }
    }
    save();
  }

  async function sendInput(event, selectedFrame = frame, textSnapshot) {
    if (!canInput()) { notice("请等待操作结果或刷新画面后再操作。", true); return; }
    if (!fresh(selectedFrame) || selectedFrame.session_id !== frame.session_id) {
      notice("操作时的画面已变化，请刷新后再操作。", true); return;
    }
    const original = {viewer_id: viewerId, request_id: makeId(), session_id: selectedFrame.session_id, frame_id: selectedFrame.frame_id,
      event: structuredClone(event), fingerprint: "", unknown: false};
    if (textSnapshot !== undefined) original.text_snapshot = textSnapshot;
    pending = original;
    inputBusy = true;
    frameLoaded = false;
    frameEpoch++;
    save();
    controls();
    notice("正在操作 Ubuntu…");
    try {
      const value = await api("/api/desktop/input", {viewer_id: viewerId, request_id: original.request_id,
        session_id: original.session_id, frame_id: original.frame_id, event: original.event});
      acceptInputResult(value, original);
    } catch (error) {
      if (pending === original) {
        if (error.status >= 400 && error.status < 500 && error.status !== 408) {
          pending = null;
          notice(`${error.message} 操作未执行，文字已保留。`, true);
        } else {
          original.unknown = true;
          notice(`${error.message} 操作结果未确认，文字已保留；请查询原请求。`, true);
        }
        save();
      }
    } finally {
      inputBusy = false;
      frameLoaded = false;
      frameEpoch++;
      controls();
      scheduleFrame(0);
    }
  }

  async function queryInput() {
    if (!visible() || !authenticated || inputBusy || !pending?.unknown) return;
    const original = pending;
    inputBusy = true;
    controls();
    notice("正在查询原操作结果…");
    try {
      acceptInputResult(await api("/api/desktop/status", {viewer_id: original.viewer_id, request_id: original.request_id,
        fingerprint: original.fingerprint || ""}), original);
    } catch (error) {
      notice(`${error.message} 继续保留原操作，未重复执行。`, true);
    } finally {
      inputBusy = false;
      frameLoaded = false;
      frameEpoch++;
      save();
      controls();
      scheduleFrame(0);
    }
  }

  function cancelTap() { clearTimeout(tapTimer); tap = null; }
  function queueTap(position, value, button) {
    if (button !== 1) { cancelTap(); sendInput({kind: "click", ...position, button, clicks: 1}, value); return; }
    if (tap && Date.now() - tap.time < 350 && tap.frame.session_id === value.session_id &&
        Math.abs(tap.position.x - position.x) < .025 && Math.abs(tap.position.y - position.y) < .035) {
      cancelTap();
      sendInput({kind: "click", ...position, button: 1, clicks: 2}, value);
      return;
    }
    cancelTap();
    tap = {position, frame: {...value}, time: Date.now()};
    tapTimer = setTimeout(() => {
      const value = tap;
      tap = null;
      if (value) { mark(value.position); sendInput({kind: "click", ...value.position, button: 1, clicks: 1}, value.frame); }
    }, 300);
  }

  function pointerDown(event) {
    const position = point(event.clientX, event.clientY);
    if ((!authenticated || !frameLoaded || !fresh() || !position) && !pureMode) return;
    pointers.set(event.pointerId, {clientX: event.clientX, clientY: event.clientY, position, frame: frame ? {...frame} : null, button: event.button});
    if (pureMode && pointers.size === 3) {
      cancelTap(); gesture = true; pinch = null;
      exitGesture = {ids: new Set(pointers.keys()), positions: new Map([...pointers].map(([id, value]) => [id, {...value}])),
        time: Date.now(), valid: true};
      event.preventDefault();
      return;
    }
    if (exitGesture) { exitGesture.valid = false; gesture = true; return; }
    if (pointers.size > 1) {
      cancelTap();
      gesture = true;
      const pair = [...pointers.values()];
      const bounds = frame ? imageRect(ui.stage.getBoundingClientRect(), frame.width, frame.height) : {width: 1};
      pinch = {distance: Math.hypot(pair[0].clientX - pair[1].clientX, pair[0].clientY - pair[1].clientY), scale: scale || bounds.width / (frame?.width || 1)};
    } else gesture = false;
    if (ui["pointer-mode"].value !== "browse") {
      event.preventDefault();
      try { ui.viewport.setPointerCapture(event.pointerId); } catch (_) {}
    }
  }

  function pointerMove(event) {
    if (!pointers.has(event.pointerId)) return;
    if (exitGesture) {
      const start = exitGesture.positions.get(event.pointerId);
      if (start && Math.hypot(event.clientX - start.clientX, event.clientY - start.clientY) > 12) exitGesture.valid = false;
      event.preventDefault();
      return;
    }
    if (pointers.size > 1 && pinch && ui["pointer-mode"].value !== "browse") {
      const moving = pointers.get(event.pointerId);
      moving.currentX = event.clientX;
      moving.currentY = event.clientY;
      const pair = [...pointers.values()];
      const distance = Math.hypot((pair[0].currentX ?? pair[0].clientX) - (pair[1].currentX ?? pair[1].clientX),
        (pair[0].currentY ?? pair[0].clientY) - (pair[1].currentY ?? pair[1].clientY));
      if (pinch.distance > 0) {
        scale = Math.max(.15, Math.min(3, pinch.scale * distance / pinch.distance));
        ui.zoom.value = "custom";
        layoutImage();
      }
      event.preventDefault();
    }
  }

  function pointerUp(event) {
    const start = pointers.get(event.pointerId);
    pointers.delete(event.pointerId);
    if (!pointers.size) pinch = null;
    if (exitGesture) {
      const original = exitGesture.positions.get(event.pointerId);
      if (original && Math.hypot(event.clientX - original.clientX, event.clientY - original.clientY) > 12) exitGesture.valid = false;
      exitGesture.ids.delete(event.pointerId);
      const done = !exitGesture.ids.size;
      const shouldExit = done && exitGesture.valid && Date.now() - exitGesture.time < 650;
      event.preventDefault();
      if (done) {
        clearTouches();
        if (shouldExit) exitPureScreen();
      }
      return;
    }
    if (!start || gesture || ui["pointer-mode"].value === "browse") return;
    event.preventDefault();
    const end = point(event.clientX, event.clientY, start.frame);
    if (!start.position || !start.frame || !end || !canInput()) return;
    const mode = ui["pointer-mode"].value;
    const distance = Math.hypot(event.clientX - start.clientX, event.clientY - start.clientY);
    if (mode === "drag" && distance > 6) {
      cancelTap();
      sendInput({kind: "drag", ...start.position, to_x: end.x, to_y: end.y, button: 1}, start.frame);
    } else if (mode === "scroll" && distance > 6) {
      cancelTap();
      sendInput({kind: "scroll", ...start.position,
        delta_x: Math.max(-1200, Math.min(1200, Math.round((start.clientX - event.clientX) / 12))),
        delta_y: Math.max(-1200, Math.min(1200, Math.round((start.clientY - event.clientY) / 12)))}, start.frame);
    } else if (distance <= 6 && mode !== "scroll") {
      queueTap(end, start.frame, mode === "right" || start.button === 2 ? 3 : start.button === 1 ? 2 : 1);
    }
  }

  ui.viewport.addEventListener("pointerdown", pointerDown);
  ui.viewport.addEventListener("pointermove", pointerMove);
  ui.viewport.addEventListener("pointerup", pointerUp);
  ui.viewport.addEventListener("pointercancel", event => {
    if (exitGesture) clearTouches();
    else { pointers.delete(event.pointerId); gesture = true; cancelTap(); }
  });
  ui.viewport.addEventListener("contextmenu", event => event.preventDefault());
  ui.viewport.addEventListener("wheel", event => {
    if (ui["pointer-mode"].value === "browse") return;
    const position = point(event.clientX, event.clientY);
    if (!position || !canInput()) return;
    event.preventDefault();
    cancelTap();
    const factor = event.deltaMode === 1 ? 3 : event.deltaMode === 2 ? 60 : 1 / 12;
    const delta_x = Math.max(-1200, Math.min(1200, Math.round(event.deltaX * factor)));
    const delta_y = Math.max(-1200, Math.min(1200, Math.round(event.deltaY * factor)));
    if (delta_x || delta_y) sendInput({kind: "scroll", ...position, delta_x, delta_y});
  }, {passive: false});

  ui["text-form"].addEventListener("submit", event => {
    event.preventDefault();
    const text = ui["remote-text"].value;
    if (!text.trim()) return;
    if (text.length > 20000) { notice("一次最多输入 20,000 个字符。", true); return; }
    cancelTap();
    sendInput({kind: "text", text}, frame, text);
  });
  ui["remote-text"].addEventListener("input", () => { save(); controls(); });
  ui["remote-text"].addEventListener("focus", syncViewport);
  ui["remote-text"].addEventListener("blur", syncViewport);
  ui["remote-text"].addEventListener("keydown", event => {
    if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
      event.preventDefault();
      ui["text-form"].requestSubmit();
    }
  });
  for (const button of keyButtons) button.addEventListener("click", () => { cancelTap(); sendInput({kind: "key", key: button.dataset.key}); });
  ui["send-key"].addEventListener("click", () => { cancelTap(); sendInput({kind: "key", key: ui["extra-key"].value}); });
  ui["query-input"].addEventListener("click", queryInput);
  ui.refresh.addEventListener("click", refreshManually);
  ui.zoom.addEventListener("change", () => {
    if (ui.zoom.value !== "custom") scale = ui.zoom.value === "fit" ? 0 : Number(ui.zoom.value);
    cancelTap(); layoutImage();
  });
  ui["pointer-mode"].addEventListener("change", () => {
    cancelTap(); pointers.clear(); gesture = false;
    const mode = ui["pointer-mode"].value;
    ui.viewport.classList.toggle("browsing", mode === "browse");
    $("mode-hint").textContent = {click: "轻点单击，连续轻点双击。多指手势只缩放画面。", right: "轻点画面发送右键。多指手势只缩放画面。",
      drag: "按住后滑动再松手，在 Ubuntu 中拖动物体。多指手势只缩放画面。", scroll: "单指上下 / 左右滑动，滚动 Ubuntu 内容。多指手势只缩放画面。",
      browse: "此模式只浏览和缩放画面，不会向 Ubuntu 发送点击。"}[mode];
  });
  ui["settings-toggle"].addEventListener("click", () => {
    ui["desktop-tools"].hidden = !ui["desktop-tools"].hidden;
    ui["settings-toggle"].setAttribute("aria-expanded", String(!ui["desktop-tools"].hidden));
  });
  ui.fullscreen.addEventListener("click", enterPureScreen);
  document.addEventListener("fullscreenchange", fullscreenChanged);
  document.addEventListener("webkitfullscreenchange", fullscreenChanged);
  document.addEventListener("keydown", event => {
    if (pureMode && event.key === "Escape") { event.preventDefault(); exitPureScreen(); }
  });

  document.addEventListener("visibilitychange", () => {
    clearTouches();
    if (document.hidden) clearTimeout(frameTimer);
    else if (authenticated) { controls(); scheduleFrame(0); }
  });
  window.addEventListener("pagehide", event => {
    stopped = true;
    clearTouches(); clearTimeout(frameTimer); save();
    if (authenticated && !event.persisted) {
      fetch("/api/desktop/close", {method: "POST", credentials: "same-origin", keepalive: true,
        headers: {"Content-Type": "application/json", "X-CSRF-Token": csrf}, body: JSON.stringify({viewer_id: viewerId})}).catch(() => {});
    }
  });
  window.addEventListener("pageshow", event => {
    if (event.persisted) { stopped = false; controls(); scheduleFrame(0); }
  });
  window.addEventListener("resize", syncViewport);
  window.visualViewport?.addEventListener("resize", syncViewport);
  window.visualViewport?.addEventListener("scroll", syncViewport);
  setInterval(controls, 1000);

  async function initialise() {
    save(); controls(); syncViewport();
    try {
      const state = await api("/api/state");
      if (typeof state.csrf !== "string" || !state.csrf) throw new ApiError("设备登录状态未确认，请返回 Claude Bridge。");
      csrf = state.csrf;
      authenticated = true;
      ui["login-required"].hidden = true;
      controls();
      if (pending) {
        notice("已恢复上次未确认的操作，只查询原请求，不会重新执行。");
        await queryInput();
      }
      scheduleFrame(0);
    } catch (error) {
      if (error.status !== 401) {
        notice(`${error.message} 可刷新此页重新连接，文字已保留。`, true);
        ui["empty-frame"].textContent = "Ubuntu 桌面尚未连接。";
      }
    }
  }
  initialise();
})();
