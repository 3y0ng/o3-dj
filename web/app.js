// O3 DJ controller UI. Polls /api/state; every control posts and re-renders.
"use strict";

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const GENRE_COLORS = { chill: "#3d8bff", jazzy_cafe: "#ff6b2c", asian: "#2fd37f" };
const EXTRA_COLORS = ["#f1f1ee", "#ffd23d", "#c77dff", "#ff4f8b"];
const WEATHER_GLYPH = { clear: "☀ clear", cloudy: "☁ cloudy", rain: "☂ rain" };

let S = null;               // last snapshot
let pending = {};           // input changes not yet sent
let sendTimer = null;
let lastInteraction = 0;    // ignore polled inputs briefly after local edits

function genreColor(key, i = 0) {
  return GENRE_COLORS[key] || EXTRA_COLORS[i % EXTRA_COLORS.length];
}

async function api(path, body) {
  const opts = body === undefined ? {} : {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  };
  const r = await fetch(path, opts);
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || r.statusText);
  return data;
}

async function act(path, body) {
  try {
    const data = await api(path, body);
    // A button's result is authoritative (e.g. "sounds right" resets the knobs for the next scenario);
    // only knob edits get the brief "don't fight the user's hand" protection.
    if (path !== "/api/inputs") { lastInteraction = 0; pending = {}; clearTimeout(sendTimer); }
    render(data);
  } catch (e) {
    flashFoot(e.message);
  }
}

function flashFoot(msg) {
  const f = $("#foot");
  f.textContent = "⚠ " + msg;
  f.classList.add("bad");
  setTimeout(() => f.classList.remove("bad"), 4000);
}

// ── inputs (optimistic) ─────────────────────────────────────────
function setInputs(patch, delay = 140) {
  Object.assign(pending, patch);
  if (S) Object.assign(S.inputs, patch);
  lastInteraction = Date.now();
  clearTimeout(sendTimer);
  sendTimer = setTimeout(() => {
    const body = pending;
    pending = {};
    act("/api/inputs", body);
  }, delay);
  if (S) render(S, true);
}

// ── encoders (endless, like the OP-1) ─────────────────────────────
const ENC = {
  volume: {
    step(d) {
      const i = S.inputs;
      if (i.auto) setInputs({ volume_trim: clamp(i.volume_trim + d, -50, 50) });
      else setInputs({ manual_volume: clamp(i.manual_volume + d, 0, S.limits.max_volume) });
      const shown = i.auto ? Math.round(S.targets.volume_base + i.volume_trim) : i.manual_volume;
      popup("volume", clamp(shown, S.limits.min_volume, S.limits.max_volume),
        i.auto ? `auto ${fmtSigned(i.volume_trim)} trim` : "manual", "var(--blue)");
    },
  },
  energy: {
    step(d) {
      const v = Math.round(clamp(S.inputs.energy_trim + d * 0.02, -0.5, 0.5) * 100) / 100;
      setInputs({ energy_trim: v });
      popup("energy", fmtSigned(v, 2), v > 0 ? "more upbeat" : v < 0 ? "more mellow" : "as the dj sees it", "var(--green)");
    },
  },
  occ: {
    step(d) {
      const v = clamp(S.inputs.occupancy + d * 2, 0, 100);
      setInputs({ occupancy: v, occupancy_enabled: true });
      const how = "space is " + (v > 85 ? "packed" : v > 55 ? "busy" : v > 25 ? "steady" : "quiet");
      popup("occupancy", v + "%", S.mode === "live" ? `override for ${(S.feeds && S.feeds.override_minutes) || 60} min · ${how}` : how, "var(--white)");
    },
  },
  time: {
    step(d) {
      if (S.mode === "live") { popup("time", fmtHour(S.targets.hour), "live mode follows the venue clock", "var(--orange)"); return; }
      const base = S.inputs.hour_override ?? S.targets.hour;
      const v = ((Math.round(base * 4) + d) / 4 + 24) % 24;
      setInputs({ hour_override: v });
      popup("time", fmtHour(v), "simulated · clock key to reset", "var(--orange)");
    },
  },
};

function initEncoders() {
  $$(".enc").forEach((el) => {
    const name = el.dataset.enc;
    const cap = $(".cap", el);
    let rot = 0, acc = 0, lastY = 0, dragging = false;
    const turn = (d) => {
      if (!S || !d) return;
      if (S.calibration && S.calibration.wizard && (name === "occ" || name === "time")) {
        return popup(name === "occ" ? "occupancy" : "time", "fixed", "set by the calibration scenario", "var(--dim)");
      }
      rot += d * 14;
      cap.style.setProperty("--rot", rot + "deg");
      ENC[name].step(d);
    };
    let moved = 0, lastTap = 0;
    el.addEventListener("pointerdown", (e) => { dragging = true; moved = 0; lastY = e.clientY; acc = 0; el.setPointerCapture(e.pointerId); el.focus({ preventScroll: true }); });
    el.addEventListener("pointermove", (e) => {
      if (!dragging) return;
      acc += lastY - e.clientY; moved += Math.abs(lastY - e.clientY); lastY = e.clientY;
      const steps = Math.trunc(acc / 7);
      if (steps) { acc -= steps * 7; turn(steps); }
    });
    const end = () => { dragging = false; };
    el.addEventListener("pointerup", (e) => {
      end();
      // touch has no dblclick: a double tap (without turning) resets the knob
      if (e.pointerType === "touch" && moved < 6) {
        if (e.timeStamp - lastTap < 350) { resetEncoder(name); lastTap = 0; } else lastTap = e.timeStamp;
      }
    });
    el.addEventListener("pointercancel", end);
    let wheelAcc = 0;
    el.addEventListener("wheel", (e) => {
      e.preventDefault();
      wheelAcc -= e.deltaY;
      const steps = Math.trunc(wheelAcc / 30);
      if (steps) { wheelAcc -= steps * 30; turn(Math.max(-3, Math.min(3, steps))); }
    }, { passive: false });
    el.addEventListener("keydown", (e) => {
      const d = { ArrowUp: 1, ArrowRight: 1, ArrowDown: -1, ArrowLeft: -1 }[e.key];
      if (d) { e.preventDefault(); turn(d * (e.shiftKey ? 5 : 1)); }
    });
    el.addEventListener("dblclick", () => resetEncoder(name));
  });
}

function resetEncoder(name) {
  if (!S) return;
  if (name === "time") { setInputs({ hour_override: null }, 0); popup("time", "clock", "following the real time", "var(--orange)"); }
  if (name === "energy") { setInputs({ energy_trim: 0 }, 0); popup("energy", "±0.00", "reset", "var(--green)"); }
  if (name === "volume") { setInputs({ volume_trim: 0 }, 0); popup("volume", "auto", "trim reset", "var(--blue)"); }
}

let popTimer = null;
function popup(label, val, sub, color) {
  const p = $("#popup");
  $(".pop-label", p).textContent = label;
  $(".pop-val", p).textContent = val;
  $(".pop-val", p).style.color = color;
  $(".pop-sub", p).textContent = sub || "";
  p.classList.add("show");
  clearTimeout(popTimer);
  popTimer = setTimeout(() => p.classList.remove("show"), 1100);
}

// ── keys ───────────────────────────────────────────────────────────
function initKeys() {
  $$("[data-weather]").forEach((k) => k.addEventListener("click", () => {
    const w = k.dataset.weather;
    if (S.mode === "live" && (S.feeds || {}).weather_src === "override" && S.inputs.weather === w) {
      return act("/api/override/clear", { key: "weather" });  // press the lit key again: back to live weather
    }
    setInputs({ weather: w }, 0);
    if (S.mode === "live") popup("weather", w, `override for ${(S.feeds && S.feeds.override_minutes) || 60} min · press again for live`, "var(--blue)");
  }));
  $("#k-occ").addEventListener("click", () => {
    if (S.mode === "live") {
      if ((S.feeds || {}).occupancy_src === "override") return act("/api/override/clear", { key: "occupancy" });
      return popup("occupancy", S.inputs.occupancy_enabled ? S.inputs.occupancy + "%" : "—", "comes from live data · turn the knob to override", "var(--white)");
    }
    setInputs({ occupancy_enabled: !S.inputs.occupancy_enabled }, 0);
  });
  const setMode = (next) => {
    if (!S || S.mode === undefined || S.mode === next) return;
    S.mode = next;
    renderMode(S);
    act("/api/mode", { mode: next });
    popup("mode", next, next === "live" ? "weather + occupancy from live data" : "set the atmosphere by hand", next === "live" ? "var(--green)" : "var(--dim)");
  };
  $("#k-mode").addEventListener("click", () => setMode(S.mode === "live" ? "demo" : "live"));
  $$(".slide-lab").forEach((l) => l.addEventListener("click", () => setMode(l.dataset.for)));
  initCalibrateKey();
  $("#k-auto").addEventListener("click", () => {
    const auto = !S.inputs.auto;
    // switching to manual keeps the current loudness as the starting point
    setInputs(auto ? { auto } : { auto, manual_volume: S.targets.volume }, 0);
  });
  $("#k-clock").addEventListener("click", () => {
    if (S.mode === "live") return popup("time", fmtHour(S.targets.hour), "live mode follows the venue clock", "var(--orange)");
    setInputs({ hour_override: S.inputs.hour_override == null ? S.targets.hour : null }, 0);
  });
  $("#k-play").addEventListener("click", () => {
    const playing = S.running && !S.paused && S.status.state !== "STOPPED";
    if (!playing && !S.running && S.status.other_source) {
      const rooms = S.speakers.filter((sp) => sp.in_group).map((sp) => sp.name);
      const what = /spotify/i.test(S.status.uri) ? "Spotify" : "something else";
      if (!confirm(`The speakers are playing ${what} in ${rooms.length} room${rooms.length === 1 ? "" : "s"}:\n\n${rooms.join("\n")}\n\nReplace it with the DJ?`)) return;
    }
    act("/api/control", { action: playing ? "pause" : "play" });
  });
  $("#k-skip").addEventListener("click", () => {
    const wiz = S.calibration && S.calibration.wizard;
    if (wiz && !wiz.proposal) return act("/api/calibration", { action: "another" });
    act("/api/control", { action: "skip" });
  });
  $("#k-up").addEventListener("click", () => act("/api/control", { action: "up" }));
  $("#k-down").addEventListener("click", () => act("/api/control", { action: "down" }));
  $("#k-party").addEventListener("click", () => {
    const others = S.speakers.filter((sp) => !sp.in_group).map((sp) => sp.name);
    if (!others.length) return;
    if (!confirm(`Party mode pulls every room into the DJ's group:\n\n${others.join("\n")}\n\nAnything those rooms are playing now (e.g. Spotify) will stop. Continue?`)) return;
    act("/api/speakers", { action: "party" });
  });
  $("#k-scan").addEventListener("click", () => act("/api/speakers", { action: "discover" }));

  $("#add-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = e.target, msg = $("#add-msg");
    try {
      render(await api("/api/tracks", { url: f.url.value, title: f.title.value, genre: f.genre.value }));
      msg.textContent = "added ✓";
      f.url.value = f.title.value = "";
    } catch (err) {
      msg.textContent = err.message;
    }
  });
}

function toggleGenre(key) {
  const set = new Set(S.inputs.genres);
  if (set.has(key)) { if (set.size > 1) set.delete(key); } else set.add(key);
  setInputs({ genres: [...set] }, 0);
}

// ── render ──────────────────────────────────────────────────────────
function render(s, local = false) {
  if (!local && Date.now() - lastInteraction < 1200 && S) s.inputs = S.inputs; // don't fight the user's knob
  S = s;
  const i = s.inputs, t = s.targets, st = s.status || {};
  const playing = s.running && !s.paused && st.state === "PLAYING";

  $("#screen").classList.toggle("playing", playing);
  spin.playing = playing;
  drawTape(s.running && st.duration ? Math.min(1, (st.elapsed || 0) / st.duration) : s.running ? 0.5 : 0);
  $("#led-power").className = "led" + (playing ? " on" : s.running ? " on green" : "");
  $("#mode-tag").textContent = s.live ? "live" : "mock";

  // status bar
  $("#s-clock").textContent = fmtHour(t.hour) + (t.clock ? "" : "*");
  $("#s-daypart").textContent = t.daypart;
  const live = s.mode === "live", L = s.feeds || {};
  const modeEl = $("#s-mode");
  modeEl.textContent = live ? "live" : "demo";
  modeEl.className = "pill " + (live ? "c-green" : "c-dim");
  const ovr = (k) => (L.overrides && L.overrides[k] != null ? ` · ovr ${L.overrides[k]}m` : "");
  const wEl = $("#s-weather"), oEl = $("#s-occ");
  if (!live) {
    wEl.textContent = WEATHER_GLYPH[i.weather] || i.weather;
    wEl.className = "c-blue";
    oEl.textContent = i.occupancy_enabled ? `occ ${i.occupancy}%` : "";
    oEl.className = "c-dim";
  } else {
    const temp = L.weather && L.weather.detail && L.weather.detail.temp_c != null ? ` ${Math.round(L.weather.detail.temp_c)}°` : "";
    wEl.textContent = L.weather_src === "off" ? "set venue" : L.weather_src === "stale" ? "weather stale" :
      (WEATHER_GLYPH[i.weather] || i.weather) + (L.weather_src === "live" ? temp : "") + ovr("weather");
    wEl.className = L.weather_src === "live" || L.weather_src === "override" ? "c-blue" : "c-red";
    const d = (L.occupancy && L.occupancy.detail) || {};
    const miss = (L.occupancy && L.occupancy.missing) || [];
    oEl.title = L.occupancy_src === "off" ? "needs: " + miss.join(", ") : (L.occupancy && L.occupancy.error) || "";
    oEl.textContent = L.occupancy_src === "off" ? "occ setup needed" : L.occupancy_src === "stale" ? "occ stale" :
      `occ ${i.occupancy}%` + (L.occupancy_src === "live" && d.count != null ? ` (${d.count}/${d.capacity})` : "") + ovr("occupancy");
    oEl.className = L.occupancy_src === "live" || L.occupancy_src === "override" ? "c-white" : "c-red";
  }
  const src = $("#s-src");
  src.textContent = s.health.source_online ? "src ok" : "offline·cache";
  src.className = "pill " + (s.health.source_online ? "c-green" : "c-red");
  $("#s-cache").textContent = `${s.cache.files} cached`;
  $("#s-cache").title = `${s.cache.files} tracks cached (${s.cache.mb} MB) · tempo analysed ${s.cache.analysed}/${s.cache.tracks}`;

  // now playing
  const n = s.now;
  const stateEl = $("#s-state");
  stateEl.textContent = s.health.speaker_error ? "speaker error" :
    !s.running ? "standby" : s.paused ? "paused" : playing ? "on air · " + fmtTime(st.elapsed) + (st.duration ? " / " + fmtTime(st.duration) : "") : (st.state || "").toLowerCase();
  stateEl.className = "np-state " + (playing ? "live" : s.health.speaker_error ? "c-red" : "c-dim");
  $("#s-title").textContent = n ? n.title : s.running ? "…" : st.other_source ? (/spotify/i.test(st.uri) ? "spotify is playing" : "another source is playing") : "press ▶ to start the dj";
  $("#s-meta").textContent = n
    ? [n.genre_label, n.bpm ? Math.round(n.bpm) + " bpm" : "bpm ?", n.cached ? "cached" : "stream", n.up || n.down ? `▲${n.up} ▼${n.down}` : ""].filter(Boolean).join(" · ")
    : s.health.speaker_error || " ";

  // meters
  $("#m-energy-target").style.left = t.energy * 100 + "%";
  $("#m-energy-track").style.width = (n && n.energy != null ? n.energy * 100 : 0) + "%";
  $("#m-energy-val").textContent = t.energy.toFixed(2);
  const segs = $("#m-vol");
  if (segs.children.length !== 20) segs.innerHTML = "<i></i>".repeat(20);
  const actual = avg(Object.values(s.volumes || {}));
  const max = s.limits.max_volume;
  [...segs.children].forEach((el, k) => {
    const lvl = ((k + 1) / 20) * max;
    el.className = actual != null && lvl <= actual + 0.01 ? "on" : lvl <= t.volume + 0.01 ? "tgt" : "";
  });
  $("#m-vol-val").textContent = actual != null && s.running ? `${Math.round(actual)}→${t.volume}` : t.volume;
  const keys = Object.keys(s.genres);
  $("#m-mix").innerHTML = Object.entries(t.weights)
    .map(([g, w]) => `<i style="flex-grow:${w};background:${genreColor(g, keys.indexOf(g))}"></i>`).join("");
  const top = Object.entries(t.weights).sort((a, b) => b[1] - a[1])[0];
  $("#m-mix-val").textContent = top ? Math.round(top[1] * 100) + "%" : "";

  setTicker(t.reasons.join("   ·   "));

  // encoders dimmed when they have no effect
  $('[data-enc="occ"]').classList.toggle("dimmed", !i.occupancy_enabled);
  $('[data-enc="time"]').classList.toggle("dimmed", i.hour_override == null || s.mode === "live");

  // keys
  renderGenreKeys(s);
  $$("[data-weather]").forEach((k) => $(".led", k).classList.toggle("on", k.dataset.weather === i.weather));
  $(".led", $("#k-occ")).classList.toggle("on", i.occupancy_enabled);
  $(".led", $("#k-auto")).classList.toggle("on", i.auto);
  $(".led", $("#k-clock")).classList.toggle("on", i.hour_override == null);
  renderMode(s);
  renderCalibration(s);
  $("em", $("#k-occ")).textContent = s.mode === "live" && (s.feeds || {}).occupancy_src === "override" ? "occ · live" : "occ";
  $(".led", $("#k-play")).className = "led" + (playing ? " on green" : "");
  $("#play-icon").innerHTML = playing
    ? '<path d="M7 5h4v14H7zM13 5h4v14h-4z" class="solid"/>'
    : '<path d="M8 5l11 7-11 7z" class="solid"/>';
  $("em", $("#k-play")).textContent = playing ? "pause" : s.running && s.paused ? "resume" : "play";

  renderRooms(s);
  renderStrip(s);

  const foot = $("#foot");
  if (!foot.classList.contains("bad")) {
    foot.textContent = `${s.live ? "live sonos" : "mock speakers (silent)"} · speakers stream cache from ${s.health.base_url} · ${s.cache.mb} MB cached · ${s.health.prefetch}`;
  }
}

function renderMode(s) {
  const sw = $("#k-mode"), live = s.mode === "live", supported = s.mode !== undefined;
  sw.classList.toggle("on", live);
  sw.setAttribute("aria-checked", String(live));
  sw.disabled = !supported;
  if (!supported) sw.title = "restart the DJ to enable live data";
  $$(".slide-lab").forEach((l) => l.classList.toggle("active", (l.dataset.for === "live") === live));
  $(".slide-lab .led").className = "led" + (live ? " on green" : "");
}

// One key does the whole calibration: press = start / sounds right / apply; hold = cancel / discard / reset.
function initCalibrateKey() {
  const key = $("#k-cal");
  let timer = null, held = false;
  const press = () => {
    const c = S && S.calibration;
    if (!c) return;
    const wiz = c.wizard;
    if (!wiz) {
      const again = c.saved_at ? `\n\nThis replaces the calibration from ${c.saved_at.slice(0, 10)}.` : "";
      if (confirm("Calibrate this venue? The speakers will play a short song for each of 6 scenarios " +
                  "(quiet morning, lunch rush, rain…). About 5 minutes." + again)) act("/api/calibration", { action: "start" });
    } else {
      act("/api/calibration", { action: wiz.proposal ? "apply" : "next" });
    }
  };
  const hold = () => {
    const c = S && S.calibration;
    if (!c) return;
    const wiz = c.wizard;
    if (wiz) {
      if (confirm(wiz.proposal ? "Discard this calibration and keep the old levels?" : "Stop calibrating? Nothing will change."))
        act("/api/calibration", { action: wiz.proposal ? "discard" : "stop" });
    } else if (c.saved_at && confirm("Reset this venue to the default levels (forget the calibration)?")) {
      act("/api/calibration", { action: "reset" });
    }
  };
  key.addEventListener("pointerdown", () => { held = false; timer = setTimeout(() => { held = true; hold(); }, 800); });
  const up = () => clearTimeout(timer);
  key.addEventListener("pointerup", up);
  key.addEventListener("pointerleave", up);
  key.addEventListener("click", (e) => { if (!held) press(); held = false; });
  key.addEventListener("keydown", (e) => { if (e.key === "Escape") hold(); });
}

function renderCalibration(s) {
  const c = s.calibration, wiz = c && c.wizard, key = $("#k-cal"), view = $("#cal-view");
  key.hidden = !c;  // older server
  view.hidden = !wiz;
  if (!c) return;
  key.classList.toggle("busy", !!wiz);
  $(".led", key).className = "led" + (wiz ? " on" : c.saved_at ? " on green" : "");
  $("em", key).textContent = !wiz ? "calibrate" : wiz.proposal ? "apply" : "sounds right";
  key.title = !wiz ? (c.saved_at ? `calibrated ${c.saved_at.slice(0, 10)}: press to redo, hold to reset\n` + (c.summary || []).join("\n")
                                 : "calibrate this venue")
                   : wiz.proposal ? "press to apply · hold to discard" : "press when it sounds right · hold to cancel";
  if (!wiz) return;

  $("#cv-venue").textContent = c.venue || "this venue";
  const dots = $("#cv-dots");
  if (dots.children.length !== wiz.of) dots.innerHTML = "<i></i>".repeat(wiz.of);
  [...dots.children].forEach((d, k) => (d.className = wiz.proposal || k < wiz.step - 1 ? "done" : k === wiz.step - 1 ? "now" : ""));
  $("#cv-scenario").hidden = wiz.proposal;
  $("#cv-result").hidden = !wiz.proposal;
  const legend = wiz.proposal
    ? '<span><span class="chip">calibrate</span>apply</span><span><span class="chip">hold</span>discard</span>'
    : '<span><span class="chip">blue</span>louder / softer</span><span><span class="chip">green</span>faster / slower</span>' +
      '<span><span class="chip">skip</span>another song</span><span><span class="chip">calibrate</span>sounds right</span>' +
      '<span><span class="chip">hold</span>cancel</span>';
  if ($("#cv-legend").innerHTML !== legend) $("#cv-legend").innerHTML = legend;
  if (wiz.proposal) {
    const html = wiz.summary.map((l) => `<li>${esc(l)}</li>`).join("");
    if ($("#cv-summary").innerHTML !== html) $("#cv-summary").innerHTML = html;
    return;
  }
  const sc = wiz.scenario;
  $("#cv-name").textContent = `${wiz.step}/${wiz.of}  ${sc.name}`;
  $("#cv-time").textContent = fmtHour(sc.hour);
  $("#cv-weather").textContent = WEATHER_GLYPH[sc.weather] || sc.weather;
  $("#cv-occ-bar").style.width = sc.occupancy + "%";
  $("#cv-occ").textContent = `${sc.occupancy}% full`;
  $("#cv-song").textContent = s.now ? `♪ ${s.now.title} · ${s.now.bpm ? Math.round(s.now.bpm) + " bpm" : ""}` : "♪ …";
  $("#cv-vol").textContent = s.targets.volume;
  $("#cv-nrg").textContent = s.targets.energy.toFixed(2);
}

function setTicker(text) {
  const tick = $("#s-ticker span");
  if (tick.textContent !== text) tick.textContent = text;
}

function renderGenreKeys(s) {
  const wrap = $("#genre-keys");
  const keys = Object.keys(s.genres);
  if (wrap.dataset.keys !== keys.join()) {
    wrap.dataset.keys = keys.join();
    wrap.innerHTML = keys.map((g, k) =>
      `<button class="key" data-genre="${g}"><span class="led"></span><span class="swatch" style="--sw:${genreColor(g, k)}"></span><em></em><span class="count"></span></button>`).join("");
    $$("[data-genre]", wrap).forEach((b) => b.addEventListener("click", () => toggleGenre(b.dataset.genre)));
    const sel = $("#add-genre");
    sel.innerHTML = keys.map((g) => `<option value="${g}">${esc(s.genres[g].label.toLowerCase())}</option>`).join("");
  }
  $$("[data-genre]", wrap).forEach((b) => {
    const g = s.genres[b.dataset.genre];
    $("em", b).textContent = g.label.toLowerCase();
    $(".count", b).textContent = `${g.cached}/${g.tracks}`;
    $(".led", b).classList.toggle("on", s.inputs.genres.includes(b.dataset.genre));
  });
}

const pendingOffset = {};  // ip -> offset set on the knob but not yet confirmed by the server
const offsetTimers = {};
const DRAG_PX = 18;        // pointer travel per detent

function fmtOffset(o) {
  return o > 0 ? `+${o}` : o < 0 ? `−${-o}` : "±0";
}

function roomSteps() {
  return (S && S.limits && S.limits.room_offset_steps) || [-10, -5, 0, 5, 10];
}

// Detents run from 9 o'clock (-90°) to 3 o'clock (+90°); in-between values (from config.json) sit between them.
function offsetAngle(o, steps) {
  const n = steps.length - 1, at = (i) => -90 + (180 * i) / n;
  if (o <= steps[0]) return at(0);
  for (let i = 1; i <= n; i++) {
    if (o <= steps[i]) return at(i - 1) + ((o - steps[i - 1]) / (steps[i] - steps[i - 1])) * (at(i) - at(i - 1));
  }
  return at(n);
}

function nearestStep(o, steps) {
  return steps.reduce((best, v, i) => (Math.abs(v - o) < Math.abs(steps[best] - o) ? i : best), 0);
}

function knobScale(steps) {
  const n = steps.length - 1;
  const ticks = steps.map((_, i) => {
    const a = (-180 + (180 * i) / n) * Math.PI / 180;  // svg 0° is 3 o'clock; first detent at 9 o'clock
    const p = (r) => `${(28 + r * Math.cos(a)).toFixed(2)} ${(28 + r * Math.sin(a)).toFixed(2)}`;
    return `<path class="tick" data-i="${i}" d="M${p(21.5)}L${p(26)}"/>`;
  }).join("");
  return `<svg class="rknob-scale" viewBox="0 0 56 56" aria-hidden="true">${ticks}` +
    `<text x="3" y="41">−</text><text x="53" y="41" text-anchor="end">+</text></svg>`;
}

function roomCard(sp) {
  const steps = roomSteps();
  return `<div class="room" data-ip="${sp.ip}">
      <button class="room-key" data-act="toggle"><span class="led"></span><b>${esc(sp.name)}</b><small></small></button>
      <div class="room-plate">
        <div class="rknob" tabindex="0" role="slider" aria-label="${esc(sp.name)} level relative to the main volume"
          aria-valuemin="${steps[0]}" aria-valuemax="${steps[steps.length - 1]}"
          title="level relative to the main volume: drag, scroll or arrow keys; double-click resets">
          ${knobScale(steps)}<div class="knob"><div class="cap"></div></div>
        </div>
        <div class="plate-mid">
          <div class="lcd" aria-hidden="true"></div>
          <button class="room-main" data-act="main"><span class="led"></span>main</button>
        </div>
        <div class="plate-mute">
          <button class="mslide" data-act="mute" role="switch" aria-label="mute ${esc(sp.name)}"><span class="thumb"></span></button>
          <span class="mlab" data-act="mute"><span class="led"></span>mute</span>
        </div>
      </div>
    </div>`;
}

function roomAction(sp, what) {
  const inGroup = S.speakers.filter((x) => x.in_group);
  const send = (action, value) => act("/api/speakers", value === undefined ? { action, ip: sp.ip } : { action, ip: sp.ip, value });
  if (what === "mute") return send("mute", !sp.muted);
  if (what === "main") {
    if (sp.coordinator) return popup("main", sp.name, "leads the group and holds the queue", "var(--green)");
    if (!sp.in_group && !confirm(`Make ${sp.name} the main room? It joins the DJ group, and anything it's playing now will stop.`)) return;
    return send("main");
  }
  // toggle on/off
  if (!sp.in_group) {
    if (!confirm(`Add ${sp.name} to the DJ group? Anything it's playing now will stop.`)) return;
    return send("join");
  }
  if (sp.coordinator) {
    const msg = inGroup.length > 1
      ? `Switch off ${sp.name}? Another room in the group takes over as the main room and the music carries on.`
      : `${sp.name} is the only room playing. Switching it off pauses the music. Continue?`;
    if (!confirm(msg)) return;
  }
  return send("leave");
}

function setRoomOffset(ip, value) {
  const cur = pendingOffset[ip] ?? S.room_offsets?.[ip] ?? 0;
  if (value === cur) return;
  pendingOffset[ip] = value;
  renderRooms(S);
  const sp = S.speakers.find((x) => x.ip === ip);
  popup(sp ? sp.name.toLowerCase() : "room", fmtOffset(value), "vs the main volume", "var(--blue)");
  clearTimeout(offsetTimers[ip]);
  offsetTimers[ip] = setTimeout(async () => {  // one request once the knob settles
    try {
      render(await api("/api/speakers", { action: "offset", ip, value }));
    } catch (e) {
      flashFoot(e.message);
    }
    if (pendingOffset[ip] === value) delete pendingOffset[ip];
    renderRooms(S);
  }, 250);
}

function initRoomCard(el) {
  const ip = el.dataset.ip;
  $$("[data-act]", el).forEach((b) => b.addEventListener("click", () => {
    const sp = S.speakers.find((x) => x.ip === ip);
    if (sp) roomAction(sp, b.dataset.act);
  }));
  const knob = $(".rknob", el);
  const step = (d) => {
    const steps = roomSteps();
    const cur = pendingOffset[ip] ?? S.room_offsets?.[ip] ?? 0;
    const i = Math.max(0, Math.min(steps.length - 1, nearestStep(cur, steps) + d));
    setRoomOffset(ip, steps[i]);
  };
  let dragging = false, acc = 0, last = null, moved = 0, lastTap = 0;
  knob.addEventListener("pointerdown", (e) => {
    dragging = true; acc = 0; moved = 0; last = [e.clientX, e.clientY];
    knob.setPointerCapture(e.pointerId); knob.focus({ preventScroll: true });
  });
  knob.addEventListener("pointermove", (e) => {
    if (!dragging) return;
    const dx = e.clientX - last[0], dy = last[1] - e.clientY;  // right or up turns clockwise
    last = [e.clientX, e.clientY];
    acc += Math.abs(dx) > Math.abs(dy) ? dx : dy; moved += Math.abs(dx) + Math.abs(dy);
    const n = Math.trunc(acc / DRAG_PX);
    if (n) { acc -= n * DRAG_PX; step(n); }
  });
  knob.addEventListener("pointerup", (e) => {
    dragging = false;
    if (e.pointerType === "touch" && moved < 6) {  // touch has no dblclick: a double tap resets
      if (e.timeStamp - lastTap < 350) { setRoomOffset(ip, 0); lastTap = 0; } else lastTap = e.timeStamp;
    }
  });
  knob.addEventListener("pointercancel", () => { dragging = false; });
  let wheelAcc = 0;
  knob.addEventListener("wheel", (e) => {
    e.preventDefault();
    wheelAcc -= e.deltaY;
    if (Math.abs(wheelAcc) >= 40) { step(Math.sign(wheelAcc)); wheelAcc = 0; }
  }, { passive: false });
  knob.addEventListener("keydown", (e) => {
    const d = { ArrowUp: 1, ArrowRight: 1, ArrowDown: -1, ArrowLeft: -1 }[e.key];
    if (d) { e.preventDefault(); step(d); }
    if (e.key === "Home" || e.key === "0") { e.preventDefault(); setRoomOffset(ip, 0); }
  });
  knob.addEventListener("dblclick", () => setRoomOffset(ip, 0));
}

function renderRooms(s) {
  const wrap = $("#room-keys");
  if (!s.speakers.length) {
    wrap.innerHTML = `<div class="room empty"><b>no speakers found yet — press scan</b></div>`;
    wrap.dataset.layout = "";
    return;
  }
  const steps = roomSteps();
  const layout = JSON.stringify([s.speakers.map((sp) => [sp.ip, sp.name]), steps]);
  if (wrap.dataset.layout !== layout) {  // rebuild only when rooms change, so a knob mid-turn isn't replaced
    wrap.dataset.layout = layout;
    wrap.innerHTML = s.speakers.map(roomCard).join("");
    $$(".room", wrap).forEach(initRoomCard);
  }
  s.speakers.forEach((sp) => {
    const el = $(`.room[data-ip="${sp.ip}"]`, wrap);
    if (!el) return;
    const vol = s.volumes?.[sp.ip];
    const off = pendingOffset[sp.ip] ?? s.room_offsets?.[sp.ip] ?? 0;
    el.classList.toggle("main", sp.coordinator);
    el.classList.toggle("off", !sp.in_group);
    el.classList.toggle("muted", !!sp.muted);
    const key = $(".room-key", el);
    $(".led", key).className = "led" + (sp.in_group ? " on" : "") + (sp.coordinator ? " green" : "");
    key.title = sp.in_group ? "tap to switch this room off" : "tap to add this room";
    $("small", key).textContent = (sp.muted ? "muted" : sp.coordinator ? "main" : sp.in_group ? "synced" : "off")
      + (vol != null && sp.in_group ? " · vol " + vol : "");
    const mainKey = $(".room-main", el);
    mainKey.classList.toggle("is-main", sp.coordinator);
    mainKey.title = sp.coordinator ? "main room: leads the group and holds the queue" : "make this the main room";
    $(".led", mainKey).className = "led" + (sp.coordinator ? " on green" : "");
    const knob = $(".rknob", el);
    $(".cap", knob).style.setProperty("--rot", offsetAngle(off, steps) + "deg");
    const ni = nearestStep(off, steps);
    $$(".tick", knob).forEach((t) => t.classList.toggle("on", +t.dataset.i === ni));
    knob.setAttribute("aria-valuenow", off);
    knob.setAttribute("aria-valuetext", fmtOffset(off) + " vs main");
    const lcd = $(".lcd", el);
    lcd.textContent = fmtOffset(off);
    lcd.classList.toggle("zero", off === 0);
    const ms = $(".mslide", el);
    ms.classList.toggle("on", !!sp.muted);
    ms.setAttribute("aria-checked", sp.muted ? "true" : "false");
    $(".mlab .led", el).className = "led" + (sp.muted ? " on red" : "");
  });
  const all = s.speakers.every((sp) => sp.in_group);
  $("#k-party").classList.toggle("hot", all && s.speakers.length > 1);
}

function renderStrip(s) {
  const up = $("#upnext");
  const items = s.upcoming.filter(Boolean);
  const sig = JSON.stringify(items.map((t) => [t.id, t.up, t.down, t.energy]));
  if (up.dataset.sig !== sig) {
    up.dataset.sig = sig;
    up.innerHTML = items.length ? items.map((t) => `<li>
        <div class="t">${esc(t.title)}<span class="m">${esc(t.genre_label.toLowerCase())} · ${t.bpm ? Math.round(t.bpm) + " bpm" : "bpm ?"} · nrg ${t.energy ?? "?"}${t.cached ? " · cached" : ""}</span></div>
        <div class="votes"><button data-v="up" data-id="${esc(t.id)}" title="more like this">▲</button><button data-v="down" data-id="${esc(t.id)}" title="swap it out">▼</button></div>
      </li>`).join("") : `<li class="empty-note">the dj picks the next tracks once it's playing</li>`;
    $$("[data-v]", up).forEach((b) => b.addEventListener("click", () => act("/api/control", { action: b.dataset.v, id: b.dataset.id })));
  }
  const log = $("#log");
  const lsig = JSON.stringify(s.events);
  if (log.dataset.sig !== lsig) {
    log.dataset.sig = lsig;
    log.innerHTML = s.events.map((e) => `<li><b>${e.t}</b>${esc(e.msg)}</li>`).join("");
  }
}

// ── tape reels: left pack unwinds onto the right as the track plays ──────────
const REEL = { l: [27, 28], r: [93, 28], guideL: [16, 64], guideR: [104, 64], gr: 2.4, core: 6.5, full: 20 };
function packRadius(fraction) {  // tape area is conserved, so radius goes with sqrt
  return Math.sqrt(REEL.core ** 2 + (REEL.full ** 2 - REEL.core ** 2) * fraction);
}
function tangent([cx, cy], r, [px, py], outer) {  // point where tape leaves the pack toward a guide
  const d = Math.hypot(px - cx, py - cy), a = Math.atan2(py - cy, px - cx), b = Math.acos(Math.min(1, r / d));
  const t = a + (outer === "left" ? b : -b);
  return [cx + r * Math.cos(t), cy + r * Math.sin(t)];
}
const spin = { l: 0, r: 0, rl: REEL.full, rr: REEL.core, playing: false, last: 0 };
function drawTape(progress) {
  const rl = packRadius(1 - progress), rr = packRadius(progress);
  spin.rl = rl; spin.rr = rr;
  $("#pack-l").setAttribute("r", rl.toFixed(2));
  $("#pack-r").setAttribute("r", rr.toFixed(2));
  const [gx1, gy] = REEL.guideL, [gx2] = REEL.guideR, low = gy + REEL.gr;
  const a = tangent(REEL.l, rl, [gx1 - REEL.gr, gy], "left");
  const b = tangent(REEL.r, rr, [gx2 + REEL.gr, gy], "right");
  const f = (p) => p.map((v) => v.toFixed(2)).join(" ");
  $("#tape").setAttribute("d",
    `M${f(a)} L${f([gx1 - REEL.gr, gy])} A${REEL.gr} ${REEL.gr} 0 0 0 ${f([gx1, low])} L${f([gx2, low])} A${REEL.gr} ${REEL.gr} 0 0 0 ${f([gx2 + REEL.gr, gy])} L${f(b)}`);
}

// Tape runs at constant speed, so each reel turns at speed / pack radius.
// Tape leaves the left pack going down its left side and winds onto the right pack
// going up its right side: both reels turn counter-clockwise.
const TAPE_SPEED = 360 * 0.35 * REEL.full;  // deg/s * radius: a full pack turns 0.35 rev/s
const reduceMotion = matchMedia("(prefers-reduced-motion: reduce)").matches;
function spinReels(t) {
  const dt = spin.last ? Math.min(0.1, (t - spin.last) / 1000) : 0;
  spin.last = t;
  if (spin.playing && !reduceMotion) {
    spin.l = (spin.l - (TAPE_SPEED / spin.rl) * dt) % 360;
    spin.r = (spin.r - (TAPE_SPEED / spin.rr) * dt) % 360;
    $(".reel.r1").setAttribute("transform", `rotate(${spin.l.toFixed(1)} ${REEL.l[0]} ${REEL.l[1]})`);
    $(".reel.r2").setAttribute("transform", `rotate(${spin.r.toFixed(1)} ${REEL.r[0]} ${REEL.r[1]})`);
  }
  requestAnimationFrame(spinReels);
}
requestAnimationFrame(spinReels);

// ── utils ──────────────────────────────────────────────────────────────
function clamp(v, lo, hi) { return Math.max(lo, Math.min(hi, v)); }
function avg(a) { return a.length ? a.reduce((x, y) => x + y, 0) / a.length : null; }
function fmtSigned(v, dp = 0) { return (v > 0 ? "+" : v < 0 ? "−" : "±") + Math.abs(v).toFixed(dp); }
function fmtHour(h) { const m = Math.round((h % 1) * 60); return `${String(Math.floor(h) % 24).padStart(2, "0")}:${String(m % 60).padStart(2, "0")}`; }
function fmtTime(sec) { sec = sec || 0; return `${Math.floor(sec / 60)}:${String(sec % 60).padStart(2, "0")}`; }
function esc(s) { return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); }

// ── boot ────────────────────────────────────────────────────────────────
async function poll() {
  try {
    render(await api("/api/state"));
  } catch (e) {
    const f = $("#foot");
    f.textContent = "⚠ can't reach the dj server — is run.py still going?";
    f.classList.add("bad");
    setTimeout(() => f.classList.remove("bad"), 1400);
  }
}

// iOS Safari only shows :active pressed states if the page listens for touches
document.addEventListener("touchstart", () => {}, { passive: true });

initEncoders();
initKeys();
poll();
setInterval(poll, 1500);
