/* SliderWeb UI — talks /ws JSON; falls back to GET /api/status. */
(function () {
  "use strict";

  var TAP_MS = 333;
  var MARK_MS = 1000;
  var HALT_MS = 1000;
  var DISABLE_MS = 2000;
  var GAMMA = 2.0;
  var SS_MS = 80;
  var WDT_MS = 1000;
  var MARKS_KEY = "sw_marks";
  var SWAP_DIR_KEY = "sw_swap_dir";
  var SWAP_DIR2_KEY = "sw_swap_dir2";
  var AXIS_MASK_KEY = "sw_axis_mask";
  var TL_MSM_KEY = "sw_tl_msm";
  var CLI_CMDS_KEY = "sw_cli_cmds";
  var PPM_NEAR_MM = 1.0;

  var ws = null;
  var pollTimer = null;
  var wdtTimer = null;
  var offlineSince = 0;
  var lastStatus = {};
  var draggingSpeed = false;
  var draggingAccel = false;
  var ssTimer = 0;
  var saTimer = 0;
  var axisMask = 1;
  var spdMin = 1;
  var spdMax = 100;
  var accMin = 1;
  var accMax = 500;
  var held = {};
  var cmdSpd = 40;
  var cmdAcc = null;
  var swapDir = false;
  var swapDir2 = false;
  var syncEnableSilent = false;
  var unitPos = "mm";
  var unitSpd = "mm/s";
  var unitAcc = "mm/s²";
  var unitPos2 = "mm";
  var unitSpd2 = "mm/s";
  var unitAcc2 = "mm/s²";
  var optionHeld = false;
  var cruise = { locked: false, dir: 0, axis: 1 };
  var marks = { a: null, b: null, c: null };
  var cfgCache = {};
  var softLimits = { min: null, max: null, min2: null, max2: null };
  var session = { enabled: true, ss: 40, sa: null };
  var activeTask = null;
  var abcChordLatch = false;

  function $(id) {
    return document.getElementById(id);
  }

  /** ETA seconds for distance at speed. Accel ignored for now. */
  function etaSeconds(distance, speed, accel) {
    var d = Math.abs(Number(distance));
    var s = Number(speed);
    if (!(d > 0) || !(s > 0) || isNaN(d) || isNaN(s)) return null;
    return d / s;
  }

  /** Speed from time and distance. Accel ignored for now. */
  function speedFromTime(time, distance, accel) {
    var t = Number(time);
    var d = Math.abs(Number(distance));
    if (!(t > 0) || !(d >= 0) || isNaN(t) || isNaN(d)) return null;
    return d / t;
  }

  function deriveAxisState(global, spd, acc, tgt) {
    var st = global || "?";
    if (st === "E" || st === "L" || st === "D" || st === "H") return st;
    if (st === "?") return "?";
    var v = spd != null && !isNaN(spd) ? Math.abs(Number(spd)) : 0;
    if (v > 0.05) {
      if (st === "A" || st === "B") return st;
      return "M";
    }
    if (tgt != null && !isNaN(Number(tgt))) return "M";
    return "I";
  }

  function setStateLetter(el, letter, subtle) {
    if (!el) return;
    el.textContent = letter || "?";
    el.classList.toggle("dim", !!subtle);
  }

  function setNum(el, v, d) {
    if (!el) return;
    var intEl = el.querySelector(".int");
    var fracEl = el.querySelector(".frac");
    if (!intEl || !fracEl) return;
    d = d == null ? 1 : d;
    if (v === null || v === undefined || v === "") {
      intEl.textContent = "—";
      fracEl.textContent = "—";
      return;
    }
    var n = Number(v);
    if (isNaN(n)) {
      intEl.textContent = "—";
      fracEl.textContent = "—";
      return;
    }
    var s = n.toFixed(d);
    var i = s.indexOf(".");
    if (i < 0) {
      intEl.textContent = s;
      fracEl.textContent = "0";
      return;
    }
    intEl.textContent = s.slice(0, i);
    fracEl.textContent = s.slice(i + 1);
  }

  function sliderToSpeed(t) {
    t = Math.max(0, Math.min(1, t));
    return spdMin + (spdMax - spdMin) * Math.pow(t, GAMMA);
  }

  function speedToSlider(v) {
    var u = (Number(v) - spdMin) / Math.max(1e-9, spdMax - spdMin);
    u = Math.max(0, Math.min(1, u));
    return Math.pow(u, 1 / GAMMA) * 1000;
  }

  function speedSliders() {
    return [
      $("spdSlider"),
      $("spdSliderHome"),
      $("spdSliderAbc"),
      $("spdSliderJoy"),
      $("spdSliderCli"),
    ].filter(Boolean);
  }

  function speedLabels() {
    return [$("ssVal"), $("ssValHome"), $("ssValAbc"), $("ssValJoy"), $("ssValCli")].filter(Boolean);
  }

  function updateAccBounds() {
    if (cfgCache.min_speed != null) accMin = Number(cfgCache.min_speed);
    else if (cfgCache.spd_min != null) accMin = Number(cfgCache.spd_min);
    else accMin = 1;
    if (cfgCache.max_accel != null) accMax = Number(cfgCache.max_accel);
    else accMax = 500;
    if (isNaN(accMin) || accMin < 0.001) accMin = 1;
    if (isNaN(accMax) || accMax < accMin) accMax = Math.max(accMin, 500);
  }

  function sliderToAccel(t) {
    t = Math.max(0, Math.min(1, t));
    return accMin + (accMax - accMin) * t;
  }

  function accelToSlider(v) {
    var u = (Number(v) - accMin) / Math.max(1e-9, accMax - accMin);
    u = Math.max(0, Math.min(1, u));
    return Math.round(u * 1000);
  }

  function syncAccelUi(v, sliderEl) {
    cmdAcc = Number(v);
    if (isNaN(cmdAcc)) return;
    var sv = String(accelToSlider(cmdAcc));
    var el = sliderEl || $("accSliderHome");
    if (el) el.value = sv;
    setNum($("accValHome"), cmdAcc);
  }

  function emitSa(force) {
    var el = $("accSliderHome");
    if (!el) return;
    var t = Number(el.value) / 1000;
    var v = sliderToAccel(t);
    syncAccelUi(v, el);
    session.sa = v;
    cmdAcc = v;
    var now = Date.now();
    if (!force && now - saTimer < SS_MS) return;
    saTimer = now;
    sendMc("SA " + fmtSs(v));
  }

  function fmtCfg(v) {
    if (v == null || v === "" || isNaN(Number(v))) return "—";
    return Number(v).toFixed(1);
  }

  function motorCount(cfg) {
    cfg = cfg || {};
    if (cfg.motors != null && cfg.motors !== "") {
      var n = Number(cfg.motors);
      if (n >= 1) return n;
    }
    return Number(cfg.axis_count || 1);
  }

  function normalizeUnit(raw) {
    var u = String(raw != null ? raw : "mm").trim();
    if (!u) u = "mm";
    var low = u.toLowerCase();
    if (low === "deg" || low === "degree" || low === "degrees" || u === "°") return "°";
    return u;
  }

  function applyUnitsFromConfig(cfg) {
    cfg = cfg || cfgCache || {};
    var u1 = normalizeUnit(cfg.unit_name || cfg.unit || "mm");
    unitPos = u1;
    unitSpd = u1 + "/s";
    unitAcc = u1 + "/s²";
    var dual = motorCount(cfg) >= 2;
    var u2 = u1;
    if (dual) {
      if (cfg.unit_name_2 != null && String(cfg.unit_name_2).trim()) {
        u2 = normalizeUnit(cfg.unit_name_2);
      }
    }
    unitPos2 = u2;
    unitSpd2 = u2 + "/s";
    unitAcc2 = u2 + "/s²";
    updateUnitLabels();
  }

  function updateUnitLabels() {
    if ($("posUnit")) $("posUnit").textContent = unitPos;
    if ($("spdUnit")) $("spdUnit").textContent = unitSpd;
    if ($("accUnit")) $("accUnit").textContent = unitAcc;
    if ($("pos2Unit")) $("pos2Unit").textContent = unitPos2;
    if ($("spd2Unit")) $("spd2Unit").textContent = unitSpd2;
    if ($("acc2Unit")) $("acc2Unit").textContent = unitAcc2;
    document.querySelectorAll(".slider-meta .unit[data-unit]").forEach(function (el) {
      var kind = el.getAttribute("data-unit");
      var ax = Number(el.getAttribute("data-axis") || 1);
      if (kind === "spd") el.textContent = ax === 2 ? unitSpd2 : unitSpd;
      else if (kind === "acc") el.textContent = ax === 2 ? unitAcc2 : unitAcc;
      else if (kind === "pos") el.textContent = ax === 2 ? unitPos2 : unitPos;
    });
  }

  function buildInfoBox() {
    var body = document.querySelector("#infoBox .info-body");
    if (!body) return;
    var c = cfgCache;
    var name = c.name != null && String(c.name).trim() ? String(c.name).trim() : "Slider";
    var dual = motorCount(c) >= 2;
    var sizeLine =
      "Slider size: " +
      fmtCfg(c.slider_min) +
      " - " +
      fmtCfg(c.slider_max) +
      " " +
      unitPos;
    if (dual && c.slider_min_2 != null && c.slider_max_2 != null) {
      sizeLine +=
        " / " +
        fmtCfg(c.slider_min_2) +
        " - " +
        fmtCfg(c.slider_max_2) +
        " " +
        unitPos2;
    }
    var spdLine = "Max speed: " + fmtCfg(c.max_speed) + " " + unitSpd;
    if (dual && c.max_speed_2 != null) {
      if (unitSpd2 === unitSpd) {
        spdLine += " / " + fmtCfg(c.max_speed_2);
      } else {
        spdLine += " / " + fmtCfg(c.max_speed_2) + " " + unitSpd2;
      }
    }
    var accLine = "Max accel: " + fmtCfg(c.max_accel) + " " + unitAcc;
    if (dual && c.max_accel_2 != null) {
      if (unitAcc2 === unitAcc) {
        accLine += " / " + fmtCfg(c.max_accel_2);
      } else {
        accLine += " / " + fmtCfg(c.max_accel_2) + " " + unitAcc2;
      }
    }
    body.textContent =
      "Name: " + name + "\n" + sizeLine + "\n" + spdLine + "\n" + accLine;
  }

  function isDrvError() {
    return lastStatus.state === "E" || !!lastStatus.warn;
  }

  function syncEnableUi(enabled) {
    var inp = $("enable");
    var wrap = inp && inp.closest(".cell-switch");
    if (!inp) return;
    syncEnableSilent = true;
    inp.checked = !!enabled;
    syncEnableSilent = false;
    var block = isDrvError();
    inp.disabled = block;
    if (wrap) wrap.classList.toggle("disabled", block);
  }

  function loadSwapDirs() {
    try {
      swapDir = localStorage.getItem(SWAP_DIR_KEY) === "1";
      swapDir2 = localStorage.getItem(SWAP_DIR2_KEY) === "1";
    } catch (e) {}
    var d1 = $("dir");
    var d2 = $("dir2");
    if (d1) d1.checked = swapDir;
    if (d2) d2.checked = swapDir2;
  }

  function saveSwapDirs() {
    try {
      localStorage.setItem(SWAP_DIR_KEY, swapDir ? "1" : "0");
      localStorage.setItem(SWAP_DIR2_KEY, swapDir2 ? "1" : "0");
    } catch (e) {}
  }

  function syncAxisMaskUi() {
    document.querySelectorAll(".chip").forEach(function (el) {
      el.classList.toggle("active", Number(el.getAttribute("data-ax")) === axisMask);
    });
  }

  function loadAxisMask() {
    try {
      var v = parseInt(localStorage.getItem(AXIS_MASK_KEY), 10);
      if (v === 0 || v === 1 || v === 2) axisMask = v;
    } catch (e) {}
    syncAxisMaskUi();
  }

  function saveAxisMask() {
    try {
      localStorage.setItem(AXIS_MASK_KEY, String(axisMask));
    } catch (e) {}
  }

  function sendAx(mask) {
    send({ ax: mask });
  }

  function uiAxisDir(sign, axis) {
    var swap = axis === 2 ? swapDir2 : swapDir;
    return sign * (swap ? -1 : 1);
  }

  function softSideFromBtn(isLeft, axis) {
    return uiAxisDir(isLeft ? -1 : 1, axis) < 0 ? "min" : "max";
  }

  function syncSpeedUi(v, sliderEl) {
    cmdSpd = Number(v);
    if (isNaN(cmdSpd)) return;
    var sv = String(Math.round(speedToSlider(cmdSpd)));
    speedSliders().forEach(function (el) {
      if (el !== sliderEl) el.value = sv;
    });
    if (sliderEl) sliderEl.value = sv;
    speedLabels().forEach(function (el) {
      setNum(el, cmdSpd);
    });
    updateEtas();
  }

  function loadMarks() {
    try {
      var raw = localStorage.getItem(MARKS_KEY);
      if (!raw) return;
      var o = JSON.parse(raw);
      if (o && typeof o === "object") {
        if (o.a != null) marks.a = Number(o.a);
        if (o.b != null) marks.b = Number(o.b);
        if (o.c != null) marks.c = Number(o.c);
      }
    } catch (e) {}
  }

  function saveMarks() {
    try {
      localStorage.setItem(MARKS_KEY, JSON.stringify(marks));
    } catch (e) {}
  }

  function send(obj) {
    var s = JSON.stringify(obj);
    if (ws && ws.readyState === 1) {
      try {
        ws.send(s);
        return true;
      } catch (e) {}
    }
    fetch("/api/cmd", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: s,
    }).catch(function () {});
    return true;
  }

  function sendMc(line) {
    return send({ mc: String(line) });
  }

  function sendTask(line) {
    return send({ task: String(line) });
  }

  function sendWdt() {
    return send({ wdt: "alive" });
  }

  function applyHello(d) {
    if (!d || typeof d !== "object") return;
    if (d.config && typeof d.config === "object") {
      cfgCache = d.config;
      if (cfgCache.max_speed != null) spdMax = Number(cfgCache.max_speed);
      if (cfgCache.spd_min != null) spdMin = Number(cfgCache.spd_min);
      else if (cfgCache.min_speed != null) spdMin = Number(cfgCache.min_speed);
      updateAccBounds();
      applyUnitsFromConfig(cfgCache);
      buildInfoBox();
    }
    if (d.soft && typeof d.soft === "object") {
      softLimits = {
        min: d.soft.min != null ? Number(d.soft.min) : null,
        max: d.soft.max != null ? Number(d.soft.max) : null,
        min2: d.soft.min2 != null ? Number(d.soft.min2) : null,
        max2: d.soft.max2 != null ? Number(d.soft.max2) : null,
      };
    }
    if (d.session && typeof d.session === "object") {
      session.enabled = !!d.session.enabled;
      syncEnableUi(session.enabled);
      if (d.session.ss != null) {
        session.ss = Number(d.session.ss);
        cmdSpd = session.ss;
        if (!draggingSpeed) syncSpeedUi(session.ss, null);
      }
      if (d.session.sa != null) {
        session.sa = Number(d.session.sa);
        cmdAcc = session.sa;
        if (!draggingAccel) syncAccelUi(session.sa, null);
      }
    }
    activeTask = d.task || null;
    if (d.linked === false && !d.sim) {
      /* soft gate: OLED will show No MC / linking via status */
    }
    updateEtas();
  }

  function applyStatus(d) {
    if (!d || typeof d !== "object") return;
    if (d.t === "hello") {
      applyHello(d);
      return;
    }
    lastStatus = d;
    if (d.soft && typeof d.soft === "object") {
      softLimits.min = d.soft.min != null ? Number(d.soft.min) : softLimits.min;
      softLimits.max = d.soft.max != null ? Number(d.soft.max) : softLimits.max;
      softLimits.min2 = d.soft.min2 != null ? Number(d.soft.min2) : softLimits.min2;
      softLimits.max2 = d.soft.max2 != null ? Number(d.soft.max2) : softLimits.max2;
    } else {
      if (d.soft_min != null) softLimits.min = Number(d.soft_min);
      if (d.soft_max != null) softLimits.max = Number(d.soft_max);
      if (d.soft_min_2 != null) softLimits.min2 = Number(d.soft_min_2);
      if (d.soft_max_2 != null) softLimits.max2 = Number(d.soft_max_2);
    }
    if (d.session && typeof d.session === "object") {
      session.enabled = !!d.session.enabled;
      syncEnableUi(session.enabled);
      if (d.session.ss != null) session.ss = Number(d.session.ss);
      if (d.session.sa != null) {
        session.sa = Number(d.session.sa);
        cmdAcc = session.sa;
        if (!draggingAccel) syncAccelUi(session.sa, null);
      }
    }
    if (d.task !== undefined) activeTask = d.task;
    setNum($("pos"), d.pos);
    setNum($("spd"), d.spd);
    setNum($("acc"), d.acc);
    var gst = d.state || "?";
    $("line1").innerHTML = d.line1 ? d.line1 : "&nbsp;";
    $("line2").innerHTML = d.line2 ? d.line2 : "&nbsp;";
    $("oled").classList.toggle("warn", !!d.warn);
    syncEnableUi(session.enabled);
    var axes = d.axes || 1;
    if (cfgCache.motors != null) axes = Number(cfgCache.motors) || axes;
    else if (cfgCache.axis_count != null) axes = Number(cfgCache.axis_count) || axes;
    var dual = axes >= 2;
    document.body.classList.toggle("axes-2", dual);
    $("tele2").classList.toggle("hidden", !dual);
    document.querySelectorAll(".axis2-only").forEach(function (el) {
      el.classList.toggle("hidden", !dual);
    });
    if (dual) {
      setNum($("pos2"), d.pos2);
      setNum($("spd2"), d.spd2);
      setNum($("acc2"), d.acc2);
      var s1 =
        d.state1 != null ? String(d.state1) : deriveAxisState(gst, d.spd, d.acc, d.tgt);
      var s2 =
        d.state2 != null
          ? String(d.state2)
          : deriveAxisState(gst, d.spd2, d.acc2, d.tgt2);
      setStateLetter($("state"), s1, s1 === "I");
      setStateLetter($("state2"), s2, s2 === "I");
    } else {
      setStateLetter($("state"), gst, false);
    }
    if (d.spd_min != null) spdMin = Number(d.spd_min);
    if (d.max_speed != null) spdMax = Number(d.max_speed);
    else if (cfgCache.max_speed != null) spdMax = Number(cfgCache.max_speed);
    if (
      !draggingSpeed &&
      d.ss != null &&
      !optionHeld &&
      !held.FAST_L &&
      !held.FAST_R
    ) {
      syncSpeedUi(d.ss, null);
      session.ss = Number(d.ss);
      cmdSpd = session.ss;
    }
    if (d.ax != null) {
      axisMask = Number(d.ax);
      if (axisMask !== 0 && axisMask !== 1 && axisMask !== 2) axisMask = 1;
      syncAxisMaskUi();
    }
    if (d.wifi) {
      var w = d.wifi;
      $("wifiHint").textContent =
        (w.mode || "") +
        "  " +
        (w.ip || "") +
        (w.ap_ssid ? "  AP " + w.ap_ssid : "");
    }
    updateEtas();
  }

  function softMin() {
    var v = softLimits.min;
    if (v === null || v === undefined || isNaN(v)) return null;
    return Number(v);
  }

  function softMax() {
    var v = softLimits.max;
    if (v === null || v === undefined || isNaN(v)) return null;
    return Number(v);
  }

  function fmtSs(v) {
    return Math.round(Number(v) * 100) / 100;
  }

  function fmtTlSpeed(v) {
    var n = Number(v);
    if (isNaN(n)) return "0.000000";
    return n.toFixed(6);
  }

  function activeTabId() {
    var t = document.querySelector(".tab.active");
    return t ? t.getAttribute("data-tab") : "home";
  }

  function isTlTab() {
    return activeTabId() === "tl";
  }

  function effectiveSpeed() {
    return optionHeld ? spdMax : cmdSpd;
  }

  function jogCmd(dir, axis) {
    dir = uiAxisDir(dir, axis || 1);
    var pct = dir < 0 ? -100 : 100;
    if (axis === 2) return "MJ 0 " + pct;
    if (document.body.classList.contains("axes-2")) {
      if (axisMask === 2) return "MJ 0 " + pct;
      if (axisMask === 0) return "MJ " + pct + " " + pct;
      return "MJ " + pct + " 0";
    }
    return "MJ " + pct;
  }

  function clearCruise() {
    cruise.locked = false;
    cruise.dir = 0;
    cruise.axis = 1;
  }

  function stopMotion() {
    sendMc("MS");
    clearCruise();
  }

  function startJog(dir, axis) {
    var spd = effectiveSpeed();
    sendMc("SS " + fmtSs(spd));
    sendMc(jogCmd(dir, axis));
    cruise.dir = dir;
    cruise.axis = axis;
    cruise.locked = false;
  }

  function curPos() {
    var v = lastStatus.pos;
    return v === null || v === undefined ? null : Number(v);
  }

  function curPos2() {
    var v = lastStatus.pos2;
    return v === null || v === undefined ? null : Number(v);
  }

  function fmtPosMc(v) {
    if (v == null || isNaN(v)) return null;
    return fmtSs(v);
  }

  function setSoftLimit(side, axis, pos) {
    var p = fmtPosMc(pos);
    if (side === "min") {
      if (axis === 2) sendMc(p != null ? "SL _ " + p : "SL _ none");
      else sendMc(p != null ? "SL " + p : "SL none");
    } else {
      if (axis === 2) sendMc(p != null ? "SR _ " + p : "SR _ none");
      else sendMc(p != null ? "SR " + p : "SR none");
    }
  }

  function resetSoftBoth(side) {
    if (side === "min") {
      sendMc("SL none");
      sendMc("SL _ none");
    } else {
      sendMc("SR none");
      sendMc("SR _ none");
    }
  }

  function handleSetWin(isLeft, axis) {
    var side = softSideFromBtn(isLeft, axis);
    var pos = axis === 2 ? curPos2() : curPos();
    if (pos == null || isNaN(pos)) return;
    setSoftLimit(side, axis, pos);
  }

  function handleResetWin(isLeft) {
    var side = softSideFromBtn(isLeft, 1);
    resetSoftBoth(side);
  }

  function updateEtas() {
    var pos = curPos();
    var mn = softMin();
    var mx = softMax();
    var spd = cmdSpd;

    if (pos != null && !isNaN(pos) && mn != null && !isNaN(mn) && pos > mn) {
      setNum($("etaMoveL"), etaSeconds(pos - mn, spd, null));
    } else {
      setNum($("etaMoveL"), null);
    }
    if (pos != null && !isNaN(pos) && mx != null && !isNaN(mx) && mx > pos) {
      setNum($("etaMoveR"), etaSeconds(mx - pos, spd, null));
    } else {
      setNum($("etaMoveR"), null);
    }

    function markEta(id, mark) {
      if (pos == null || isNaN(pos) || mark == null || isNaN(mark)) {
        setNum($(id), null);
        return;
      }
      setNum($(id), etaSeconds(mark - pos, spd, null));
    }
    markEta("etaA", marks.a);
    markEta("etaB", marks.b);
    markEta("etaC", marks.c);
    markEta("etaTlA", marks.a);
    markEta("etaTlB", marks.b);
    markEta("etaTlC", marks.c);

    if (mn != null && !isNaN(mn) && mx != null && !isNaN(mx) && mx > mn) {
      setNum($("etaWin"), etaSeconds(mx - mn, spd, null));
    } else {
      setNum($("etaWin"), null);
    }
  }

  function emitSs(force, fromEl) {
    var el = fromEl || $("spdSlider");
    var t = Number(el.value) / 1000;
    var v = sliderToSpeed(t);
    syncSpeedUi(v, el);
    session.ss = v;
    cmdSpd = v;
    var now = Date.now();
    if (!force && now - ssTimer < SS_MS) return;
    ssTimer = now;
    sendMc("SS " + fmtSs(v));
  }

  function bindSpeedSlider(el) {
    if (!el) return;
    el.addEventListener("pointerdown", function () {
      draggingSpeed = true;
    });
    el.addEventListener("pointerup", function () {
      draggingSpeed = false;
      emitSs(true, el);
    });
    el.addEventListener("input", function () {
      emitSs(false, el);
    });
  }

  function setMark(letter, pos) {
    if (pos == null || isNaN(pos)) return;
    marks[letter] = pos;
    saveMarks();
    updateEtas();
  }

  function gotoMark(letter) {
    var pos = marks[letter];
    if (pos == null || isNaN(pos)) return;
    var spd = optionHeld ? spdMax : cmdSpd;
    sendMc("SS " + fmtSs(spd));
    sendMc("MT " + fmtSs(pos));
    clearCruise();
  }

  function showUiError(msg) {
    var l1 = $("line1");
    var oled = $("oled");
    if (l1) l1.textContent = msg;
    if (oled) oled.classList.add("warn");
    setTimeout(function () {
      if (oled) oled.classList.remove("warn");
    }, 1600);
  }

  function loopWaitSec() {
    var inp = $("loopWaitAbc");
    var t = Number(inp && inp.value);
    if (isNaN(t) || t < 0) t = 0;
    if (t > 100) t = 100;
    if (inp) inp.value = String(t);
    return t;
  }

  /** Start ping-pong between two marks (letters 'a'|'b'|'c'). */
  function startPpmPair(let1, let2) {
    var p1 = marks[let1];
    var p2 = marks[let2];
    if (p1 == null || isNaN(p1) || p2 == null || isNaN(p2)) {
      showUiError("Set marks first");
      return false;
    }
    if (Math.abs(p1 - p2) < PPM_NEAR_MM) {
      showUiError("Ends too close");
      return false;
    }
    var pos = curPos();
    var first;
    var second;
    // If already at first mark, go to the other end first.
    if (pos != null && !isNaN(pos) && Math.abs(pos - p1) <= PPM_NEAR_MM) {
      first = p2;
      second = p1;
    } else {
      first = p1;
      second = p2;
    }
    var delay = loopWaitSec();
    sendTask(
      "TSK_PPM " +
        fmtSs(first) +
        " _ " +
        fmtSs(second) +
        " _ " +
        fmtSs(delay)
    );
    return true;
  }

  function chordPair() {
    var a = !!held.A;
    var b = !!held.B;
    var c = !!held.C;
    if (a && b && !c) return ["a", "b"];
    if (b && c && !a) return ["b", "c"];
    if (c && a && !b) return ["c", "a"];
    return null;
  }

  function tlTriggerTime() {
    var factor = Number($("TL_FACTOR") && $("TL_FACTOR").value);
    var fps = Number($("TL_FPS") && $("TL_FPS").value);
    if (isNaN(factor) || factor < 3) factor = 3;
    if (isNaN(fps) || fps < 1) fps = 1;
    var t = factor / fps;
    if (t < 0.2) t = 0.2;
    return t;
  }

  function tlExposureSec() {
    var inp = $("TL_EXPOSURE");
    var t = Number(inp && inp.value);
    if (isNaN(t) || t < 0.1) t = 0.1;
    if (t > 60) t = 60;
    if (inp) inp.value = String(t);
    return t;
  }

  function calcMsmFrames(delta) {
    var factor = Number($("TL_FACTOR") && $("TL_FACTOR").value);
    var fps = Number($("TL_FPS") && $("TL_FPS").value);
    if (isNaN(factor) || factor < 3) factor = 3;
    if (isNaN(fps) || fps < 1) fps = 1;
    if (!(cmdSpd > 0) || !(delta >= PPM_NEAR_MM)) return 0;
    var tlSpeed = cmdSpd / factor;
    return Math.max(1, Math.ceil((delta / tlSpeed) * fps));
  }

  function startTimelapse(letter) {
    var dest = marks[letter];
    if (dest == null || isNaN(dest)) {
      showUiError("Set marks first");
      return false;
    }
    var pos = curPos();
    if (pos == null || isNaN(pos)) {
      showUiError("No position");
      return false;
    }
    var delta = Math.abs(dest - pos);
    if (delta < PPM_NEAR_MM) {
      showUiError("Already there");
      return false;
    }
    var factor = Number($("TL_FACTOR") && $("TL_FACTOR").value);
    if (isNaN(factor) || factor < 3) factor = 3;
    if (!(cmdSpd > 0)) {
      showUiError("Set SPEED");
      return false;
    }
    var trigTime = tlTriggerTime();
    var trigLen = tlExposureSec();
    var msm = !!($("TL_MSM") && $("TL_MSM").checked);
    if (msm) {
      var frames = calcMsmFrames(delta);
      if (frames < 1) {
        showUiError("TL too close");
        return false;
      }
      sendTask(
        "TSK_TL_MSM " +
          fmtSs(dest) +
          " _ " +
          frames +
          " " +
          fmtTlSpeed(trigTime) +
          " " +
          fmtTlSpeed(trigLen)
      );
    } else {
      var spd = cmdSpd / factor;
      var acc =
        cmdAcc != null && !isNaN(cmdAcc) ? cmdAcc / factor : 100 / factor;
      sendTask(
        "TSK_TL_CONT " +
          fmtSs(dest) +
          " _ " +
          fmtTlSpeed(spd) +
          " " +
          fmtTlSpeed(acc) +
          " " +
          fmtTlSpeed(trigTime) +
          " " +
          fmtTlSpeed(trigLen)
      );
    }
    return true;
  }

  function timeToMark(letter) {
    var inp = $("timeAbc");
    var t = Number(inp && inp.value);
    if (!(t >= 0.1)) {
      t = 0.1;
      if (inp) inp.value = "0.1";
    }
    var pos = curPos();
    var mark = marks[letter];
    if (pos == null || isNaN(pos) || mark == null || isNaN(mark)) return;
    var spd = speedFromTime(t, mark - pos, null);
    if (spd == null) return;
    spd = Math.max(spdMin, Math.min(spdMax, spd));
    syncSpeedUi(spd, null);
    sendMc("SS " + fmtSs(spd));
  }

  function bindHold(el) {
    var name = el.getAttribute("data-btn");
    if (!name) return;
    var downAt = 0;
    var haltTimer = 0;
    var disTimer = 0;
    var markTimer = 0;
    var markSaved = false;

    function clearT() {
      if (haltTimer) clearTimeout(haltTimer);
      if (disTimer) clearTimeout(disTimer);
      if (markTimer) clearTimeout(markTimer);
      haltTimer = disTimer = markTimer = 0;
    }

    function up(ev) {
      if (!held[name]) return;
      held[name] = false;
      el.classList.remove("held");
      try {
        el.releasePointerCapture(ev.pointerId);
      } catch (e) {}
      var dt = Date.now() - downAt;
      clearT();

      if (name === "OPTION") {
        optionHeld = !!document.querySelector('[data-btn="OPTION"].held');
        if (cruise.locked && cruise.dir) {
          sendMc("SS " + fmtSs(effectiveSpeed()));
        }
        return;
      }

      if (name === "MOVE_L" || name === "MOVE_R") {
        var dir = name === "MOVE_L" ? -1 : 1;
        if (cruise.dir === dir && cruise.axis === 1) {
          if (dt <= TAP_MS) {
            cruise.locked = true;
          } else if (!cruise.locked) {
            stopMotion();
          }
        }
        return;
      }

      if (name === "MOVE_L2" || name === "MOVE_R2") {
        var dir2 = name === "MOVE_L2" ? -1 : 1;
        if (cruise.dir === dir2 && cruise.axis === 2) {
          if (dt <= TAP_MS) {
            cruise.locked = true;
          } else if (!cruise.locked) {
            stopMotion();
          }
        }
        return;
      }

      if (name === "FAST_L" || name === "FAST_R") {
        stopMotion();
        sendMc("SS " + fmtSs(cmdSpd));
        return;
      }

      if (name === "A" || name === "B" || name === "C") {
        if (isTlTab()) {
          if (!markSaved && dt < MARK_MS) {
            startTimelapse(name.toLowerCase());
          }
        } else if (!abcChordLatch && !markSaved && dt < MARK_MS) {
          gotoMark(name.toLowerCase());
        }
        if (!held.A && !held.B && !held.C) abcChordLatch = false;
        return;
      }
    }

    el.addEventListener("pointerdown", function (ev) {
      ev.preventDefault();
      held[name] = true;
      downAt = Date.now();
      markSaved = false;
      el.classList.add("held");
      try {
        el.setPointerCapture(ev.pointerId);
      } catch (e) {}
      clearT();

      if (name === "OPTION") {
        optionHeld = true;
        if (cruise.locked && cruise.dir) {
          sendMc("SS " + fmtSs(spdMax));
        }
        return;
      }

      if (name === "STOP") {
        stopMotion();
        haltTimer = setTimeout(function () {
          if (held[name]) sendMc("HT");
        }, HALT_MS);
        disTimer = setTimeout(function () {
          if (held[name]) sendMc("SE 0");
        }, DISABLE_MS);
        return;
      }

      if (name === "HOME") {
        sendMc("MH");
        clearCruise();
        return;
      }

      if (name === "MOVE_L" || name === "MOVE_R") {
        var dir = name === "MOVE_L" ? -1 : 1;
        if (cruise.locked && cruise.dir === dir && cruise.axis === 1) {
          stopMotion();
          return;
        }
        startJog(dir, 1);
        return;
      }

      if (name === "MOVE_L2" || name === "MOVE_R2") {
        var dir2 = name === "MOVE_L2" ? -1 : 1;
        if (cruise.locked && cruise.dir === dir2 && cruise.axis === 2) {
          stopMotion();
          return;
        }
        startJog(dir2, 2);
        return;
      }

      if (name === "FAST_L") {
        clearCruise();
        sendMc("SS " + fmtSs(spdMax));
        sendMc(jogCmd(-1, 1));
        cruise.dir = -1;
        cruise.axis = 1;
        cruise.locked = false;
        return;
      }
      if (name === "FAST_R") {
        clearCruise();
        sendMc("SS " + fmtSs(spdMax));
        sendMc(jogCmd(1, 1));
        cruise.dir = 1;
        cruise.axis = 1;
        cruise.locked = false;
        return;
      }

      if (name === "AB") {
        startPpmPair("a", "b");
        return;
      }
      if (name === "BC") {
        startPpmPair("b", "c");
        return;
      }
      if (name === "CA") {
        startPpmPair("c", "a");
        return;
      }

      if (name === "A" || name === "B" || name === "C") {
        if (!isTlTab()) {
          var pair = chordPair();
          if (pair) {
            abcChordLatch = true;
            startPpmPair(pair[0], pair[1]);
            return;
          }
        }
        var letter = name.toLowerCase();
        markTimer = setTimeout(function () {
          if (!held[name] || abcChordLatch) return;
          var p = curPos();
          if (p != null && !isNaN(p)) {
            setMark(letter, p);
            markSaved = true;
          }
        }, MARK_MS);
        return;
      }

      if (name === "T_A") {
        timeToMark("a");
        return;
      }
      if (name === "T_B") {
        timeToMark("b");
        return;
      }
      if (name === "T_C") {
        timeToMark("c");
        return;
      }

      if (name === "SET_W_L") {
        handleSetWin(true, 1);
        return;
      }
      if (name === "SET_W_R") {
        handleSetWin(false, 1);
        return;
      }
      if (name === "SET_W_L2") {
        handleSetWin(true, 2);
        return;
      }
      if (name === "SET_W_R2") {
        handleSetWin(false, 2);
        return;
      }
      if (name === "RESET_W_L") {
        handleResetWin(true);
        return;
      }
      if (name === "RESET_W_R") {
        handleResetWin(false);
        return;
      }
    });
    el.addEventListener("pointerup", up);
    el.addEventListener("pointercancel", up);
    el.addEventListener("lostpointercapture", function (ev) {
      if (held[name]) up(ev);
    });
  }

  function connectWs() {
    var proto = location.protocol === "https:" ? "wss:" : "ws:";
    var url = proto + "//" + location.host + "/ws";
    var socket;
    try {
      socket = new WebSocket(url);
    } catch (e) {
      startPoll();
      return;
    }
    ws = socket;
    socket.onopen = function () {
      offlineSince = 0;
      showOffline(false);
      stopPoll();
      sendWdt();
    };
    socket.onmessage = function (ev) {
      offlineSince = 0;
      showOffline(false);
      try {
        var d = JSON.parse(ev.data);
        if (d.t === "pong") return;
        if (d.t === "hello") {
          applyHello(d);
          return;
        }
        applyStatus(d);
      } catch (e) {}
    };
    socket.onclose = function () {
      ws = null;
      startPoll();
      if (!offlineSince) offlineSince = Date.now();
      if (Date.now() - offlineSince > 1600) showOffline(true);
      setTimeout(connectWs, 120);
    };
    socket.onerror = function () {
      try {
        socket.close();
      } catch (e) {}
    };
  }

  function startPoll() {
    if (pollTimer) return;
    fetch("/api/hello")
      .then(function (r) {
        return r.json();
      })
      .then(applyHello)
      .catch(function () {});
    pollTimer = setInterval(function () {
      if (ws && ws.readyState === 1) return;
      fetch("/api/status")
        .then(function (r) {
          return r.json();
        })
        .then(applyStatus)
        .catch(function () {});
    }, 250);
  }

  function stopPoll() {
    if (pollTimer) {
      clearInterval(pollTimer);
      pollTimer = null;
    }
  }

  document.querySelectorAll(".tab").forEach(function (tab) {
    tab.addEventListener("click", function () {
      var id = tab.getAttribute("data-tab");
      document.querySelectorAll(".tab").forEach(function (t) {
        t.classList.toggle("active", t === tab);
      });
      document.querySelectorAll(".panel").forEach(function (p) {
        var show = (p.getAttribute("data-show") || "").split(" ");
        p.classList.toggle("hidden", show.indexOf(id) < 0);
      });
      if (id === "config") loadConfig();
    });
  });

  loadSwapDirs();
  loadAxisMask();
  sendAx(axisMask);
  document.querySelectorAll("[data-btn]").forEach(bindHold);
  speedSliders().forEach(bindSpeedSlider);

  (function bindHome() {
    var en = $("enable");
    if (en) {
      en.addEventListener("change", function () {
        if (syncEnableSilent) return;
        if (en.checked && isDrvError()) {
          syncEnableUi(false);
          return;
        }
        sendMc(en.checked ? "SE 1" : "SE 0");
      });
    }

    var d1 = $("dir");
    if (d1) {
      d1.addEventListener("change", function () {
        swapDir = !!d1.checked;
        saveSwapDirs();
      });
    }
    var d2 = $("dir2");
    if (d2) {
      d2.addEventListener("change", function () {
        swapDir2 = !!d2.checked;
        saveSwapDirs();
      });
    }

    var accEl = $("accSliderHome");
    if (accEl) {
      accEl.addEventListener("pointerdown", function () {
        draggingAccel = true;
      });
      accEl.addEventListener("pointerup", function () {
        draggingAccel = false;
        emitSa(true);
      });
      accEl.addEventListener("input", function () {
        emitSa(false);
      });
    }
  })();

  document.querySelectorAll(".chip").forEach(function (el) {
    el.addEventListener("click", function () {
      axisMask = Number(el.getAttribute("data-ax"));
      if (axisMask !== 0 && axisMask !== 1 && axisMask !== 2) axisMask = 1;
      syncAxisMaskUi();
      saveAxisMask();
      sendAx(axisMask);
    });
  });

  (function bindTimeAbc() {
    var inp = $("timeAbc");
    if (!inp) return;
    function clamp() {
      var t = Number(inp.value);
      if (isNaN(t) || t < 0.1) inp.value = "0.1";
    }
    inp.addEventListener("change", clamp);
    inp.addEventListener("blur", clamp);
  })();

  (function bindLoopWait() {
    var inp = $("loopWaitAbc");
    if (!inp) return;
    function clamp() {
      var t = Number(inp.value);
      if (isNaN(t) || t < 0) inp.value = "0";
      else if (t > 100) inp.value = "100";
    }
    inp.addEventListener("change", clamp);
    inp.addEventListener("blur", clamp);
  })();

  (function bindTlExposure() {
    var inp = $("TL_EXPOSURE");
    if (!inp) return;
    function clamp() {
      var t = Number(inp.value);
      if (isNaN(t) || t < 0.1) inp.value = "0.1";
      else if (t > 60) inp.value = "60";
    }
    inp.addEventListener("change", clamp);
    inp.addEventListener("blur", clamp);
  })();

  (function bindTlFactor() {
    var inp = $("TL_FACTOR");
    if (!inp) return;
    function clamp() {
      var t = Number(inp.value);
      if (isNaN(t) || t < 3) inp.value = "3";
      else if (t > 1000) inp.value = "1000";
    }
    inp.addEventListener("change", clamp);
    inp.addEventListener("blur", clamp);
  })();

  (function bindTlFps() {
    var inp = $("TL_FPS");
    if (!inp) return;
    function clamp() {
      var t = Number(inp.value);
      if (isNaN(t) || t < 1) inp.value = "1";
      else if (t > 60) inp.value = "60";
    }
    inp.addEventListener("change", clamp);
    inp.addEventListener("blur", clamp);
  })();

  (function bindTlMsm() {
    var inp = $("TL_MSM");
    if (!inp) return;
    try {
      var saved = localStorage.getItem(TL_MSM_KEY);
      if (saved === "1") inp.checked = true;
      else if (saved === "0") inp.checked = false;
    } catch (e) {}
    inp.addEventListener("change", function () {
      try {
        localStorage.setItem(TL_MSM_KEY, inp.checked ? "1" : "0");
      } catch (e) {}
    });
  })();

  (function bindCli() {
    function loadCliCmds() {
      var cmds = ["", "", "", "", ""];
      try {
        var raw = localStorage.getItem(CLI_CMDS_KEY);
        if (raw) {
          var parsed = JSON.parse(raw);
          if (parsed && parsed.length) {
            for (var i = 0; i < 5; i++) {
              cmds[i] = parsed[i] != null ? String(parsed[i]) : "";
            }
          }
        }
      } catch (e) {}
      for (var n = 1; n <= 5; n++) {
        var el = $("cmd" + n);
        if (el) el.value = cmds[n - 1];
      }
    }

    function saveCliCmds() {
      var arr = [];
      for (var n = 1; n <= 5; n++) {
        var el = $("cmd" + n);
        arr.push(el ? el.value : "");
      }
      try {
        localStorage.setItem(CLI_CMDS_KEY, JSON.stringify(arr));
      } catch (e) {}
    }

    function sendCmd(n) {
      var inp = $("cmd" + n);
      if (!inp) return;
      sendMc(inp.value);
    }

    for (var i = 1; i <= 5; i++) {
      (function (n) {
        var inp = $("cmd" + n);
        var btn = $("send" + n);
        if (inp) {
          inp.addEventListener("change", saveCliCmds);
          inp.addEventListener("blur", saveCliCmds);
          inp.addEventListener("keydown", function (ev) {
            if (ev.key === "Enter") {
              ev.preventDefault();
              sendCmd(n);
            }
          });
        }
        if (btn) {
          btn.addEventListener("click", function () {
            sendCmd(n);
          });
        }
      })(i);
    }

    loadCliCmds();
  })();

  function loadConfig() {
    fetch("/api/wifi")
      .then(function (r) {
        return r.json();
      })
      .then(function (w) {
        $("ssid").value = w.ssid || "";
        $("hostname").value = w.hostname || "slider";
        $("wifiHint").textContent =
          (w.mode || "") + "  " + (w.ip || "") + (w.ap_ssid ? "  AP " + w.ap_ssid : "");
      })
      .catch(function () {});
    fetch("/api/files")
      .then(function (r) {
        return r.json();
      })
      .then(function (d) {
        var sel = $("fileList");
        sel.innerHTML = "";
        (d.files || []).forEach(function (p) {
          var o = document.createElement("option");
          o.value = p;
          o.textContent = p;
          sel.appendChild(o);
        });
        if (sel.value) loadFile(sel.value);
      })
      .catch(function () {});
  }

  function loadFile(path) {
    fetch("/api/files?path=" + encodeURIComponent(path))
      .then(function (r) {
        return r.json();
      })
      .then(function (d) {
        $("fileText").value = d.text || "";
      })
      .catch(function () {});
  }

  $("fileList").addEventListener("change", function () {
    loadFile($("fileList").value);
  });
  $("fileSave").addEventListener("click", function () {
    fetch("/api/files", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path: $("fileList").value, text: $("fileText").value }),
    }).then(function () {});
  });
  $("wifiSave").addEventListener("click", function () {
    fetch("/api/wifi", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        ssid: $("ssid").value,
        password: $("password").value,
        hostname: $("hostname").value,
      }),
    }).then(function () {
      loadConfig();
    });
  });
  $("wifiForget").addEventListener("click", function () {
    fetch("/api/wifi", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ forget: true }),
    }).then(function () {
      $("ssid").value = "";
      $("password").value = "";
      loadConfig();
    });
  });

  wdtTimer = setInterval(function () {
    sendWdt();
  }, WDT_MS);

  (function bindJoy() {
    var pad = $("joyPad");
    var handle = $("joyHandle");
    var logEl = $("joyLog");
    var lockX = $("joyLockX");
    var lockY = $("joyLockY");
    if (!pad || !handle || !logEl) return;

    var K = 9;
    var RIM = 45;
    var REACH = 0.9;
    var SNAP_MS = 120;
    var nx = 0;
    var ny = 0;
    var dragging = false;
    var snapTimer = 0;
    var pid = 0;
    var mjTimer = 0;
    var lastMj = "";

    function joyPctX() {
      return mapped(joyNx());
    }

    function joyPctY() {
      return mapped(-joyNy());
    }

    function fmtMjPct(v) {
      return Math.round(Number(v) * 10) / 10;
    }

    function mjStopLine() {
      return dual() ? "MJ 0 0" : "MJ 0";
    }

    function emitMj(force) {
      var x = joyPctX();
      var line = dual()
        ? "MJ " + fmtMjPct(x) + " " + fmtMjPct(joyPctY())
        : "MJ " + fmtMjPct(x);
      if (!force && line === lastMj) return;
      lastMj = line;
      sendMc(line);
    }

    function scheduleMj() {
      if (mjTimer) return;
      mjTimer = setTimeout(function () {
        mjTimer = 0;
        if (dragging) emitMj(false);
      }, 50);
    }

    function stopMj() {
      if (mjTimer) {
        clearTimeout(mjTimer);
        mjTimer = 0;
      }
      lastMj = "";
      sendMc(mjStopLine());
    }

    function dual() {
      return document.body.classList.contains("axes-2");
    }

    function curve(u) {
      u = Math.max(0, Math.min(1, u));
      if (pad.getAttribute("data-curve") === "log") {
        return (Math.pow(1 + K, u) - 1) / K;
      }
      return u;
    }

    function invCurve(p) {
      p = Math.max(0, Math.min(1, p));
      if (pad.getAttribute("data-curve") === "log") {
        return Math.log(1 + K * p) / Math.log(1 + K);
      }
      return p;
    }

    function fmtPct(v) {
      var n = Math.round(v);
      if (n === 0) return "0";
      return (n > 0 ? "+" : "") + String(n);
    }

    function mapped(axis) {
      if (!axis) return 0;
      return (axis < 0 ? -1 : 1) * 100 * curve(Math.abs(axis));
    }

    function setRings() {
      var ids = { joyRing25: 0.25, joyRing50: 0.5, joyRing75: 0.75 };
      Object.keys(ids).forEach(function (id) {
        var el = $(id);
        if (el) el.setAttribute("r", String(RIM * invCurve(ids[id])));
      });
    }

    function reach() {
      var rect = pad.getBoundingClientRect();
      return (rect.width / 2) * REACH;
    }

    function joyNx() {
      return swapDir ? -nx : nx;
    }

    function joyNy() {
      return swapDir2 ? -ny : ny;
    }

    function applyPos() {
      var R = reach();
      handle.style.setProperty("--jx", nx * R + "px");
      handle.style.setProperty("--jy", ny * R + "px");
      $("joyX").textContent = fmtPct(mapped(joyNx()));
      $("joyY").textContent = fmtPct(mapped(-joyNy()));
    }

    function setFromPointer(ev) {
      var rect = pad.getBoundingClientRect();
      var cx = rect.left + rect.width / 2;
      var cy = rect.top + rect.height / 2;
      var R = reach();
      if (R < 1) return;
      var dx = ev.clientX - cx;
      var dy = ev.clientY - cy;
      if (!dual()) {
        dy = 0;
      } else if (lockX && lockX.checked) {
        dy = 0;
      } else if (lockY && lockY.checked) {
        dx = 0;
      }
      var len = Math.sqrt(dx * dx + dy * dy);
      if (len > R && len > 0) {
        dx = (dx / len) * R;
        dy = (dy / len) * R;
      }
      nx = dx / R;
      ny = dy / R;
      applyPos();
    }

    function snap() {
      var x0 = nx;
      var y0 = ny;
      var t0 = Date.now();
      if (snapTimer) cancelAnimationFrame(snapTimer);
      function step() {
        var t = Math.min(1, (Date.now() - t0) / SNAP_MS);
        var e = 1 - (1 - t) * (1 - t);
        nx = x0 * (1 - e);
        ny = y0 * (1 - e);
        applyPos();
        if (t < 1) snapTimer = requestAnimationFrame(step);
        else {
          nx = ny = 0;
          applyPos();
          snapTimer = 0;
        }
      }
      snapTimer = requestAnimationFrame(step);
    }

    pad.addEventListener("pointerdown", function (ev) {
      if (ev.target.closest(".joy-log") || ev.target.closest(".joy-lock")) return;
      ev.preventDefault();
      dragging = true;
      pid = ev.pointerId;
      if (snapTimer) {
        cancelAnimationFrame(snapTimer);
        snapTimer = 0;
      }
      handle.classList.add("held");
      try {
        pad.setPointerCapture(ev.pointerId);
      } catch (e) {}
      setFromPointer(ev);
      sendMc("SS " + fmtSs(cmdSpd));
      emitMj(true);
    });
    pad.addEventListener("pointermove", function (ev) {
      if (!dragging || ev.pointerId !== pid) return;
      setFromPointer(ev);
      scheduleMj();
    });
    function endDrag(ev) {
      if (!dragging || (ev && ev.pointerId !== pid)) return;
      dragging = false;
      handle.classList.remove("held");
      try {
        pad.releasePointerCapture(pid);
      } catch (e) {}
      stopMj();
      snap();
    }
    pad.addEventListener("pointerup", endDrag);
    pad.addEventListener("pointercancel", endDrag);

    logEl.addEventListener("pointerdown", function (ev) {
      ev.stopPropagation();
    });
    logEl.addEventListener("change", function () {
      pad.setAttribute("data-curve", logEl.checked ? "log" : "lin");
      setRings();
      applyPos();
      if (dragging) emitMj(true);
    });

    if (lockX && lockY) {
      function onLock(which) {
        return function () {
          if (which === "x" && lockX.checked) lockY.checked = false;
          if (which === "y" && lockY.checked) lockX.checked = false;
        };
      }
      lockX.addEventListener("pointerdown", function (ev) {
        ev.stopPropagation();
      });
      lockY.addEventListener("pointerdown", function (ev) {
        ev.stopPropagation();
      });
      lockX.addEventListener("change", onLock("x"));
      lockY.addEventListener("change", onLock("y"));
    }

    window.addEventListener("resize", applyPos);
    setRings();
    applyPos();
  })();

  loadMarks();
  connectWs();
  startPoll();
  sendWdt();
})();
