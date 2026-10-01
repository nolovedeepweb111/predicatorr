// Точка входа: общий стейт, маршрутизация по #/..., статус данных.

import { api, qs } from "./api.js";
import { clear, closeAllModals, fmt, h, nGames, store, toast } from "./ui.js";
import * as predict from "./predict.js";
import * as rosters from "./rosters.js";
import * as bets from "./bets.js";
import * as model from "./model.js";

const routes = { predict, rosters, bets, model };

export const state = {
  tournamentId: null,
  tournaments: [],
  heroes: [],
  heroById: new Map(),
  teams: null,
  status: null,
};

export async function loadTeams(force = false) {
  if (!force && state.teams && state.teams.tournament_id === state.tournamentId) return state.teams;
  state.teams = await api(`/api/teams${qs({ tournament_id: state.tournamentId })}`);
  return state.teams;
}

export function teamByKey(key) {
  return state.teams ? state.teams.teams.find((t) => t.key === key) || null : null;
}

export function navigate(hash) {
  if (location.hash === hash) render();
  else location.hash = hash;
}

function parseHash() {
  const raw = location.hash.replace(/^#\/?/, "");
  const [path, query] = raw.split("?");
  return { route: routes[path] ? path : "predict", params: new URLSearchParams(query || "") };
}

let renderToken = 0;
export async function render() {
  const { route, params } = parseHash();
  closeAllModals();
  document.querySelectorAll(".nav a").forEach((a) => a.classList.toggle("active", a.dataset.route === route));
  const view = clear(document.getElementById("view"));
  view.append(h("div", { class: "empty" }, h("span", { class: "spinner" }), " Загрузка…"));
  const token = ++renderToken;
  try {
    await routes[route].render(view, { params, isCurrent: () => token === renderToken });
  } catch (e) {
    if (token !== renderToken) return;
    clear(view).append(h("div", { class: "notice err" }, h("b", null, "Не удалось загрузить. "), e.message));
  }
}

// --- статус данных -----------------------------------------------------------

function syncProblems(sync) {
  if (!sync) return [];
  const out = [];
  if (sync.backup && sync.backup.error) out.push(`бэкап: ${sync.backup.error}`);
  for (const m of sync.mixer || []) if (m.error) out.push(`mixer-cup (${m.series}): ${m.error}`);
  return out;
}

function paintStatus() {
  const btn = document.getElementById("sync");
  const label = document.getElementById("sync-label");
  const st = state.status;
  btn.classList.remove("ok", "warn", "err", "busy");
  if (!st) return;
  if (st.sync_running) btn.classList.add("busy");
  const sync = st.sync;
  const problems = syncProblems(sync);
  const backupFailed = sync && sync.backup && sync.backup.error;
  btn.classList.add(!st.model || !st.model.ready || backupFailed ? "err" : problems.length ? "warn" : "ok");
  label.textContent = st.sync_running ? "обновляю…" : sync ? `данные ${fmt.ago(sync.finished_at)}` : "данные не обновлялись";
  btn.title = ["Обновить данные сейчас", ...problems].join("\n");
}

async function refreshStatus() {
  try {
    state.status = await api("/api/status");
  } catch (_) {
    state.status = null;
  }
  paintStatus();
  return state.status;
}

async function syncNow() {
  const before = state.status && state.status.sync ? state.status.sync.finished_at : 0;
  await api("/api/sync", { method: "POST" });
  toast("Обновляю данные…");
  for (let i = 0; i < 120; i++) {
    await new Promise((r) => setTimeout(r, 3000));
    const st = await refreshStatus();
    if (st && !st.sync_running && st.sync && st.sync.finished_at !== before) break;
  }
  const problems = syncProblems(state.status && state.status.sync);
  toast(problems.length ? `Обновлено с ошибками: ${problems.join("; ")}` : "Данные обновлены",
    problems.length ? "err" : "ok", 7000);
  await reloadEverything();
}

async function reloadEverything() {
  await loadTournaments();
  await loadHeroes();
  state.teams = null;
  render();
}

// --- загрузка ----------------------------------------------------------------

async function loadTournaments() {
  const t = await api("/api/tournaments");
  state.tournaments = t.tournaments;
  const saved = store("tournament");
  const keep = [state.tournamentId, saved].find((id) => id && t.tournaments.some((x) => x.id === id));
  state.tournamentId = keep || t.default;
  const sel = clear(document.getElementById("tournament"));
  for (const x of t.tournaments) {
    const label = `${x.title}${x.games ? ` · ${nGames(x.games)}` : ""}`;
    sel.append(h("option", { value: x.id, selected: x.id === state.tournamentId }, label));
  }
}

async function loadHeroes() {
  const hs = await api("/api/heroes");
  state.heroes = hs.heroes;
  state.heroById = new Map(hs.heroes.map((x) => [x.id, x]));
}

async function waitForModel(view) {
  for (;;) {
    const st = await refreshStatus();
    if (st && st.model && st.model.ready) return;
    clear(view).append(h("div", { class: "empty" }, h("span", { class: "spinner" }),
      " Загружаю историю матчей и обучаю модель… ",
      st && st.model && st.model.error ? h("div", { class: "muted" }, st.model.error) : null));
    await new Promise((r) => setTimeout(r, 3000));
  }
}

async function boot() {
  const view = document.getElementById("view");
  await waitForModel(view);
  await loadTournaments();
  await loadHeroes();
  document.getElementById("tournament").addEventListener("change", (e) => {
    state.tournamentId = Number(e.target.value);
    store("tournament", state.tournamentId);
    state.teams = null;
    render();
  });
  document.getElementById("sync").addEventListener("click", () => syncNow().catch((e) => toast(e.message, "err")));
  window.addEventListener("hashchange", render);
  setInterval(refreshStatus, 60000);
  render();
}

boot().catch((e) => {
  clear(document.getElementById("view")).append(
    h("div", { class: "notice err" }, h("b", null, "Сайт не запустился. "), e.message));
});
