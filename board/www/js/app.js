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

  var ws = null;
  var pollTimer = null;
  var wdtTimer = null;
  var offlineSince = 0;
  var lastStatus = {};
  var draggingSpeed = false;
  var ssTimer = 0;
  var axisMask = 1;
  var spdMin = 1;
  var spdMax = 100;
  var held = {};
  var cmdSpd = 40;
  var optionHeld = false;
  var cruise = { locked: false, dir: 0, axis: 1 };
  var marks = { a: null, b: null, c: null };

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

  function showOffline(on) {
    $("offline").classList.toggle("hidden", !on);
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
    return [$("spdSlider"), $("spdSliderHome"), $("spdSliderAbc")].filter(Boolean);
  }

  function speedLabels() {
    return [$("ssVal"), $("ssValHome"), $("ssValAbc")].filter(Boolean);
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

  function sendWdt() {
    return send({ wdt: "alive" });
  }

  function fmtSs(v) {
    return Math.round(Number(v) * 100) / 100;
  }

  function effectiveSpeed() {
    return optionHeld ? spdMax : cmdSpd;
  }

  function jogCmd(dir, axis) {
    var cmd = dir < 0 ? "ML" : "MR";
    if (axis === 2) return cmd + " 2";
    return cmd;
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

  function applyStatus(d) {
    if (!d || typeof d !== "object") return;
    lastStatus = d;
    setNum($("pos"), d.pos);
    setNum($("spd"), d.spd);
    setNum($("acc"), d.acc);
    $("state").textContent = d.state || "?";
    $("line1").innerHTML = d.line1 ? d.line1 : "&nbsp;";
    $("line2").innerHTML = d.line2 ? d.line2 : "&nbsp;";
    $("oled").classList.toggle("warn", !!d.warn);
    var unit = d.unit || "mm";
    $("posUnit").textContent = unit;
    $("spdUnit").textContent = unit + "/s";
    $("accUnit").textContent = unit + "/s²";
    var axes = d.axes || 1;
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
      $("pos2Unit").textContent = unit;
      $("spd2Unit").textContent = unit + "/s";
      $("acc2Unit").textContent = unit + "/s²";
    }
    if (d.spd_min != null) spdMin = Number(d.spd_min);
    if (d.max_speed != null) spdMax = Number(d.max_speed);
    if (
      !draggingSpeed &&
      d.ss != null &&
      !optionHeld &&
      !held.FAST_L &&
      !held.FAST_R
    ) {
      syncSpeedUi(d.ss, null);
    }
    if (d.ax != null) {
      axisMask = Number(d.ax);
      document.querySelectorAll(".chip").forEach(function (el) {
        el.classList.toggle("active", Number(el.getAttribute("data-ax")) === axisMask);
      });
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
    var v = lastStatus.slider_min;
    return v === null || v === undefined || v === "" ? null : Number(v);
  }

  function softMax() {
    var v = lastStatus.slider_max;
    return v === null || v === undefined || v === "" ? null : Number(v);
  }

  function curPos() {
    var v = lastStatus.pos;
    return v === null || v === undefined ? null : Number(v);
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
        if (!markSaved && dt < MARK_MS) {
          gotoMark(name.toLowerCase());
        }
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
          if (held[name]) sendMc("H");
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

      if (name === "A" || name === "B" || name === "C") {
        var letter = name.toLowerCase();
        markTimer = setTimeout(function () {
          if (!held[name]) return;
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

  document.querySelectorAll("[data-btn]").forEach(bindHold);
  speedSliders().forEach(bindSpeedSlider);

  document.querySelectorAll(".chip").forEach(function (el) {
    el.addEventListener("click", function () {
      axisMask = Number(el.getAttribute("data-ax"));
      document.querySelectorAll(".chip").forEach(function (c) {
        c.classList.toggle("active", c === el);
      });
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

    function applyPos() {
      var R = reach();
      handle.style.setProperty("--jx", nx * R + "px");
      handle.style.setProperty("--jy", ny * R + "px");
      $("joyX").textContent = fmtPct(mapped(nx));
      $("joyY").textContent = fmtPct(mapped(-ny));
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
    });
    pad.addEventListener("pointermove", function (ev) {
      if (!dragging || ev.pointerId !== pid) return;
      setFromPointer(ev);
    });
    function endDrag(ev) {
      if (!dragging || (ev && ev.pointerId !== pid)) return;
      dragging = false;
      handle.classList.remove("held");
      try {
        pad.releasePointerCapture(pid);
      } catch (e) {}
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
