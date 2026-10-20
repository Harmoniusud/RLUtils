// The window's UI. Python (rlutils/app.py) sends the whole view state as JSON over QWebChannel (bridge.state);
// this file draws it and sends clicks back with bridge.call(action, json). Pure presentation: every number is
// worked out in Python (rlutils/viewmodel.py).
"use strict";

let S = null;            // last state from Python
let bridge = null;
const U = { screen: "MMR", profileOpen: false, trayOpen: false, ctx: null, modal: null, hover: null };
const PAGES = ["MMR", "Games", "Play", "Settings"];

const esc = v => String(v == null ? "" : v).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const attr = v => esc(JSON.stringify(v === undefined ? null : v));
const act = (a, v) => `data-a="${a}"` + (v === undefined ? "" : ` data-v="${attr(v)}"`);

function call(a, v) { if (bridge) bridge.call(a, JSON.stringify(v === undefined ? null : v)); }

// ---------------------------------------------------------------------------------------------------
// Regions. Each is redrawn only when its HTML changes, keeping scroll positions and focused inputs.

const last = {};
function paint(id, html) {
  if (last[id] === html) return;
  const el = document.getElementById(id);
  const scrolls = [...el.querySelectorAll("[data-scroll]")].map(e => [e.dataset.scroll, e.scrollTop]);
  const mainTop = id === "main" ? el.scrollTop : 0;
  const f = document.activeElement;
  const focus = f && el.contains(f) && f.id ? { id: f.id, value: f.value, s: f.selectionStart, e: f.selectionEnd } : null;
  el.innerHTML = html;
  last[id] = html;
  for (const [k, top] of scrolls) { const e = el.querySelector(`[data-scroll="${k}"]`); if (e) e.scrollTop = top; }
  if (id === "main") el.scrollTop = mainTop;
  if (focus) { const e = document.getElementById(focus.id); if (e) { e.value = focus.value; e.focus(); try { e.setSelectionRange(focus.s, focus.e); } catch (_) {} } }
}

let pointerDown = false, pending = false;
function render() {
  if (!S) return;
  if (pointerDown) { pending = true; return; }  // don't replace a slider or button while it's held
  document.body.classList.toggle("max", !!S.app.max);
  document.getElementById("maxbtn").innerHTML = S.app.max
    ? '<svg viewBox="0 0 10 10"><rect x="0.5" y="2.5" width="7" height="7"/><path d="M2.5 2.5 V0.5 H9.5 V7.5 H7.5"/></svg>'
    : '<svg viewBox="0 0 10 10"><rect x="0.5" y="0.5" width="9" height="9"/></svg>';
  document.getElementById("maxbtn").title = S.app.max ? "Restore" : "Maximize";
  document.getElementById("apptitle").textContent = S.app.title;
  paint("nav", nav());
  paint("header", header());
  paint("main", ({ MMR: mmrPage, Games: gamesPage, Play: playPage, Settings: settingsPage })[U.screen]());
  paint("footer", footer());
  importer();
  paint("setup", setupPanel());
  paint("overlay", overlay());
  if (U.screen === "MMR") drawTip();
}

// ---------------------------------------------------------------------------------------------------

function nav() {
  const item = (k, icon) => `<div class="navi${U.screen === k ? " on" : ""}" ${act("go", k)} title="${k}  (Ctrl+${PAGES.indexOf(k) + 1})">${icon}${k}</div>`;
  const bars = '<div style="display:flex;align-items:flex-end;gap:3px;height:16px;"><div style="width:4px;height:7px;background:currentColor;border-radius:1px;"></div><div style="width:4px;height:12px;background:currentColor;border-radius:1px;"></div><div style="width:4px;height:16px;background:currentColor;border-radius:1px;"></div></div>';
  const grid = '<div style="display:grid;grid-template-columns:7px 7px;gap:2px;"><div style="height:7px;background:currentColor;border-radius:1px;"></div><div style="height:7px;background:currentColor;border-radius:1px;"></div><div style="height:7px;background:currentColor;border-radius:1px;"></div><div style="height:7px;border:1.5px solid currentColor;border-radius:1px;"></div></div>';
  const play = '<div style="width:0;height:0;border-left:13px solid currentColor;border-top:8px solid transparent;border-bottom:8px solid transparent;margin-left:3px;"></div>';
  const gear = '<div style="width:16px;height:16px;border-radius:50%;border:4px dotted currentColor;"></div>';
  let h = item("MMR", bars) + item("Games", grid) + item("Play", play) + item("Settings", gear) + '<div style="flex:1;"></div>';
  h += `<div class="avatar" ${act("toggleProfile")} title="Switch profile">${esc(S.profile ? S.profile.initial : "?")}</div>`;
  if (U.profileOpen) {
    h += `<div id="profiles" class="pop" data-stop="1"><div style="font-size:13px;color:#d0d0d0;font-weight:600;padding:6px 8px;">Profiles</div>`;
    for (const p of S.profiles) h += `<div class="item${p.on ? " on" : ""}" ${act("pickProfile", p.key)}><div class="mini">${esc(p.initial)}</div><div style="flex:1;min-width:0;"><div style="font-size:13px;font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${esc(p.key)}</div><div style="font-size:11px;color:#a8a8a8;">${esc(p.sub)}</div></div>${p.tracked ? `<div class="rm" ${act("removeProfile", p.key)} title="Stop tracking">✕</div>` : ""}</div>`;
    if (!S.profiles.length) h += `<div style="padding:8px;font-size:12px;color:#a8a8a8;">No profiles yet</div>`;
    h += `<div style="border-top:1px solid #424242;margin:6px 0;"></div><div class="new" ${act("newProfile")}>+ Track a player</div></div>`;
  }
  return h;
}

function seg(options, cur, a, small) {
  return `<div class="seg${small ? " sm" : ""}">` + options.map(o => `<div class="${o.id === cur ? "on" : ""}" ${act(a, o.id)}>${esc(o.label)}</div>`).join("") + "</div>";
}

// 1v1 / 2v2 / 3v3, and a "More" menu for other playlists that have readings (Casual, Hoops, ...).
function playlistSeg() {
  const main = S.pl.options.filter(o => !o.more), more = S.pl.options.filter(o => o.more);
  let h = `<div class="seg" style="position:relative;">` + main.map(o => `<div class="${o.id === S.pl.cur ? "on" : ""}" ${act("setPlaylist", o.id)}>${esc(o.label)}</div>`).join("");
  if (more.length) {
    const cur = more.find(o => o.id === S.pl.cur);
    h += `<div class="${cur ? "on" : ""}" ${act("toggleMore")} title="Other playlists">${esc(cur ? cur.label : "More")} <span style="font-size:9px;opacity:.7;">▼</span></div>`;
    if (U.moreOpen) {
      h += `<div id="more" class="pop" data-stop="1">` + more.map(o => `<div class="item${o.id === S.pl.cur ? " on" : ""}" ${act("setPlaylist", o.id)}>${esc(o.label)}</div>`).join("") + `</div>`;
    }
  }
  return h + "</div>";
}

function header() {
  const t = S.top, k = S.log;
  return `<div class="who"><div class="gem" style="border-color:${t.gem};"></div><div style="min-width:0;"><div class="name">${esc(S.profile ? S.profile.name : "No profile yet")}</div><div class="rank" style="color:${t.rankColor}">${esc(t.rank)}</div></div></div>
    <div class="vr"></div>
    <div class="bigmmr"><span class="v">${esc(t.mmr)}</span><span class="u">MMR</span><span class="t" style="color:${t.todayColor}">${esc(t.today)}</span></div>
    <div class="flex1"></div>
    ${playlistSeg()}
    <div class="pill" ${act("go", "Play")} title="${esc(k.full)}"><div class="dot" style="background:${k.color}"></div><span style="font-weight:600;">MMR log</span><span style="color:#a8a8a8;">${esc(k.short)}</span></div>`;
}

// --- MMR ---------------------------------------------------------------------------------------------

function mmrPage() {
  const m = S.mmr, g = m.gauge;
  let h = `<div class="col">
    <div class="row" style="gap:20px;">
      <h1>MMR · ${esc(S.pl.title)}</h1><div class="flex1"></div>
      <div class="segl"><span>Range</span>${seg(m.ranges, m.range, "setRange", true)}</div>
      <div class="segl"><span>Per</span>${seg(m.grans, m.gran, "setGran", true)}</div>
    </div>
    <div style="display:grid;grid-template-columns:300px repeat(3,minmax(0,1fr));gap:12px;">
      <div class="card gauge">
        <div style="position:relative;width:120px;height:72px;flex:none;">
          <svg width="120" height="72" viewBox="0 0 120 72"><path d="M 10 64 A 50 50 0 0 1 110 64" fill="none" stroke="#3a3a3a" stroke-width="9" stroke-linecap="round"></path>${g.prog > 0 ? `<path d="M 10 64 A 50 50 0 0 1 110 64" fill="none" stroke="#b78cff" stroke-width="9" stroke-linecap="round" stroke-dasharray="${(g.prog * 157).toFixed(1)} 400"></path>` : ""}</svg>
          <div style="position:absolute;left:0;right:0;bottom:2px;text-align:center;font:600 20px var(--fd);">${esc(g.pct)}</div>
        </div>
        <div style="display:flex;flex-direction:column;gap:3px;min-width:0;">
          <div class="lbl">Rank</div>
          <div style="font:600 18px/1.1 var(--ft);color:#d5c6ff;">${esc(g.tier)}</div>
          <div style="font-size:13px;color:#d6d6d6;">${esc(g.div)}</div>
          <div style="font:500 11px var(--ft);color:#a8a8a8;">${esc(g.toNext)}</div>
        </div>
      </div>`;
  for (const c of m.cards) h += `<div class="card stat"><div class="lbl">${esc(c.label)}</div><div class="big" style="color:${c.color}">${esc(c.value)}</div><div class="sub" title="${esc(c.sub)}">${esc(c.sub)}</div></div>`;
  h += `</div>
    <div class="card" style="padding:16px 16px 10px;">
      <div class="row" style="justify-content:space-between;margin-bottom:12px;"><div class="lbl">MMR over time</div><div style="font:500 11px var(--ft);color:#7d7d7d;">${esc(m.count)}</div></div>
      <div class="chartbox" id="chart">${chartBody()}</div>
      <div class="ticks">${ticks()}</div>
    </div>
  </div>`;
  return h;
}

function chartGeom() {
  const c = S.mmr.chart, n = c.points.length;
  return { c, n, Y: v => (c.hi - v) / (c.hi - c.lo) * 100, X: i => n < 2 ? 500 : i / (n - 1) * 1000 };
}

function chartBody() {
  const { c, n, X, Y } = chartGeom();
  if (!n) return `<div class="chartmsg">${esc(S.mmr.empty)}</div>`;
  let h = "";
  for (const b of c.bands) h += `<div class="band" style="top:${Y(b.hi)}%;height:${(b.hi - b.lo) / (c.hi - c.lo) * 100}%;background:${b.alt ? "rgba(185,162,255,0.045)" : "rgba(122,167,217,0.035)"};"><div style="color:${b.color}">${esc(b.label)}</div></div>`;
  for (const d of c.divs) h += `<div class="divl" style="top:${Y(d.v)}%;"><div>${esc(d.label)}</div></div>`;
  const line = c.points.map((p, i) => (i ? "L" : "M") + X(i).toFixed(1) + " " + Y(p.v).toFixed(2)).join(" ");
  const single = n === 1 ? `<circle cx="500" cy="${Y(c.points[0].v).toFixed(2)}" r="0"/>` : "";
  h += `<svg viewBox="0 0 1000 100" preserveAspectRatio="none" style="position:absolute;inset:0;width:100%;height:100%;">
    <path d="${line} L${X(n - 1).toFixed(1)} 100 L${X(0).toFixed(1)} 100 Z" fill="rgba(182,92,242,0.10)"></path>
    <path d="${line}" fill="none" stroke="#B65CF2" stroke-width="2" vector-effect="non-scaling-stroke" stroke-linejoin="round"></path>${single}</svg>`;
  if (n === 1) h += `<div style="position:absolute;left:50%;top:${Y(c.points[0].v)}%;width:9px;height:9px;margin:-4.5px 0 0 -4.5px;border-radius:50%;background:#B65CF2;"></div>`;
  h += `<div id="tip"></div><div id="hit" style="position:absolute;inset:0;cursor:crosshair;"></div>`;
  return h;
}

function ticks() {
  const { c, n, X } = chartGeom();
  const tn = Math.min(6, n);
  let h = "";
  for (let k = 0; k < tn; k++) {
    const i = Math.round(k / Math.max(1, tn - 1) * (n - 1));
    const shift = tn === 1 ? "translateX(-50%)" : k === 0 ? "none" : k === tn - 1 ? "translateX(-100%)" : "translateX(-50%)";
    h += `<div style="left:${X(i) / 10}%;transform:${shift};">${esc(c.points[i].date)}</div>`;
  }
  return h;
}

function drawTip() {
  const el = document.getElementById("tip");
  if (!el) return;
  const { c, n, X, Y } = chartGeom();
  const i = U.hover;
  if (i == null || i >= n) { el.innerHTML = ""; return; }
  const p = c.points[i], x = X(i) / 10;
  el.innerHTML = `<div style="position:absolute;top:0;bottom:0;left:${x}%;border-left:1px solid rgba(233,236,239,.25);"></div>
    <div style="position:absolute;left:${x}%;top:${Y(p.v)}%;width:11px;height:11px;margin:-5.5px 0 0 -5.5px;border-radius:50%;background:#202020;border:2px solid #B65CF2;"></div>
    <div class="tipbox" style="left:${x}%;transform:${x > 75 ? "translateX(calc(-100% - 12px))" : "translateX(12px)"};" title="${esc(p.time)}">
      <div style="font-size:11px;color:#a8a8a8;">${esc(p.when)}</div>
      <div style="display:flex;align-items:baseline;gap:8px;"><span style="font:600 20px var(--fd);">${esc(p.v)}</span><span style="font:500 12px var(--ft);color:${p.dColor};">${esc(p.d)}</span></div>
      <div style="font-size:11px;color:#b9a2ff;">${esc(p.rank)}</div>
    </div>`;
}

// --- Games -------------------------------------------------------------------------------------------

function gamesPage() {
  const G = S.games, a = S.api;
  let h = `<div class="col">
    <div class="row" style="gap:16px;">
      <h1>Games</h1>${seg(G.kinds, G.kind, "setGamesKind", true)}${G.options.length > 2 ? seg(G.options, G.pl, "setGamesPlaylist", true) : ""}<div class="flex1"></div>
      <div class="apipill" style="background:${a.bg};border:1px solid ${a.border};" title="${esc(a.full)}"><div class="dot" style="background:${a.color}"></div><span style="font-weight:600;color:${a.color};white-space:nowrap;">Stats API · ${esc(a.label)}</span><span class="d">${esc(a.detail)}</span></div>
    </div>
    <div style="display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px;">
      <div class="card stat"><div class="lbl">Record</div>
        <div style="display:flex;align-items:baseline;gap:10px;"><span class="big" style="overflow:visible;">${esc(G.rec.wl)}</span><span style="font:500 13px var(--ft);color:${G.rec.pctColor};">${esc(G.rec.pct)}</span></div>
        <div style="display:flex;align-items:center;gap:8px;font-size:12px;color:#a8a8a8;white-space:nowrap;"><span>Streak</span><span style="font:600 12px var(--ft);color:${G.rec.streakColor};">${esc(G.rec.streak)}</span><span>·</span><span>${esc(G.rec.mmr)}</span></div></div>
      <div class="card stat" title="${esc(G.style.title)}"><div class="lbl">Your style</div><div class="big" style="color:#d5c6ff;">${esc(G.style.top)}</div><div style="font:500 12px var(--ft);color:#a8a8a8;">${esc(G.style.per)}</div></div>
      <div class="card stat"><div class="lbl">Teammates</div>
        <div style="display:flex;align-items:baseline;gap:10px;min-width:0;"><span class="big">${esc(G.mates.best)}</span><span style="font:500 13px var(--ft);color:${G.mates.bestColor};">${esc(G.mates.bestRec)}</span></div>
        <div class="sub">${esc(G.mates.sub)}</div></div>
    </div>
    <div style="display:grid;grid-template-columns:360px minmax(0,1fr);gap:12px;align-items:start;">
      <div class="card" style="overflow:hidden;">
        <div style="display:flex;justify-content:space-between;padding:12px 14px;border-bottom:1px solid #333333;"><span class="lbl">Recent games</span><span style="font-size:11px;color:#7d7d7d;">Right-click to delete</span></div>
        <div class="glist" data-scroll="glist">`;
  if (!G.list.length) h += `<div class="empty" style="padding:14px;">No games saved yet. Games appear here when a match ends while the app runs.</div>`;
  for (const g of G.list) {
    h += `<div class="grow${g.id === G.sel ? " on" : ""}" ${act("selectGame", g.id)} data-ctx="${attr({ id: g.id, title: g.ctxTitle })}">
      <div class="res ${g.win ? "w" : "l"}">${g.res}</div>
      <div style="font:600 11px var(--ft);color:#a8a8a8;white-space:nowrap;overflow:hidden;" title="${esc(g.plTitle)}">${esc(g.pl)}</div>
      <div style="min-width:0;"><div style="font:600 14px var(--fd);letter-spacing:.02em;white-space:nowrap;">${esc(g.score)}<span style="font:500 11px var(--ft);color:#fce100;margin-left:6px;">${esc(g.ot)}</span></div><div style="font-size:11px;color:#7d7d7d;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">${esc(g.with)}</div></div>
      <div style="font-size:11px;color:#a8a8a8;text-align:right;white-space:nowrap;">${esc(g.when)}</div>
      <div style="font:500 12px var(--ft);text-align:right;color:${g.dColor};">${esc(g.delta)}</div>
    </div>`;
  }
  h += `</div></div>`;
  const d = G.detail;
  h += `<div class="card" style="overflow:hidden;">`;
  if (d) {
    h += `<div style="display:flex;align-items:center;gap:12px;padding:12px 16px;border-bottom:1px solid #333333;min-width:0;">
        <div class="res ${d.win ? "w" : "l"}" style="width:auto;height:auto;padding:3px 8px;font-size:12px;">${d.result}</div>
        <div style="font:600 20px var(--fd);white-space:nowrap;">${esc(d.score)}</div>
        <div style="font-size:12px;color:#a8a8a8;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;min-width:0;" title="${esc(d.meta)}">${esc(d.meta)}</div>
        <div class="flex1"></div>
        <div style="font:500 13px var(--ft);color:${d.dColor};white-space:nowrap;">${esc(d.delta)}</div>
      </div>
      <div class="ptable head"><div>Player</div><div>Score</div><div>Goals</div><div>Assists</div><div>Saves</div><div>Shots</div><div>MVP</div></div>`;
    for (const grp of d.groups) {
      h += `<div style="padding:8px 16px 4px;font-size:11px;font-weight:600;color:${grp.color};border-top:1px solid #2d2d2d;">${esc(grp.label)}</div>`;
      for (const r of grp.rows) h += `<div class="ptable" style="background:${r.you ? "rgba(182,92,242,.06)" : "transparent"};"><div class="pn">${esc(r.name)}</div><div>${esc(r.score)}</div><div>${esc(r.g)}</div><div>${esc(r.a)}</div><div>${esc(r.sv)}</div><div>${esc(r.sh)}</div><div style="color:#fce100;font:700 11px var(--ft);">${esc(r.mvp)}</div></div>`;
    }
    h += `<div style="height:8px;"></div>`;
  } else {
    h += `<div class="empty" style="padding:16px;">Pick a game to see everyone's stats.</div>`;
  }
  h += `</div></div>`;
  const y = G.hyp;
  h += `<div class="card" style="padding:16px;">
      <div style="display:flex;align-items:baseline;gap:10px;margin-bottom:16px;"><div style="font:600 20px var(--fd);">How you play</div><div style="font-size:12px;color:#a8a8a8;">${esc(y.basis)}</div></div>`;
  if (!G.count) {
    h += `<div class="empty" style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;">Games come from Rocket League's own Stats API, which must be on (<b>PacketSendRate</b> above 0 in TAStatsAPI.ini).<div class="btn" ${act("setupOpen", "stats")}>Turn it on</div></div></div></div>`;
    return h;
  }
  h += `<div style="display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:28px;">
        <div style="display:flex;flex-direction:column;gap:12px;">
          <div class="lbl">Per game</div>
          ${y.shares.map(s => `<div style="display:flex;align-items:center;gap:10px;font-size:13px;"><div style="width:8px;height:8px;border-radius:2px;background:${s.color};"></div><span style="font:600 14px var(--ft);min-width:34px;">${esc(s.value)}</span><span style="color:#d6d6d6;">${esc(s.label)}</span></div>`).join("")}
        </div>
        <div style="display:flex;flex-direction:column;gap:8px;">
          <div class="lbl" style="margin-bottom:4px;">Teammates</div>
          <div class="mtable" style="font-size:11px;color:#7d7d7d;font-weight:600;"><div>Name</div><div>GP</div><div>W–L</div><div>Win</div></div>
          ${y.mates.map(m => `<div class="mtable" style="font:500 12px var(--ft);align-items:center;padding:4px 0;border-top:1px solid #2d2d2d;"><div style="font:600 13px var(--ft);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">${esc(m.name)}</div><div style="color:#a8a8a8;">${esc(m.gp)}</div><div>${esc(m.wl)}</div><div style="color:${m.color};">${esc(m.pct)}</div></div>`).join("")}
          ${y.mates.length ? "" : `<div class="empty" style="font-size:12px;">No teammates in these games.</div>`}
        </div>
        <div style="display:flex;flex-direction:column;gap:10px;">
          <div class="lbl">Win / loss patterns</div>
          ${y.patterns.map(p => `<div style="display:flex;flex-direction:column;gap:4px;"><div style="display:flex;font-size:12px;"><span style="flex:1;">${esc(p.label)}</span><span style="font:500 11px var(--ft);color:#7d7d7d;margin-right:8px;">${esc(p.n)}</span><span style="font:600 12px var(--ft);color:${p.color};">${esc(p.pct)}</span></div><div style="height:4px;background:#3a3a3a;border-radius:2px;overflow:hidden;"><div style="height:100%;width:${p.w};background:${p.color};"></div></div></div>`).join("")}
          ${y.standout.length ? `<div class="lbl" style="margin-top:6px;">Stand-out differences</div>` + y.standout.map(p => `<div style="display:flex;gap:8px;font-size:12px;line-height:1.45;"><div style="width:7px;height:7px;border-radius:50%;background:${p.color};margin-top:5px;flex:none;"></div><div>${esc(p.text)} <span style="color:#7d7d7d;">${esc(p.n)}</span></div></div>`).join("") : ""}
          ${!y.standout.length && y.note ? `<div style="font-size:11px;color:#7d7d7d;line-height:1.45;">${esc(y.note)}</div>` : ""}
        </div>
      </div></div></div>`;
  return h;
}

// --- Play --------------------------------------------------------------------------------------------

function playPage() {
  const P = S.play, k = S.log;
  let h = `<div style="display:grid;grid-template-columns:minmax(0,1.15fr) minmax(0,1fr);gap:16px;align-items:start;">
    <div class="col">
      <div class="card" style="padding:24px;display:flex;flex-direction:column;gap:18px;">
        <div style="display:flex;align-items:center;gap:20px;">
          <div class="playbtn" ${act("play")} style="background:${P.btn.bg};color:${P.btn.color};"><div class="tri"></div>${esc(P.btn.label)}</div>
          <div style="display:flex;flex-direction:column;gap:4px;min-width:0;">
            <div style="display:flex;align-items:center;gap:8px;font-size:14px;font-weight:600;"><div style="width:8px;height:8px;border-radius:50%;background:${P.status.color};flex:none;"></div>${esc(P.status.title)}</div>
            <div style="font-size:13px;color:#a8a8a8;user-select:text;">${esc(P.status.detail)}</div>
          </div>
        </div>
      </div>
      <div class="card" style="padding:16px;display:flex;flex-direction:column;gap:12px;">
        <div class="row" style="justify-content:space-between;"><div class="lbl">Account</div><div style="font-size:11px;color:#7d7d7d;">${esc(P.refreshNote)}</div></div>
        <div style="display:flex;flex-direction:column;gap:6px;">`;
  for (const a of P.accounts) h += `<div class="acct${a.on ? " on" : ""}" ${act("pickAccount", a.id)}><div class="radio"><div></div></div><div style="flex:1;min-width:0;"><div style="font-size:14px;font-weight:600;">${esc(a.name)}</div><div style="font-size:12px;color:#a8a8a8;">${esc(a.note)}</div></div><div class="tag">${esc(a.platform)}</div></div>`;
  if (!P.accounts.length) h += `<div class="empty">${esc(P.noAccounts)}</div>`;
  h += `</div>
        <div style="display:flex;gap:8px;flex-wrap:wrap;">
          <div class="btn${P.busy ? " dis" : ""}" ${act("refreshAccounts")} title="Read the accounts saved in the account switcher again">Refresh</div>
          <div class="btn${P.busy ? " dis" : ""}" ${act("addAccount")} title="Restart the Epic launcher on its sign-in screen to sign into another account">Add account</div>
          <div class="btn${P.busy ? " dis" : ""}" ${act("addCurrent")} title="Save the account the Epic launcher is signed into now">Add current account</div>
        </div>
      </div>
    </div>
    <div class="col">
      <div class="card" style="padding:16px;display:flex;flex-direction:column;gap:12px;">
        <div class="lbl">MMR</div>
        <div style="display:flex;align-items:center;gap:10px;"><div style="width:8px;height:8px;border-radius:50%;background:${k.color};flex:none;"></div><div style="font-size:14px;font-weight:600;">${esc(k.title)}</div></div>
        <div style="font-size:13px;color:#a8a8a8;margin-top:-6px;user-select:text;">${esc(k.detail)}</div>
        <div style="font-size:11px;color:#7d7d7d;line-height:1.5;">${esc(k.note)}</div>
        <div style="display:flex;gap:8px;">
          <div class="btn" ${act("importOpen")} title="One-time import of your past MMR from tracker.gg, for the account picked on the left">Import history</div>
        </div>
      </div>
      <div class="card" style="padding:16px;display:flex;flex-direction:column;gap:12px;">
        <div class="row" style="justify-content:space-between;"><div class="lbl">Vibrance</div><div id="vibval" style="font:600 14px var(--ft);">${P.vib}%</div></div>
        <input id="vib" type="range" min="50" max="100" value="${P.vib}" style="width:100%;" title="NVIDIA Digital Vibrance while Rocket League is in front; 50% is the driver default">
        <div style="display:flex;justify-content:space-between;font:500 10px var(--ft);color:#7d7d7d;margin-top:-6px;"><span>50 default</span><span>75</span><span>100</span></div>
        <div class="check${P.obs ? " on" : ""}" ${act("toggleObs")}><div class="box">${P.obs ? "✓" : ""}</div><span>OBS</span><span style="color:#a8a8a8;">Show it in recordings too</span></div>
        <div style="font-size:11px;color:#7d7d7d;line-height:1.5;">${P.vibState.map(esc).join("<br>")}</div>
      </div>
    </div>
  </div>`;
  return h;
}

// --- Settings ----------------------------------------------------------------------------------------

function settingsPage() {
  const T = S.settings;
  let h = `<div style="max-width:760px;display:flex;flex-direction:column;gap:16px;">
    <h1>Settings</h1>
    <div class="card" style="padding:16px;display:flex;flex-direction:column;gap:12px;">
      <div class="lbl">Profile</div>
      <div style="display:grid;grid-template-columns:160px minmax(0,1fr);gap:10px 16px;align-items:center;font-size:13px;">`;
  T.profile.forEach(([label, value], i) => {
    h += `<div style="color:#a8a8a8;">${esc(label)}</div>` + (i === 0
      ? `<div class="field" style="font-size:13px;color:var(--text);">${esc(value)}</div>`
      : `<div style="font:500 13px var(--ft);user-select:text;overflow:hidden;text-overflow:ellipsis;">${esc(value)}</div>`);
  });
  h += `</div><div style="font-size:11px;color:#7d7d7d;">${esc(T.profileNote)}</div></div>
    <div class="card" style="padding:6px 16px;">`;
  T.toggles.forEach((t, i) => {
    h += `<div style="display:flex;align-items:center;gap:16px;padding:12px 0;${i < T.toggles.length - 1 ? "border-bottom:1px solid #2d2d2d;" : ""}"><div style="flex:1;"><div style="font-size:14px;font-weight:600;">${esc(t.label)}</div><div style="font-size:12px;color:#a8a8a8;">${esc(t.sub)}</div></div><div class="toggle${t.on ? " on" : ""}" ${act("toggle", t.key)}><div></div></div></div>`;
  });
  const R = T.rl;
  h += `</div>
    <div class="card" style="padding:16px;display:flex;flex-direction:column;gap:12px;">
      <div class="lbl">Rocket League</div>
      <div style="font-size:13px;">${esc(R.launch)}</div>
      <div class="row" style="gap:8px;flex-wrap:wrap;"><div class="btn" ${act("findGame")}>Find automatically</div><div class="btn" ${act("chooseGame")}>Choose the game or a shortcut…</div></div>
      <div style="display:flex;align-items:center;gap:8px;font-size:13px;"><div class="dot" style="background:${R.apiColor}"></div><span style="font-weight:600;">Stats API</span><span style="color:#a8a8a8;" title="${esc(R.apiPath)}">${esc(R.api)}</span></div>
      <div class="row" style="gap:8px;flex-wrap:wrap;">${R.apiOn ? "" : `<div class="btn${R.gameRunning ? " dis" : ""}" ${act("enableStats")} title="${R.gameRunning ? "Close Rocket League first" : "Sets PacketSendRate=10 in TAStatsAPI.ini"}">Enable Stats API</div>`}<div class="btn" ${act("setupOpen", "profile")}>Run setup again</div></div>
    </div>
    <div class="card" style="padding:16px;display:flex;flex-direction:column;gap:12px;">
      <div class="lbl">Account switcher (optional)</div>`;
  for (const p of T.paths) h += `<div style="display:grid;grid-template-columns:160px minmax(0,1fr) auto;gap:10px 16px;align-items:center;font-size:13px;"><div style="color:#a8a8a8;">${esc(p.label)}</div><input class="field" id="path-${p.key}" data-path="${p.key}" value="${esc(p.value)}" placeholder="${esc(p.placeholder)}" spellcheck="false" title="${esc(p.value || p.placeholder)}"><div class="btn" ${act("browse", p.key)}>Browse</div></div>`;
  h += `</div>
    <div class="card" style="padding:16px;display:flex;align-items:center;gap:8px;">
      <div style="flex:1;min-width:0;"><div class="lbl" style="margin-bottom:4px;">Data</div><div style="font-size:13px;color:#d6d6d6;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;" title="${esc(T.dataDir)}">${esc(T.data)}</div></div>
      <div class="btn" ${act("import")} title="Import past MMR from tracker.gg for the account picked on the Play page">Import</div>
      <div class="btn" ${act("export")} title="Export the MMR history as CSV or JSON">Export</div>
      <div class="btn" ${act("openFolder")}>Open folder</div>
    </div>
    <div style="font-size:11px;color:#7d7d7d;line-height:1.5;">${esc(S.app.title)} ${esc(S.app.version)} · Not affiliated with Psyonix, Epic Games or Tracker Network. Rocket League is a trademark of Psyonix LLC. Reads only the game's own log files and its local Stats API; the game itself is never touched.</div>
  </div>`;
  return h;
}

// --- Import history (one-time paste from tracker.gg) ------------------------------------------------
// The panel's frame and paste box are built once when it opens; only #impdyn and #impbtns are repainted, so
// the pasted text survives the state updates that arrive while it's open.

function importer() {
  const I = S.imp, root = document.getElementById("importer");
  if (!I) {
    if (root.innerHTML) { root.innerHTML = ""; delete last.impdyn; delete last.impbtns; }
    return;
  }
  let box = document.getElementById("imptext");
  if (!box) {
    root.innerHTML = `<div id="impbg" class="wizbg"><div id="imp" class="pop wiz" data-stop="1"><div id="impdyn"></div>
      <textarea id="imptext" spellcheck="false" placeholder='Paste the tracker.gg page here: text that starts with {"data"'></textarea>
      <div id="impbtns" class="ibtns"></div></div></div>`;
    delete last.impdyn; delete last.impbtns;
    box = document.getElementById("imptext");
    U.impClear = I.clear;
    setTimeout(() => box.focus(), 0);
  }
  if (I.clear !== U.impClear) { U.impClear = I.clear; box.value = ""; }
  box.style.display = I.step === "paste" ? "" : "none";
  paint("impdyn", importBody(I));
  paint("impbtns", importButtons(I));
}

function importBody(I) {
  const link = (url, text) => `<a href="${esc(url)}" title="Opens in your browser">${esc(text)}</a>`;
  let h = `<div class="mt">Import MMR history · ${esc(I.profile)}</div>`;
  if (I.step === "paste") {
    h += `<div class="steps">
      <div class="step"><div class="num${I.trackerId ? " ok" : ""}">${I.trackerId ? "✓" : "1"}</div><div>${I.trackerId
        ? `tracker.gg profile confirmed (id ${esc(I.trackerId)}). <span class="dim">To check again: ${link(I.profileUrl, "profile of " + I.profileName)}.</span>`
        : `Open the ${link(I.profileUrl, "tracker.gg profile of " + I.profileName)} and paste that page below. It confirms the player and unlocks step 2.`}</div></div>
      <div class="step"><div class="num">2</div><div>${I.historyUrl
        ? `Open the ${link(I.historyUrl, "tracker.gg MMR history")} and paste that page below.`
        : `<span class="dim">The MMR history link appears here after step 1.</span>`}</div></div>
      <div class="step"><div class="num" style="background:#373737;color:#a8a8a8;">?</div><div class="dim">Each link opens in your browser. If tracker.gg shows a security check, finish it. When the page shows text starting with <b>{"data"</b>, press <b>Ctrl+A</b>, <b>Ctrl+C</b> (in Firefox, click <b>Raw Data</b> first), then click in the box below and press <b>Ctrl+V</b>.</div></div>
    </div>`;
    if (I.info) h += `<div class="note info">${esc(I.info)}</div>`;
    if (I.error) h += `<div class="note err">${esc(I.error)}</div>`;
  } else if (I.step === "preview") {
    const p = I.preview;
    h += `<div class="note ${p.identity.startsWith("Matches") ? "info" : "warn"}">${esc(p.identity)}</div>
      <div class="pv">
        <div class="card"><div class="lbl">New readings</div><div class="big" style="color:${p.new ? "var(--win)" : "var(--muted)"}">${p.new}</div><div class="sub">of ${p.total} in the paste</div></div>
        <div class="card"><div class="lbl">Already stored</div><div class="big">${p.stored}</div><div class="sub">skipped</div></div>
        <div class="card"><div class="lbl">Differ from stored</div><div class="big" style="color:${p.conflicts ? "var(--warn)" : "inherit"}">${p.conflicts}</div><div class="sub">stored ones kept</div></div>
        <div class="card"><div class="lbl">Dates</div><div style="font:600 14px var(--ft);margin-top:6px;">${esc(p.range)}</div><div class="sub">${p.malformed ? p.malformed + " unreadable, skipped" : "all readable"}</div></div>
      </div>
      <div class="pls"><div class="h">Playlist</div><div class="h">In paste</div><div class="h">New</div>
        ${p.playlists.map(r => `<div style="color:${r.ranked ? "var(--text)" : "#a8a8a8"}">${esc(r.name)}</div><div>${r.n}</div><div style="color:${r.new ? "var(--win)" : "#7d7d7d"}">${r.new}</div>`).join("")}</div>`;
    if (p.examples.length) h += `<div class="dim" style="font-size:12px;line-height:1.5;">Differences, e.g.: ${p.examples.map(esc).join(" · ")}. tracker.gg can rewrite a day after it was saved here; what's stored stays.</div>`;
    if (!p.new) h += `<div class="note info">Nothing new to import: everything in it is already stored.</div>`;
  } else {
    h += `<div class="note info">${esc(I.result)}</div>
      <div class="dim" style="font-size:13px;line-height:1.5;">From now on, new MMR comes from Rocket League's log each time you queue ranked. There's no need to import again.</div>`;
  }
  return h;
}

function importButtons(I) {
  if (I.step === "paste") return `<div class="btn" ${act("importCancel")}>Cancel</div><div class="btn primary" ${act("impCheck")}>Import</div>`;
  if (I.step === "preview") return `<div class="btn" ${act("importBack")}>Back</div><div class="btn" ${act("importCancel")}>Cancel</div>`
    + (I.preview.new ? `<div class="btn primary" ${act("importConfirm")}>Import ${I.preview.new} reading${I.preview.new === 1 ? "" : "s"}</div>` : "");
  return `<div class="btn primary" ${act("importCancel")}>Close</div>`;
}

// --- Setup (first start; Settings > Run setup again) -------------------------------------------------
// Steps: your Epic name, how Play starts the game, Rocket League's Stats API. The typed name is kept in
// U.setupName so a state update while typing doesn't lose it.

function setupPanel() {
  const T = S.setup;
  if (!T) { U.setupName = undefined; return ""; }
  if (U.setupSeq !== T.seq) { U.setupSeq = T.seq; U.setupName = undefined; }
  const idx = T.steps.indexOf(T.step);
  let h = `<div class="wizbg"><div class="pop wiz" data-stop="1" style="width:640px;">
    <div class="tabs">${T.steps.map((s, i) => `<div class="${i <= idx || T.step === "done" ? "on" : ""}"></div>`).join("")}</div>`;
  const err = T.error ? `<div class="note err">${esc(T.error)}</div>` : "";
  if (T.step === "profile") {
    const name = U.setupName !== undefined ? U.setupName : T.name;
    h += `<div class="mt">Welcome to ${esc(S.app.title)}</div>
      <div class="lead">Three quick things and you're set. Everything here can be changed later in Settings.</div>
      <div class="lbl">Your Epic name</div>
      <input class="field" id="setupname" value="${esc(name)}" placeholder="The name other players see in Rocket League" spellcheck="false" maxlength="64">
      ${T.detected.length ? `<div class="sub">Found on this PC:</div><div class="chips">${T.detected.map(n => `<div class="chip" ${act("setupChip", n)}>${esc(n)}</div>`).join("")}</div>` : ""}
      <div class="sub" style="line-height:1.5;">Your MMR and games are saved under this name.</div>
      <div class="sub" style="line-height:1.5;"><b>Several Epic accounts?</b> The Play page can switch between them for you (optional). It saves the Epic launcher's "Remember me" sign-in for each account you add, encrypted so only your Windows user on this PC can read it, and swaps it in before starting the game. Nothing is sent anywhere, and your password is never seen.</div>
      ${err}
      <div class="ibtns"><div class="btn" ${act("setupFinish")}>Skip setup</div><div class="btn primary" ${act("setupName")}>Next</div></div>`;
  } else if (T.step === "game") {
    h += `<div class="mt">How should Play start Rocket League?</div>
      <div class="lead">Right now: through <b>${esc(T.launch)}</b>.</div>
      <div class="row2"><div class="btn" ${act("findGame")}>Find automatically</div><div class="btn" ${act("chooseGame")}>Choose the game or a shortcut…</div></div>`;
    if (T.found) for (const f of T.found)
      h += `<div class="opt${f.on ? " on" : ""}" ${act("setLaunch", f.method)}><div class="radio"><div></div></div><div style="flex:1;min-width:0;"><div style="font-weight:600;">${esc(f.label)}</div><div class="sub" style="white-space:nowrap;overflow:hidden;text-overflow:ellipsis;" title="${esc(f.dir)}">${esc(f.dir)}</div></div></div>`;
    if (T.method === "path" && T.shortcut)
      h += `<div class="opt on"><div class="radio"><div></div></div><div style="flex:1;min-width:0;"><div style="font-weight:600;">Your choice</div><div class="sub" style="white-space:nowrap;overflow:hidden;text-overflow:ellipsis;" title="${esc(T.shortcut)}">${esc(T.shortcut)}</div></div></div>`;
    h += `<div class="sub" style="line-height:1.5;">An Epic install starts through the Epic Games launcher, which signs the game in. A shortcut can be anything that starts the game, for example a Steam shortcut you made for it.</div>
      ${err}
      <div class="ibtns"><div class="btn" ${act("setupGo", "profile")}>Back</div><div class="btn primary" ${act("setupGo", "stats")}>Next</div></div>`;
  } else if (T.step === "stats") {
    h += `<div class="mt">Turn on Rocket League's Stats API?</div>
      <div class="lead">The Stats API is the game's official data feed for programs on this PC. A lot of this app needs it:
        the <b>Games</b> page (every match with everyone's stats), your playstyle, teammates, win/loss patterns and the MMR change per game.
        It only sends data to programs on this computer, and nothing about the game changes.</div>`;
    if (T.apiOn) h += `<div class="note info">${esc(T.statsMsg || "It's already on.")}</div>`;
    else {
      h += `<div class="note warn">It's off. Enabling it sets PacketSendRate=10 in the game folder's settings file (as Psyonix describes) and in your Documents copy, keeping a backup of each: <span style="word-break:break-all;">${esc(T.apiInstallPath || "")}${T.apiInstallPath ? " and " : ""}${esc(T.apiPath)}</span>.</div>`;
      if (T.gameRunning) h += `<div class="note err">Close Rocket League first: it reads this setting when it starts.</div>`;
      if (T.statsMsg) h += `<div class="note ${T.statsOk ? "info" : "err"}">${esc(T.statsMsg)}</div>`;
    }
    h += `<div class="ibtns"><div class="btn" ${act("setupGo", "game")}>Back</div>` + (T.apiOn
      ? `<div class="btn primary" ${act("setupGo", "done")}>Next</div>`
      : `<div class="btn" ${act("setupGo", "done")}>Not now</div><div class="btn primary${T.gameRunning ? " dis" : ""}" ${act("enableStats")}>Enable Stats API</div>`) + `</div>`;
  } else {
    h += `<div class="mt">You're set</div>
      <div class="steps">
        <div class="step"><div class="num ok">✓</div><div>Profile: <b>${esc(T.profile || T.name)}</b></div></div>
        <div class="step"><div class="num ok">✓</div><div>Play starts Rocket League through <b>${esc(T.launch)}</b></div></div>
        <div class="step"><div class="num${T.apiOn ? " ok" : ""}">${T.apiOn ? "✓" : "!"}</div><div>Stats API: ${T.apiOn ? "on" : "off. Turn it on any time in Settings > Rocket League"}</div></div>
        <div class="step"><div class="num" style="background:#373737;color:#a8a8a8;">i</div><div class="dim">Your MMR is read from the game's log each time you queue ranked. Past MMR can be imported once from tracker.gg: Play page > Import history.</div></div>
      </div>
      <div class="ibtns"><div class="btn primary" ${act("setupFinish")}>Finish</div></div>`;
  }
  return h + `</div></div>`;
}

// --- footer, menus, dialogs -------------------------------------------------------------------------

function footer() {
  const st = S.status;
  let h = `<div style="display:flex;align-items:center;gap:6px;" title="${esc(S.api.full)}"><div style="width:6px;height:6px;border-radius:50%;background:${S.api.color};"></div>Stats API ${esc(S.api.label)}</div>
    <div>${esc(st.session)}</div><div class="msg" title="${esc(st.msg)}">${esc(st.msg)}</div><div class="flex1"></div>
    <div>v${esc(S.app.version)}</div>
    <div class="traybtn" ${act("toTray")} title="Hide to the tray (the app keeps running; click the tray icon to open it again)"><div style="width:8px;height:8px;background:#B65CF2;transform:rotate(45deg);"></div>Tray</div>`;
  if (U.trayOpen) {
    h += `<div id="traymenu" class="pop" data-stop="1">`;
    for (const t of S.tray) {
      if (t.sep) { h += `<div class="sep"></div>`; continue; }
      h += `<div class="item" ${act("tray", t.a)}><span style="width:12px;color:#B65CF2;">${t.check ? "✓" : ""}</span><span style="flex:1;font-weight:${t.bold ? 600 : 500};">${esc(t.label)}</span><span style="font:500 11px var(--ft);color:#a8a8a8;">${esc(t.right || "")}</span></div>`;
    }
    h += `</div>`;
  }
  return h;
}

function overlay() {
  let h = "";
  if (U.ctx) h += `<div id="ctx" class="pop" data-stop="1" style="left:${U.ctx.x}px;top:${U.ctx.y}px;"><div style="padding:5px 10px 7px;font-size:11px;color:#a8a8a8;">${esc(U.ctx.title)}</div><div class="del" ${act("deleteGame", U.ctx.id)}>Delete game</div></div>`;
  const m = U.modal;
  if (m) {
    h += `<div id="modalbg"><div id="modal" class="pop" data-stop="1"><div class="mt">${esc(m.title)}</div><div class="mb">${esc(m.text)}</div>`;
    if (m.field) h += `<input class="field" id="modalfield" placeholder="${esc(m.field.placeholder || "")}" spellcheck="false" style="font-size:13px;">`;
    h += `<div class="mbtns">`;
    (m.buttons || [{ label: "OK" }]).forEach((b, i) => { h += `<div class="btn${b.primary ? " primary" : ""}${b.danger ? " danger" : ""}" ${act("modal", i)}>${esc(b.label)}</div>`; });
    h += `</div></div></div>`;
  }
  return h;
}

// ---------------------------------------------------------------------------------------------------
// Input

function closePopups() {
  if (U.ctx || U.trayOpen || U.profileOpen || U.moreOpen) { U.ctx = null; U.trayOpen = false; U.profileOpen = false; U.moreOpen = false; return true; }
  return false;
}

const LOCAL = {
  go: v => { U.screen = v; U.ctx = null; U.hover = null; call("screen", v); },
  toggleProfile: () => { const o = !U.profileOpen; closePopups(); U.profileOpen = o; },
  toggleTray: () => { const o = !U.trayOpen; closePopups(); U.trayOpen = o; },
  toggleMore: () => { const o = !U.moreOpen; closePopups(); U.moreOpen = o; },
  setPlaylist: v => { U.moreOpen = false; U.hover = null; call("setPlaylist", v); },
  pickProfile: v => { U.profileOpen = false; call("pickProfile", v); },
  newProfile: () => { U.profileOpen = false; call("newProfile"); },
  removeProfile: v => { U.profileOpen = false; call("removeProfile", v); },
  tray: v => { U.trayOpen = false; if (v === "vibrance") { U.screen = "Play"; call("screen", "Play"); } else call("tray", v); },
  deleteGame: v => { U.ctx = null; call("deleteGame", v); },
  impCheck: () => { const box = document.getElementById("imptext"); call("importCheck", { text: box ? box.value : "" }); },
  setupChip: v => { U.setupName = v; const f = document.getElementById("setupname"); if (f) f.value = v; },
  setupName: () => { const f = document.getElementById("setupname"); call("setupProfile", { name: f ? f.value : (U.setupName || "") }); },
  modal: i => {
    const m = U.modal, f = document.getElementById("modalfield");
    U.modal = null;
    const b = (m.buttons || [])[i];
    if (b && b.a) call(b.a, m.field ? { ...(b.v || {}), value: f ? f.value : "" } : b.v);
  },
};

document.addEventListener("click", e => {
  const t = e.target.closest("[data-a]");
  const inPop = e.target.closest("[data-stop]");
  if (!t) { if (!inPop && closePopups()) render(); return; }
  const a = t.dataset.a, v = t.dataset.v ? JSON.parse(t.dataset.v) : null;
  if (["minimize", "maximize", "close"].includes(a)) { call(a); return; }
  if (!inPop && !["toggleProfile", "toggleTray", "toggleMore"].includes(a)) closePopups();
  if (LOCAL[a]) LOCAL[a](v); else call(a, v);
  render();
});

document.addEventListener("contextmenu", e => {
  e.preventDefault();
  const r = e.target.closest("[data-ctx]");
  closePopups();
  if (r) {
    const c = JSON.parse(r.dataset.ctx);
    U.ctx = { id: c.id, title: c.title, x: Math.min(e.clientX, innerWidth - 190), y: Math.min(e.clientY, innerHeight - 90) };
    call("selectGame", c.id);
  }
  render();
});

// Title bar: drag moves the window (Windows runs the move, so the page never sees a dblclick there: a second
// press within 400 ms maximizes instead).
let lastDrag = { t: 0, x: 0, y: 0 };
document.addEventListener("mousedown", e => {
  if (e.button !== 0) return;
  const edge = e.target.closest("[data-edge]");
  if (edge) { e.preventDefault(); call("resize", edge.dataset.edge); return; }
  if (e.target.closest("[data-drag]") && !e.target.closest("[data-a]")) {
    e.preventDefault();
    const now = performance.now(), near = Math.abs(e.screenX - lastDrag.x) < 6 && Math.abs(e.screenY - lastDrag.y) < 6;
    if (now - lastDrag.t < 400 && near) { lastDrag.t = 0; call("maximize"); return; }
    lastDrag = { t: now, x: e.screenX, y: e.screenY };
    call("move");
    return;
  }
  pointerDown = true;
});
document.addEventListener("mouseup", () => { pointerDown = false; if (pending) { pending = false; render(); } });

document.addEventListener("mousemove", e => {
  if (e.target.id !== "hit") { if (U.hover != null) { U.hover = null; drawTip(); } return; }
  const n = S.mmr.chart.points.length, r = e.target.getBoundingClientRect();
  const i = Math.max(0, Math.min(n - 1, Math.round((e.clientX - r.left) / r.width * (n - 1))));
  if (i !== U.hover) { U.hover = i; drawTip(); }
});
document.addEventListener("mouseleave", () => { if (U.hover != null) { U.hover = null; drawTip(); } });

document.addEventListener("input", e => {
  if (e.target.id === "vib") document.getElementById("vibval").textContent = e.target.value + "%";
  if (e.target.id === "setupname") U.setupName = e.target.value;
});
document.addEventListener("change", e => {
  if (e.target.id === "vib") call("setVibrance", +e.target.value);
  if (e.target.dataset.path) call("setPath", { key: e.target.dataset.path, value: e.target.value });
});

document.addEventListener("keydown", e => {
  if (e.key === "Escape") {
    if (U.modal) { U.modal = null; render(); return; }
    if (closePopups()) { render(); return; }
    if (S && S.imp) call("importCancel");
    else if (S && S.setup) call("setupFinish");
    return;
  }
  if (e.key === "Enter" && e.target.id === "setupname") { LOCAL.setupName(); return; }
  if (e.key === "Enter" && e.target.dataset && e.target.dataset.path) { e.target.blur(); return; }
  if (e.key === "Enter" && e.target.id === "modalfield" && U.modal) {
    const i = (U.modal.buttons || []).findIndex(b => b.primary);
    if (i >= 0) { LOCAL.modal(i); render(); }
    return;
  }
  if (e.ctrlKey && !e.altKey && "1234".includes(e.key)) { e.preventDefault(); LOCAL.go(PAGES[+e.key - 1]); closePopups(); render(); }
  if (e.ctrlKey && ["r", "R", "F5"].includes(e.key) || e.key === "F5") e.preventDefault();
});

// ---------------------------------------------------------------------------------------------------
// Python -> page

function onEvent(ev) {
  if (ev.go) { U.screen = ev.go; closePopups(); }
  if (ev.modal) U.modal = ev.modal;
  render();
  const f = ev.modal && document.getElementById("modalfield");
  if (f) f.focus();
}

new QWebChannel(qt.webChannelTransport, ch => {
  bridge = ch.objects.bridge;
  bridge.state.connect(json => { S = JSON.parse(json); render(); });
  bridge.event.connect(json => onEvent(JSON.parse(json)));
  call("ready", U.screen);
});
