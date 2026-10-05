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

// ── faders ────────────────────────────────────────────────────────
// A fader maps its value onto the slot (0 = bottom, 1 = top). Dragging moves it relative to where
// you grab it (no jump to the finger), scroll and arrow keys step it, double-click / double-tap resets.
// Room levels use the same fader with printed detents.
function faderHTML(label, { ticks = 11, marks = null, centre = false } = {}) {
  const n = marks ? marks.length : ticks;
  const scale = Array.from({ length: n }, (_, k) =>
    `<i class="f-tick${centre && k === (n - 1) / 2 ? " mid" : ""}" style="--at:${k / (n - 1)}" data-k="${k}">${marks ? `<b>${marks[k]}</b>` : ""}</i>`).join("");
  return `<div class="f-body"><div class="f-scale" aria-hidden="true">${scale}</div>` +
    `<div class="f-slot"><i class="f-led"></i></div><div class="f-cap"><i></i></div></div>` +
    (label ? `<span class="f-label">${label}</span>` : "");
}

// o: { min(), max(), quantum, get(), set(v), reset() }
function bindFader(el, o) {
  const body = $(".f-body", el), cap = $(".f-cap", el);
  const snap = (v) => clamp(Math.round(v / o.quantum) * o.quantum, o.min(), o.max());
  const move = (v) => {
    v = snap(v);
    if (Math.abs(v - snap(o.get())) > 1e-9) o.set(+v.toFixed(4));
  };
  let drag = null, lastTap = 0, wheelAcc = 0;
  el.addEventListener("pointerdown", (e) => {
    if (!S) return;
    drag = { y: e.clientY, v: o.get(), moved: 0, travel: Math.max(40, body.clientHeight - cap.offsetHeight) };
    el.setPointerCapture(e.pointerId);
    el.focus({ preventScroll: true });
    el.classList.add("held");
  });
  el.addEventListener("pointermove", (e) => {
    if (!drag) return;
    const dy = drag.y - e.clientY;
    drag.moved = Math.max(drag.moved, Math.abs(dy));
    move(drag.v + (dy / drag.travel) * (o.max() - o.min()));
  });
  const end = (e) => {
    if (!drag) return;
    const tap = drag.moved < 6;
    drag = null;
    el.classList.remove("held");
    if (e && e.type === "pointerup" && e.pointerType === "touch" && tap) {  // touch has no dblclick
      if (e.timeStamp - lastTap < 350) { o.reset(); lastTap = 0; } else lastTap = e.timeStamp;
    }
  };
  el.addEventListener("pointerup", end);
  el.addEventListener("pointercancel", end);
  el.addEventListener("wheel", (e) => {
    e.preventDefault();
    if (!S) return;
    wheelAcc -= e.deltaY;
    if (Math.abs(wheelAcc) >= 30) { move(o.get() + Math.sign(wheelAcc) * o.quantum); wheelAcc = 0; }
  }, { passive: false });
  el.addEventListener("keydown", (e) => {
    if (!S) return;
    const d = { ArrowUp: 1, ArrowRight: 1, ArrowDown: -1, ArrowLeft: -1, PageUp: 5, PageDown: -5 }[e.key];
    if (d) { e.preventDefault(); move(o.get() + d * (e.shiftKey ? 5 : 1) * o.quantum); }
    if (e.key === "Home") { e.preventDefault(); o.reset(); }
  });
  el.addEventListener("dblclick", () => S && o.reset());
}

// Position the cap and the lit part of the slot (from the bottom, or from `origin` for a centred fader).
function placeFader(el, v, lo, hi, origin = lo, text = null) {
  const f = (x) => clamp((x - lo) / (hi - lo), 0, 1);
  const pos = f(v), o = f(origin);
  el.style.setProperty("--pos", pos.toFixed(4));
  el.style.setProperty("--f-lo", Math.min(o, pos).toFixed(4));
  el.style.setProperty("--f-hi", Math.max(o, pos).toFixed(4));
  el.setAttribute("aria-valuemin", lo);
  el.setAttribute("aria-valuemax", hi);
  el.setAttribute("aria-valuenow", Math.round(v * 100) / 100);
  if (text != null) el.setAttribute("aria-valuetext", text);
}

const calibrating = () => !!(S.calibration && S.calibration.wizard && !S.calibration.wizard.proposal);

const wizardFixed = (name) => {
  if (!(S.calibration && S.calibration.wizard)) return false;
  popup(name === "occ" ? "occupancy" : "time", "fixed", "set by the calibration scenario", "var(--dim)");
  return true;
};

const FADERS = {
  volume: {
    label: "volume", opts: { ticks: 11 },
    min: () => S.limits.min_volume, max: () => S.limits.max_volume, quantum: 1,
    get: () => (S.inputs.auto ? clamp(Math.round(S.targets.volume_base + S.inputs.volume_trim), S.limits.min_volume, S.limits.max_volume)
                               : S.inputs.manual_volume),
    set(v) {
      if (calibrating()) {  // the walk-through learns from how far you move it from its suggestion
        setInputs({ volume_trim: clamp(Math.round(v - S.targets.volume_base), -50, 50) });
        return popup("volume", v, "calibrating", "var(--blue)");
      }
      // Setting the volume by hand takes over from the schedule until auto vol is switched back on.
      setInputs({ auto: false, manual_volume: v });
      popup("volume", v, "manual · auto vol off", "var(--blue)");
    },
    reset() {
      if (calibrating()) { setInputs({ volume_trim: 0 }, 0); return popup("volume", "reset", "calibrating", "var(--blue)"); }
      setInputs({ auto: true, volume_trim: 0 }, 0);
      popup("volume", "auto", "back to the schedule", "var(--blue)");
    },
  },
  energy: {
    label: "energy", opts: { ticks: 11, centre: true }, origin: 0,
    min: () => -0.5, max: () => 0.5, quantum: 0.02,
    get: () => S.inputs.energy_trim,
    set(v) {
      setInputs({ energy_trim: v });
      popup("energy", fmtSigned(v, 2), v > 0 ? "more upbeat" : v < 0 ? "more mellow" : "as the dj sees it", "var(--green)");
    },
    reset() { setInputs({ energy_trim: 0 }, 0); popup("energy", "±0.00", "reset", "var(--green)"); },
  },
  occ: {
    label: "occupancy", opts: { ticks: 11 },
    min: () => 0, max: () => 100, quantum: 1,
    get: () => S.inputs.occupancy,
    set(v) {
      if (wizardFixed("occ")) return;
      setInputs({ occupancy: v, occupancy_enabled: true });
      const how = "space is " + (v > 85 ? "packed" : v > 55 ? "busy" : v > 25 ? "steady" : "quiet");
      popup("occupancy", v + "%", S.mode === "live" ? `override for ${(S.feeds && S.feeds.override_minutes) || 60} min · ${how}` : how, "var(--white)");
    },
    reset() {},
  },
  time: {
    label: "time", opts: { ticks: 9 },  // a tick every 3 hours
    min: () => 0, max: () => 23.75, quantum: 0.25,
    get: () => S.inputs.hour_override ?? S.targets.hour,
    set(v) {
      if (wizardFixed("time")) return;
      if (S.mode === "live") return popup("time", fmtHour(S.targets.hour), "live mode follows the venue clock", "var(--orange)");
      setInputs({ hour_override: v });
      popup("time", fmtHour(v), "simulated · double-tap or clock key to reset", "var(--orange)");
    },
    reset() {
      if (S.mode === "live") return;
      setInputs({ hour_override: null }, 0);
      popup("time", "clock", "following the real time", "var(--orange)");
    },
  },
};

// ── eq mode ───────────────────────────────────────────────────────────
// Like the OP-1's modes: the eq key turns the blue and green faders into bass and treble (and the screen
// into an eq page) until eq is pressed again or nothing is touched for a while. Tapping a room's plate
// tunes just that room, on top of the all-rooms setting. Nothing new stays on the panel day to day.
const EQ_IDLE_MS = 10000;
const eqMode = { on: false, room: null, timer: null };
const pendingEq = {};  // "" (all rooms) or room name -> {bass?, treble?} sent but not yet confirmed
const eqTimers = {};

function eqStored(band, room) {
  const src = room ? (S.eq.rooms || {})[room] || {} : S.eq;
  return src[band] || 0;
}
function eqValue(band, room = eqMode.room) {  // all rooms, or a room's trim on top
  const p = pendingEq[room || ""];
  return p && p[band] != null ? p[band] : eqStored(band, room);
}
function eqEffective(band, room) {
  const r = S.eq.range;
  return clamp(eqValue(band, null) + (room ? eqValue(band, room) : 0), -r, r);
}

function eqBump() {
  clearTimeout(eqMode.timer);
  eqMode.timer = setTimeout(exitEq, EQ_IDLE_MS);
}
function enterEq() {
  if (!S || !S.eq) return;
  if (S.calibration && S.calibration.wizard) return popup("eq", "later", "finish calibrating first", "var(--dim)");
  Object.assign(eqMode, { on: true, room: null });
  eqBump();
  render(S, true);
}
function exitEq() {
  clearTimeout(eqMode.timer);
  Object.assign(eqMode, { on: false, room: null });
  if (S) render(S, true);
}

function setEq(band, v) {
  const room = eqMode.room, k = room || "";
  (pendingEq[k] = pendingEq[k] || {})[band] = v;
  eqBump();
  render(S, true);
  popup(room ? room.toLowerCase() : "all rooms", fmtSigned(v), band + (room ? " vs all rooms" : ""),
    band === "bass" ? "var(--blue)" : "var(--green)");
  clearTimeout(eqTimers[k]);
  eqTimers[k] = setTimeout(async () => {  // one request once the fader settles
    const sent = { ...pendingEq[k] };
    try {
      render(await api("/api/eq", room ? { ...sent, room } : sent));
    } catch (e) {
      flashFoot(e.message);
    }
    if (JSON.stringify(pendingEq[k]) === JSON.stringify(sent)) delete pendingEq[k];
    render(S, true);
  }, 250);
}

function eqFader(band) {
  return {
    label: band, opts: { ticks: 11, centre: true }, origin: 0,
    min: () => -S.eq.range, max: () => S.eq.range, quantum: 1,
    get: () => eqValue(band), set: (v) => setEq(band, v), reset: () => setEq(band, 0),
  };
}
const EQ_FADERS = { volume: eqFader("bass"), energy: eqFader("treble") };
const EQ_WAITING = {
  set() { eqBump(); popup("eq", "tone", "occupancy and time wait until eq is done", "var(--dim)"); },
  reset() {},
};
function faderFor(name) {
  if (!eqMode.on) return FADERS[name];
  return EQ_FADERS[name] || { ...FADERS[name], ...EQ_WAITING };
}

function initFaders() {
  $$(".enc").forEach((el) => {
    const name = el.dataset.enc, f = FADERS[name];
    el.innerHTML = faderHTML(f.label, f.opts);
    el.title = "drag or scroll · double-click to reset";
    bindFader(el, {  // whichever job the fader has right now (normal, or bass / treble in eq mode)
      get quantum() { return faderFor(name).quantum; },
      min: () => faderFor(name).min(), max: () => faderFor(name).max(),
      get: () => faderFor(name).get(), set: (v) => faderFor(name).set(v), reset: () => faderFor(name).reset(),
    });
  });
}

function renderFaders(s) {
  const i = s.inputs;
  $$(".enc").forEach((el) => {
    const name = el.dataset.enc, f = faderFor(name);
    const v = f.get();
    const text = eqMode.on && EQ_FADERS[name] ? `${f.label} ${fmtSigned(v)}`
      : { volume: String(v), energy: fmtSigned(v, 2), occ: v + "%", time: fmtHour(v) }[name];
    placeFader(el, v, f.min(), f.max(), f.origin ?? f.min(), text);
    const label = $(".f-label", el);
    if (label && label.textContent !== f.label) label.textContent = f.label;
    const ticks = $$(".f-tick", el), mid = (ticks.length - 1) / 2;
    ticks.forEach((t) => t.classList.toggle("mid", !!(f.opts && f.opts.centre) && +t.dataset.k === mid));
  });
  // dimmed when they have no effect
  $('[data-enc="occ"]').classList.toggle("dimmed", eqMode.on || !i.occupancy_enabled);
  $('[data-enc="time"]').classList.toggle("dimmed", eqMode.on || i.hour_override == null || s.mode === "live");
  $('[data-enc="volume"]').classList.toggle("auto", !eqMode.on && !!i.auto);
  $(".encoders").classList.toggle("eq", eqMode.on);
}

// A picture of the tone: a low shelf (bass) and a high shelf (treble) over 20 Hz..20 kHz.
function eqCurve(bass, treble) {
  const pts = [];
  for (let k = 0; k <= 60; k++) {
    const f = 20 * Math.pow(1000, k / 60);
    const low = 1 / (1 + Math.pow(f / 180, 1.6)), high = 1 / (1 + Math.pow(3500 / f, 1.6));
    pts.push(`${(k / 60 * 200).toFixed(1)} ${(30 - (bass * low + treble * high) * 2.6).toFixed(1)}`);
  }
  return "M" + pts.join("L");
}

function renderEq(s) {
  const key = $("#k-eq"), view = $("#eq-view");
  key.hidden = !s.eq;  // older server
  if (!s.eq) return;
  const tuned = s.eq.bass || s.eq.treble || Object.keys(s.eq.rooms || {}).length;
  $(".led", key).className = "led" + (tuned ? " on" : "");
  key.classList.toggle("hot", eqMode.on);
  key.title = eqMode.on ? "done (or wait a few seconds)" : "tone: bass and treble for all rooms, or tap a room to tune just that one";
  if (eqMode.on && s.calibration && s.calibration.wizard) exitEq();
  view.hidden = !eqMode.on;
  if (!eqMode.on) return;
  const room = eqMode.room;
  const b = eqEffective("bass", room), t = eqEffective("treble", room);
  $("#eq-target").textContent = room ? room.toLowerCase() : "all rooms";
  $("#eq-bass").textContent = fmtSigned(b);
  $("#eq-treble").textContent = fmtSigned(t);
  const trimmed = Object.keys(s.eq.rooms || {}).length;
  $("#eq-note").textContent = room
    ? `this room ${fmtSigned(eqValue("bass", room))} / ${fmtSigned(eqValue("treble", room))} vs all rooms`
    : trimmed ? `${trimmed} room${trimmed === 1 ? "" : "s"} tuned separately` : "tap a room to tune just that one";
  $("#eq-path").setAttribute("d", eqCurve(b, t));
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
      return popup("occupancy", S.inputs.occupancy_enabled ? S.inputs.occupancy + "%" : "—", "comes from live data · move the fader to override", "var(--white)");
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
    // switching to manual keeps the current loudness as the starting point; back on returns to the schedule
    setInputs(auto ? { auto, volume_trim: 0 } : { auto, manual_volume: S.targets.volume }, 0);
    if (auto) popup("volume", "auto", "back to the schedule", "var(--blue)");
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
  $("#k-listen").addEventListener("click", () => {
    if (!S) return;
    if (listenOn(S) && listen.blocked) return syncListen(S);  // the browser was waiting for a tap
    const next = listenOn(S) ? "off" : "on";
    try { localStorage.setItem(LISTEN_KEY, next); } catch (e) { /* private mode: applies to this page only */ }
    listen.pref = next;
    syncListen(S);
    popup("listen", next, next === "on" ? "playing on this device, in step with the speakers" : "this device is quiet", "var(--green)");
  });
  $("#k-down").addEventListener("click", () => act("/api/control", { action: "down" }));
  $("#k-party").addEventListener("click", () => {
    const others = S.speakers.filter((sp) => !sp.in_group).map((sp) => sp.name);
    if (!others.length) return;
    if (!confirm(`Party mode pulls every room into the DJ's group:\n\n${others.join("\n")}\n\nAnything those rooms are playing now (e.g. Spotify) will stop. Continue?`)) return;
    act("/api/speakers", { action: "party" });
  });
  $("#k-scan").addEventListener("click", () => act("/api/speakers", { action: "discover" }));
  $("#k-eq").addEventListener("click", () => (eqMode.on ? exitEq() : enterEq()));

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
  marquee($("#s-title"), n ? n.title : s.running ? "…" : st.other_source ? (/spotify/i.test(st.uri) ? "spotify is playing" : "another source is playing") : "press ▶ to start the dj");
  $("#s-meta").textContent = n
    ? [n.genre_label, n.bpm ? Math.round(n.bpm) + " bpm" : "bpm ?", n.cached ? "cached" : "stream",
       n.gain_db != null ? "lvl " + fmtSigned(n.gain_db, 1) + " dB" : "", n.up || n.down ? `▲${n.up} ▼${n.down}` : ""].filter(Boolean).join(" · ")
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

  renderFaders(s);

  // keys
  renderGenreKeys(s);
  $$("[data-weather]").forEach((k) => $(".led", k).classList.toggle("on", k.dataset.weather === i.weather));
  $(".led", $("#k-occ")).classList.toggle("on", i.occupancy_enabled);
  $(".led", $("#k-auto")).classList.toggle("on", i.auto);
  $(".led", $("#k-clock")).classList.toggle("on", i.hour_override == null);
  renderMode(s);
  renderCalibration(s);
  renderEq(s);
  $("em", $("#k-occ")).textContent = s.mode === "live" && (s.feeds || {}).occupancy_src === "override" ? "occ · live" : "occ";
  $(".led", $("#k-play")).className = "led" + (playing ? " on green" : "");
  $("#play-icon").innerHTML = playing
    ? '<path d="M7 5h4v14H7zM13 5h4v14h-4z" class="solid"/>'
    : '<path d="M8 5l11 7-11 7z" class="solid"/>';
  $("em", $("#k-play")).textContent = playing ? "pause" : s.running && s.paused ? "resume" : "play";

  renderRooms(s);
  renderStrip(s);
  if (!local) noteSpeakerClock(st);
  syncListen(s);

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
  marquee($("#cv-song"), s.now ? `♪ ${s.now.title}` + (s.now.bpm ? ` · ${Math.round(s.now.bpm)} bpm` : "") : "♪ …");
  $("#cv-vol").textContent = s.targets.volume;
  $("#cv-nrg").textContent = s.targets.energy.toFixed(2);
}

// ── marquee: text too long for the screen scrolls like a hardware display, pausing at the start of each pass ──
const MQ_PAUSE = 2200;   // ms held at the start
const mqWidths = new WeakMap();
const mqObserver = "ResizeObserver" in window ? new ResizeObserver((entries) => entries.forEach((e) => {
  const w = Math.round(e.contentRect.width);
  if (mqWidths.get(e.target) !== w) { mqWidths.set(e.target, w); fitMarquee(e.target); }
})) : null;

function marquee(el, text) {
  if (el.dataset.mq === text) return;
  el.dataset.mq = text;
  el.innerHTML = '<span class="mq"><span class="mq-t"></span></span>';
  $(".mq-t", el).textContent = text;
  if (mqObserver && !el.dataset.mqWatched) { el.dataset.mqWatched = "1"; mqObserver.observe(el); }
  fitMarquee(el);
}

function fitMarquee(el) {
  const inner = $(".mq", el);
  if (!inner) return;
  inner.getAnimations().forEach((a) => a.cancel());
  $$(".mq-t", inner).slice(1).forEach((c) => c.remove());
  el.classList.remove("scrolling");
  const first = $(".mq-t", inner);
  if (reduceMotion || first.offsetWidth <= el.clientWidth + 1) return;
  // a second copy follows a gap behind the first, so the loop is seamless
  const fs = parseFloat(getComputedStyle(el).fontSize) || 14;
  const gap = Math.round(fs * 3);
  const copy = first.cloneNode(true);
  copy.setAttribute("aria-hidden", "true");
  copy.style.marginLeft = gap + "px";
  inner.append(copy);
  el.classList.add("scrolling");
  const dist = first.offsetWidth + gap;
  const run = (dist / (fs * 2.6)) * 1000;  // speed: 2.6 × font size per second, a few characters a second
  const total = MQ_PAUSE + run;
  inner.animate([
    { transform: "translateX(0)", offset: 0 },
    { transform: "translateX(0)", offset: MQ_PAUSE / total },
    { transform: `translateX(${-dist}px)`, offset: 1 },
  ], { duration: total, iterations: Infinity });
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
    wrap.innerHTML = `<button class="key dark" data-moods title="add genres for the time of day and weather"><span class="led"></span><em>auto</em></button>` +
      keys.map((g, k) =>
      `<button class="key" data-genre="${g}"><span class="led"></span><span class="swatch" style="--sw:${genreColor(g, k)}"></span><em></em><span class="count"></span></button>`).join("");
    $$("[data-genre]", wrap).forEach((b) => b.addEventListener("click", () => toggleGenre(b.dataset.genre)));
    $("[data-moods]", wrap).addEventListener("click", () => setInputs({ moods: S.inputs.moods === false }, 0));
    const sel = $("#add-genre");
    sel.innerHTML = keys.map((g) => `<option value="${g}">${esc(s.genres[g].label.toLowerCase())}</option>`).join("");
  }
  $$("[data-genre]", wrap).forEach((b) => {
    const g = s.genres[b.dataset.genre];
    $("em", b).textContent = g.label.toLowerCase();
    $(".count", b).textContent = `${g.cached}/${g.tracks}`;
    $(".led", b).classList.toggle("on", s.inputs.genres.includes(b.dataset.genre));
  });
  $(".led", $("[data-moods]", wrap)).classList.toggle("on", s.inputs.moods !== false);
}

const pendingOffset = {};  // ip -> offset set on the fader but not yet confirmed by the server
const offsetTimers = {};

function fmtOffset(o) {
  return o > 0 ? `+${o}` : o < 0 ? `−${-o}` : "0";
}

function roomSteps() {
  return (S && S.limits && S.limits.room_offset_steps) || [-10, -5, 0, 5, 10];
}

function nearestStep(o, steps) {
  return steps.reduce((best, v, i) => (Math.abs(v - o) < Math.abs(steps[best] - o) ? i : best), 0);
}

function roomOffset(ip) {
  return pendingOffset[ip] ?? S.room_offsets?.[ip] ?? 0;
}

// A channel strip per room: the name and a small LCD sit on a raised (not pressable) white plate; the controls are a level fader
// (detents relative to the main volume), an on/off switch, a mute switch and a latching "main" key.
function roomStrip(sp) {
  const steps = roomSteps();
  return `<div class="room" data-ip="${sp.ip}">
      <div class="ch-plate">
        <div class="ch-head"><span class="led"></span><b>${esc(sp.name)}</b></div>
        <div class="lcd" aria-hidden="true"><span class="lcd-st"></span><span class="lcd-vol"></span></div>
      </div>
      <div class="ch-body">
        <div class="rfader" tabindex="0" role="slider" aria-label="${esc(sp.name)} level relative to the main volume"
          title="level vs the main volume: drag, scroll or arrow keys; double-click resets">
          ${faderHTML("", { marks: steps.map(fmtOffset), centre: steps.includes(0) && steps.length % 2 === 1 })}
        </div>
        <div class="ch-switches">
          <div class="sw-row"><button class="mslide on-sw" data-act="toggle" role="switch" aria-label="${esc(sp.name)} on"><span class="thumb"></span></button>
            <span class="mlab" data-act="toggle">on</span></div>
          <div class="sw-row"><button class="mslide mute-sw" data-act="mute" role="switch" aria-label="mute ${esc(sp.name)}"><span class="thumb"></span></button>
            <span class="mlab" data-act="mute">mute</span></div>
          <button class="room-main" data-act="main"><span class="led"></span>main</button>
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
  // on/off
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
  if (value === roomOffset(ip)) return;
  pendingOffset[ip] = value;
  renderRooms(S);
  const sp = S.speakers.find((x) => x.ip === ip);
  popup(sp ? sp.name.toLowerCase() : "room", fmtOffset(value), "vs the main volume", "var(--blue)");
  clearTimeout(offsetTimers[ip]);
  offsetTimers[ip] = setTimeout(async () => {  // one request once the fader settles
    try {
      render(await api("/api/speakers", { action: "offset", ip, value }));
    } catch (e) {
      flashFoot(e.message);
    }
    if (pendingOffset[ip] === value) delete pendingOffset[ip];
    renderRooms(S);
  }, 250);
}

function initRoomStrip(el) {
  const ip = el.dataset.ip;
  $$("[data-act]", el).forEach((b) => b.addEventListener("click", () => {
    const sp = S.speakers.find((x) => x.ip === ip);
    if (sp) roomAction(sp, b.dataset.act);
  }));
  // in eq mode, the room's plate picks it for tuning (tap again for all rooms)
  $(".ch-plate", el).addEventListener("click", () => {
    const sp = S && S.speakers.find((x) => x.ip === ip);
    if (!eqMode.on || !sp) return;
    eqMode.room = eqMode.room === sp.name ? null : sp.name;
    eqBump();
    render(S, true);
    popup("eq", eqMode.room ? sp.name.toLowerCase() : "all rooms", eqMode.room ? "tap again for all rooms" : "", "var(--blue)");
  });
  // the fader's value is the detent index; each detent is an offset from room_offset_steps
  bindFader($(".rfader", el), {
    min: () => 0, max: () => roomSteps().length - 1, quantum: 1,
    get: () => nearestStep(roomOffset(ip), roomSteps()),
    set: (i) => setRoomOffset(ip, roomSteps()[i]),
    reset: () => setRoomOffset(ip, 0),
  });
}

function renderRooms(s) {
  const wrap = $("#room-keys");
  if (!s.speakers.length) {
    wrap.innerHTML = `<div class="room empty"><b>no speakers found yet · press scan</b></div>`;
    wrap.dataset.layout = "";
    return;
  }
  const steps = roomSteps();
  const layout = JSON.stringify([s.speakers.map((sp) => [sp.ip, sp.name]), steps]);
  if (wrap.dataset.layout !== layout) {  // rebuild only when rooms change, so a fader mid-drag isn't replaced
    wrap.dataset.layout = layout;
    wrap.innerHTML = s.speakers.map(roomStrip).join("");
    $$(".room", wrap).forEach(initRoomStrip);
  }
  s.speakers.forEach((sp) => {
    const el = $(`.room[data-ip="${sp.ip}"]`, wrap);
    if (!el) return;
    const vol = s.volumes?.[sp.ip];
    const off = roomOffset(sp.ip);
    el.classList.toggle("main", sp.coordinator);
    el.classList.toggle("off", !sp.in_group);
    el.classList.toggle("muted", !!sp.muted);
    $(".ch-head .led", el).className = "led" + (sp.in_group ? " on" : "") + (sp.coordinator ? " green" : "");
    el.classList.toggle("eq-pick", eqMode.on);
    el.classList.toggle("eq-sel", eqMode.on && eqMode.room === sp.name);
    if (eqMode.on && s.eq) {  // the LCD shows each room's tone while tuning
      $(".lcd-st", el).textContent = `b${fmtSigned(eqEffective("bass", sp.name))} t${fmtSigned(eqEffective("treble", sp.name))}`;
      $(".lcd-vol", el).textContent = s.eq.rooms && s.eq.rooms[sp.name] ? "own" : "";
    } else {
      $(".lcd-st", el).textContent = !sp.in_group ? "off" : sp.muted ? "muted" : sp.coordinator ? "main" : "synced";
      $(".lcd-vol", el).textContent = vol != null && sp.in_group ? "vol " + vol : "";
    }
    const on = $(".on-sw", el);
    on.classList.toggle("on", sp.in_group);
    on.setAttribute("aria-checked", String(sp.in_group));
    on.title = sp.in_group ? "switch this room off" : "add this room to the DJ group";
    const mute = $(".mute-sw", el);
    mute.classList.toggle("on", !!sp.muted);
    mute.setAttribute("aria-checked", String(!!sp.muted));
    const mainKey = $(".room-main", el);
    mainKey.classList.toggle("is-main", sp.coordinator);
    mainKey.title = sp.coordinator ? "main room: leads the group and holds the queue" : "make this the main room";
    $(".led", mainKey).className = "led" + (sp.coordinator ? " on green" : "");
    const fader = $(".rfader", el), ni = nearestStep(off, steps);
    placeFader(fader, ni, 0, steps.length - 1, steps.includes(0) ? steps.indexOf(0) : 0, fmtOffset(off) + " vs main");
    $$(".f-tick", fader).forEach((t) => t.classList.toggle("on", +t.dataset.k === ni));
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

// ── listen: play what the speakers are playing on this device ──────────────
// The speakers stream from this server's /media URLs, so the browser can fetch the same file and follow
// the speaker's position. On by default in demo mode; the key's choice is remembered per device.
const LISTEN_KEY = "o3dj.listen";
// Two players take turns so a new song can fade in while the last one fades out, like the speakers' crossfade.
const XFADE_S = 5;
function newListenAudio() {
  const a = new Audio();
  a.preload = "auto";
  a.preservesPitch = true;  // speed nudges below shouldn't bend the pitch
  a.addEventListener("playing", () => S && renderListenKey(listenOn(S)));
  a.addEventListener("pause", () => S && renderListenKey(listenOn(S)));
  return a;
}
const listen = { els: [newListenAudio(), newListenAudio()], cur: 0, pref: null, blocked: false, starting: false, told: false,
                 uri: null, start: null, rtt: 0, base: 1, gain: 1, out: null, fadeTimer: null, unlocked: false };
Object.defineProperty(listen, "audio", { get: () => listen.els[listen.cur] });
Object.defineProperty(listen, "spare", { get: () => listen.els[1 - listen.cur] });
// iOS Safari ignores audio.volume, so it can't fade: there the new song simply takes over, as before.
const CAN_FADE = (() => { const t = new Audio(); t.volume = 0.5; return t.volume === 0.5; })();
try { listen.pref = localStorage.getItem(LISTEN_KEY); } catch (e) { /* storage blocked */ }

function listenOn(s) { return listen.pref ? listen.pref === "on" : s.mode !== "live"; }
function mediaPath(uri) {
  try { const u = new URL(uri); return u.pathname.startsWith("/media/") ? u.pathname : null; } catch (e) { return null; }
}

// The speaker's clock, as "when (on this device's clock) the current track started". Each status reading gives
// an estimate: the speaker position, plus how old the reading was on the server, plus half the request time.
// Readings can only run late (Sonos rounds down to whole seconds), so the earliest start seen is the best one;
// a big jump means the speaker really moved (skip, seek, stall) and the estimate starts over.
function noteSpeakerClock(st) {
  if (st.state !== "PLAYING" || !st.uri) { listen.uri = null; listen.start = null; return; }
  const pos = st.position ?? (st.elapsed || 0) + 0.5;
  const est = performance.now() / 1000 - (pos + (st.age || 0) + listen.rtt / 2);
  if (st.uri !== listen.uri || listen.start == null || Math.abs(est - listen.start) > 2) {
    listen.uri = st.uri;
    listen.start = est;
  } else {
    listen.start = Math.min(listen.start, est);
  }
}
function applyListenVolume() {
  listen.audio.volume = clamp(listen.base * listen.gain, 0, 1);
  if (listen.out) listen.out.el.volume = clamp(listen.base * listen.out.gain, 0, 1);
}

function endCrossfade() {
  clearInterval(listen.fadeTimer);
  if (listen.out) listen.out.el.pause();
  listen.out = null;
  listen.gain = 1;
  applyListenVolume();
}

// Hand over to the spare player: the old song fades out while the new one fades in (equal power, so the
// overall level holds steady through the overlap).
function crossfadeToSpare() {
  endCrossfade();
  const old = listen.audio;
  listen.cur = 1 - listen.cur;
  listen.out = { el: old, gain: 1 };
  listen.gain = 0;
  applyListenVolume();
  const t0 = performance.now();
  listen.fadeTimer = setInterval(() => {
    const k = clamp((performance.now() - t0) / (XFADE_S * 1000), 0, 1);
    listen.gain = Math.sin(k * Math.PI / 2);
    if (listen.out) listen.out.gain = Math.cos(k * Math.PI / 2);
    applyListenVolume();
    if (k >= 1) endCrossfade();
  }, 50);
}

// Browsers (iOS especially) only let a player make sound once it has been started from a tap; start the
// spare silently during the first tap so it can take over later without one.
function unlockSpare() {
  if (listen.unlocked || !listen.audio.src) return;
  listen.unlocked = true;
  const b = listen.spare;
  b.muted = true;
  b.src = listen.audio.src;
  b.play().then(() => { b.pause(); b.muted = false; }).catch(() => { b.muted = false; listen.unlocked = false; });
}

function listenTarget() { return listen.start == null ? 0 : performance.now() / 1000 - listen.start; }

function syncListen(s) {
  let a = listen.audio;
  const st = s.status || {};
  const on = listenOn(s);
  const path = s.running && !s.paused && st.state === "PLAYING" ? mediaPath(st.uri) : null;  // null: not our music
  // On only by the demo default: a page in the background (another tab, a hidden window) stays quiet, so
  // a forgotten tab doesn't play over the one you're using. Switched on by hand: keeps playing (e.g. phone locked).
  const background = document.hidden && listen.pref !== "on";
  if (!on || !path || background) {
    endCrossfade();
    if (!a.paused) a.pause();
    listen.blocked = false;
    return renderListenKey(on);
  }
  // This device is one more speaker in the group: it follows the group's level (so fades and auto volume are
  // heard), without the main room's own offset or mute. The device volume sets the overall level.
  const main = s.speakers.find((sp) => sp.coordinator);
  const vol = main && s.volumes ? s.volumes[main.ip] : null;
  listen.base = vol != null ? clamp((vol - (s.room_offsets?.[main.ip] || 0)) / s.limits.max_volume, 0, 1) : 1;
  applyListenVolume();
  if (a.dataset.path !== path) {
    if (CAN_FADE && a.dataset.path && !a.paused) {  // the speakers moved on to the next song: crossfade like they do
      crossfadeToSpare();
      a = listen.audio;
    } else {
      endCrossfade();
    }
    a.dataset.path = path;
    a.src = path;
    a.playbackRate = 1;
    a.addEventListener("loadedmetadata", () => { a.currentTime = listenTarget(); }, { once: true });
  } else if (a.readyState >= 2 && !a.seeking && !a.paused) {
    // Stay in step without audible jumps: speed up or slow down a few percent for small drift, and only
    // re-seek when far off. Real Sonos positions are only accurate to about a second, so allow more slack there.
    const off = listenTarget() - a.currentTime;  // > 0: this device is behind the speaker
    const [slack, far] = s.live ? [0.5, 2.5] : [0.05, 1.0];
    if (Math.abs(off) > far) { a.currentTime = listenTarget(); a.playbackRate = 1; }
    else if (Math.abs(off) > slack) a.playbackRate = 1 + clamp(off * 0.3, -0.06, 0.06);
    else a.playbackRate = 1;
  }
  if (a.paused && !listen.starting) {
    listen.starting = true;
    a.play().then(() => { listen.blocked = false; unlockSpare(); }).catch((e) => {
      if (e.name !== "NotAllowedError") return;
      listen.blocked = true;  // browsers only start sound after a tap; the next tap anywhere starts it
      if (!listen.told) { listen.told = true; popup("listen", "tap", "tap anywhere to hear it on this device", "var(--green)"); }
    }).finally(() => { listen.starting = false; renderListenKey(listenOn(S)); });
  }
  renderListenKey(on);
}

function renderListenKey(on) {
  const sw = $("#k-listen"), a = listen.audio;
  sw.classList.toggle("on", on);
  sw.setAttribute("aria-checked", String(on));
  sw.classList.toggle("busy", on && listen.blocked);
  $(".side-label .led").className = "led" + (on && !a.paused ? " on green" : on && listen.blocked ? " on" : "");
  sw.title = !on ? "listen on this device, in step with the speakers"
    : listen.blocked ? "tap to start listening on this device" : "listening on this device · slide to turn off";
}

document.addEventListener("visibilitychange", () => S && syncListen(S));
for (const ev of ["click", "touchend", "keydown"]) {
  document.addEventListener(ev, () => { if (listen.blocked && S) syncListen(S); unlockSpare(); }, true);
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
    const t0 = performance.now();
    const data = await api("/api/state");
    listen.rtt = (performance.now() - t0) / 1000;
    render(data);
  } catch (e) {
    const f = $("#foot");
    f.textContent = "⚠ can't reach the dj server — is run.py still going?";
    f.classList.add("bad");
    setTimeout(() => f.classList.remove("bad"), 1400);
  }
}

// iOS Safari only shows :active pressed states if the page listens for touches
document.addEventListener("touchstart", () => {}, { passive: true });

initFaders();
initKeys();
poll();
setInterval(poll, 1500);
