// Страница прогноза: две команды, драфт по игрокам, вероятность и сравнение с PARI.

import { api, qs } from "./api.js";
import { clear, debounce, fmt, h, heroImg, nGames, store, toast } from "./ui.js";
import { loadTeams, state, teamByKey } from "./app.js";
import { openHeroPicker } from "./heropicker.js";
import { openSubDialog } from "./subdialog.js";
import { openBetForm } from "./betform.js";

const DRAFT_LABELS = {
  hero_wr: "Сила героев в нашей лиге",
  comfort: "Опыт игроков на этих героях в лиге",
  meta: "Мета героев на высоком рейтинге",
  vs: "Матчапы: герои против героев",
  with: "Синергия героев внутри команды",
  pub_exp: "Опыт игроков на этих героях в рейтинге",
  pub_wr: "Винрейт игроков на этих героях в рейтинге",
};

const FEATURE_LABELS = {
  gold: "Доля золота игроков",
  elo_max: "Эло сильнейшего",
  durability: "Доигрываемость",
  mmr: "Суммарный MMR",
};

let oddsCache = { at: 0, tid: null, data: null, error: null };

async function pariOdds(force = false) {
  const fresh = Date.now() - oddsCache.at < 25000 && oddsCache.tid === state.tournamentId;
  if (fresh && !force) return oddsCache;
  try {
    const data = await api(`/api/odds${qs({ tournament_id: state.tournamentId, refresh: force ? 1 : null })}`);
    oddsCache = { at: Date.now(), tid: state.tournamentId, data, error: data.error };
  } catch (e) {
    oddsCache = { at: Date.now(), tid: state.tournamentId, data: null, error: e.message };
  }
  return oddsCache;
}

// Драфты старше полусуток уже не нужны: игра сыграна. Храним не больше 30 последних.
const DRAFT_TTL = 12 * 3600 * 1000;
function freshDrafts(drafts) {
  const now = Date.now();
  const live = Object.entries(drafts && typeof drafts === "object" ? drafts : {})
    .filter(([, d]) => d && d.heroes && Object.keys(d.heroes).length && now - (d.at || 0) < DRAFT_TTL)
    .sort((x, y) => y[1].at - x[1].at)
    .slice(0, 30);
  return Object.fromEntries(live);
}

function findEvent(odds, a, b) {
  const events = (odds.data && odds.data.events) || [];
  for (const ev of events) {
    if (!ev.linked) continue;
    if (ev.team1_key === a && ev.team2_key === b) return { ev, swapped: false };
    if (ev.team1_key === b && ev.team2_key === a) return { ev, swapped: true };
  }
  return null;
}

export async function render(view, { params, isCurrent }) {
  const data = await loadTeams();
  if (!isCurrent()) return;
  const teams = data.teams.filter((t) => t.players.length > 0);
  const storeKey = `predict:${state.tournamentId}`;
  const sel = Object.assign({ a: null, b: null, drafts: {}, k_a: null, k_b: null, manualOdds: false },
    store(storeKey) || {});
  // Раньше герои хранились по игроку на весь турнир и переезжали в следующую игру команды.
  delete sel.heroes;
  sel.drafts = freshDrafts(sel.drafts);
  if (params.get("a")) sel.a = params.get("a");
  if (params.get("b")) sel.b = params.get("b");
  if (params.get("ka")) { sel.k_a = Number(params.get("ka")); sel.k_b = Number(params.get("kb")); sel.manualOdds = false; }
  const sorted = [...teams].sort((x, y) => x.name.localeCompare(y.name, "ru"));
  if (!teamByKey(sel.a)) sel.a = sorted[0] ? sorted[0].key : null;
  if (!teamByKey(sel.b) || sel.b === sel.a) sel.b = (sorted.find((t) => t.key !== sel.a) || {}).key || null;

  clear(view);
  if (!sel.a || !sel.b) {
    view.append(h("div", { class: "empty" }, "В этом турнире пока нет двух команд с составами."));
    return;
  }

  let result = null;
  let event = null;
  let pending = 0;
  let liveGame = null;                 // идущая игра этой пары (из pari-mixer)
  const provisional = new Map();       // account_id -> герой, назначенный предварительно
  if (sel.autoLive === undefined) sel.autoLive = true;
  const save = () => store(storeKey, sel);

  const resultBox = h("section", { class: "card result" });
  const panelA = h("section", { class: "card team-panel a" });
  const panelB = h("section", { class: "card team-panel b" });
  const oddsBox = h("section", { class: "card" });
  view.append(
    h("div", { class: "page-title" }, h("h1", null, "Прогноз игры"),
      h("span", { class: "muted" }, data.games_played ? `в кубке сыграно: ${nGames(data.games_played)}` : "кубок ещё не начался"),
      h("a", { href: "#/schedule", class: "muted", style: { marginLeft: "auto", fontSize: "13px" } }, "Расписание →")),
    h("div", { class: "stack" }, resultBox, h("div", { class: "predict-grid" }, panelA, panelB), oddsBox));

  const lineup = (team) => team.players.slice(0, 5).map((p) => p.account_id);
  // Герои выбираются на игру: драфт хранится по паре команд (порядок A/B не важен).
  const pairKey = () => [sel.a, sel.b].sort().join("|");
  const draft = () => (sel.drafts[pairKey()] || {}).heroes || {};
  const setHero = (acc, id) => {
    const key = pairKey();
    const d = sel.drafts[key] || (sel.drafts[key] = { at: 0, heroes: {} });
    if (id) d.heroes[acc] = id;
    else delete d.heroes[acc];
    d.at = Date.now();
    if (!Object.keys(d.heroes).length) delete sel.drafts[key];
  };
  const heroesFor = (team) => lineup(team).map((acc) => draft()[acc] ?? null);
  const takenHeroes = () => new Set(Object.entries(draft())
    .filter(([acc]) => [...lineup(teamByKey(sel.a)), ...lineup(teamByKey(sel.b))].includes(Number(acc)))
    .map(([, hero]) => hero).filter(Boolean));

  // --- запрос прогноза ---
  const runPredict = debounce(async () => {
    const ta = teamByKey(sel.a);
    const tb = teamByKey(sel.b);
    if (!ta || !tb) return;
    if (ta.players.length < 5 || tb.players.length < 5) {
      result = { error: "В составе меньше пяти игроков — добавьте замену." };
      paintAll();
      return;
    }
    const token = ++pending;
    try {
      const body = { tournament_id: state.tournamentId, team_a: sel.a, team_b: sel.b,
        lineup_a: lineup(ta), lineup_b: lineup(tb), heroes_a: heroesFor(ta), heroes_b: heroesFor(tb) };
      if (sel.k_a > 1 && sel.k_b > 1) {
        body.k_a = sel.k_a;
        body.k_b = sel.k_b;
        // коэффициенты из лайва: игра идёт, и линия уже учитывает её ход
        body.in_play = Boolean(event && !sel.manualOdds && event.ev.place === "live");
      }
      const res = await api("/api/predict", { method: "POST", body });
      if (token !== pending || !isCurrent()) return;
      result = res;
    } catch (e) {
      if (token !== pending) return;
      result = { error: e.message };
    }
    paintAll();
  }, 120);

  // Живая игра: раз в 15 секунд берём драфт и время игры и подставляем героев.
  const pairGame = (games) => (games || []).find((g) =>
    [g.team_keys.radiant, g.team_keys.dire].sort().join("|") === pairKey()) || null;
  let liveSig = "";
  async function pollLive() {
    if (!isCurrent()) { clearInterval(liveTimer); return; }
    let data;
    try {
      data = await api(`/api/live${qs({ tournament_id: state.tournamentId })}`);
    } catch (_) {
      return;
    }
    if (!isCurrent()) return;
    if (!data.enabled) { clearInterval(liveTimer); return; }
    const game = pairGame(data.games);
    const sig = JSON.stringify(game && [game.game_time, game.heroes, game.picks, sel.autoLive]);
    if (sig === liveSig) return;
    liveSig = sig;
    liveGame = game;
    provisional.clear();
    if (game && sel.autoLive) {
      const ours = new Set([...lineup(teamByKey(sel.a)), ...lineup(teamByKey(sel.b))]);
      for (const x of game.heroes) {
        if (!ours.has(x.account_id)) continue;
        setHero(x.account_id, x.hero_id);
        if (x.provisional) provisional.set(x.account_id, x.hero_id);
      }
      save();
    }
    paintAll();
    runPredict();
  }
  const liveTimer = setInterval(pollLive, 15000);
  pollLive();

  async function loadOdds(force = false) {
    const odds = await pariOdds(force);
    if (!isCurrent()) return;
    event = findEvent(odds, sel.a, sel.b);
    if (event && !sel.manualOdds) {
      const { ev, swapped } = event;
      const k = [swapped ? ev.k2 : ev.k1, swapped ? ev.k1 : ev.k2];
      if (k[0] !== sel.k_a || k[1] !== sel.k_b) {      // пересчёт — только если линия сдвинулась
        [sel.k_a, sel.k_b] = k;
        save();
        runPredict();
      }
    }
    paintOdds();
  }

  // Линия обновляется сама: в лайве PARI то приостанавливает приём, то открывает снова.
  const oddsTimer = setInterval(() => {
    if (!isCurrent()) { clearInterval(oddsTimer); return; }
    if (document.visibilityState === "visible") loadOdds();
  }, 30000);

  function setTeam(side, key) {
    liveGame = null;
    liveSig = "";
    provisional.clear();
    sel[side] = key;
    if (sel.a === sel.b) sel[side === "a" ? "b" : "a"] = (sorted.find((t) => t.key !== key) || {}).key;
    sel.manualOdds = false;
    sel.k_a = sel.k_b = null;
    save();
    result = null;
    paintAll();
    runPredict();
    loadOdds();
  }

  // --- отрисовка ---
  function paintAll() {
    paintResult();
    paintTeam(panelA, "a");
    paintTeam(panelB, "b");
    paintOdds();
  }

  function playerResult(side, acc) {
    if (!result || !result.teams) return null;
    return result.teams[side].players.find((p) => p.account_id === acc) || null;
  }

  function paintTeam(panel, side) {
    const team = teamByKey(sel[side]);
    clear(panel);
    const select = h("select", { class: "input", "aria-label": side === "a" ? "Команда A" : "Команда B",
      onchange: (e) => setTeam(side, e.target.value) },
      sorted.map((t) => h("option", { value: t.key, selected: t.key === team.key },
        `${t.name}${t.rank ? ` · #${t.rank}` : ""}`)));
    const tr = result && result.teams ? result.teams[side] : null;
    panel.append(
      h("div", { class: "team-head" },
        h("span", { class: `badge ${side}` }, side === "a" ? "A" : "B"), select,
        h("button", { type: "button", class: "btn small", title: "Отметить замену в составе",
          onclick: () => openSubDialog({ team, tournamentId: state.tournamentId, onDone: async () => {
            await loadTeams(true);
            paintAll();
            runPredict();
          } }) }, "Замена")),
      h("div", { class: "team-meta" },
        team.rank ? h("span", { class: "badge" }, `#${team.rank} в кубке по силе`) : null,
        tr ? h("span", { class: "badge" }, `оценка состава ${tr.score.toFixed(3)}`) : null,
        tr && tr.form.games > 0.05 ? h("span", { class: `badge ${tr.form.adj >= 0 ? "good" : "bad"}` },
          `форма ${tr.form.adj >= 0 ? "+" : ""}${tr.form.adj.toFixed(2)} (${tr.form.wins.toFixed(1)}/${tr.form.games.toFixed(1)})`) : null));

    team.players.slice(0, 5).forEach((p) => {
      const heroId = draft()[p.account_id] ?? null;
      const hero = heroId ? state.heroById.get(heroId) : null;
      const pr = playerResult(side, p.account_id);
      const guess = hero && provisional.get(p.account_id) === heroId;
      const slot = h("button", { type: "button", class: `hero-slot${hero ? " filled" : ""}${guess ? " provisional" : ""}`,
        title: guess ? `${hero.name} — предварительно: капитан взял героя, но игрок ещё не выбран`
          : hero ? `${hero.name} — сменить` : "Выбрать героя",
        onclick: () => openHeroPicker({ player: p, current: heroId, taken: takenHeroes(), onPick: (id) => {
          setHero(p.account_id, id);
          provisional.delete(p.account_id);
          save();
          paintTeam(panel, side);
          runPredict();
        } }) }, hero ? heroImg(hero) : "+");
      let heroNote = null;
      if (hero && pr && pr.hero_games !== undefined) {
        const wr = pr.hero_games ? ` · ${fmt.pct(pr.hero_wins / pr.hero_games, 0)} побед` : "";
        const pub = pr.pub
          ? (pr.pub.games
            ? `; в рейтинге ${nGames(pr.pub.games)} (за полгода ${pr.pub.games_recent}), ${fmt.pct(pr.pub.wins / pr.pub.games, 0)} побед`
            : "; в рейтинге на нём не играл")
          : "";
        const seasoned = pr.hero_games >= 5 || (pr.pub && pr.pub.games >= 100);
        const fresh = pr.hero_games === 0 && (!pr.pub || pr.pub.games < 10);
        heroNote = h("div", { class: `hero-note ${seasoned ? "good" : fresh ? "bad" : "muted"}` },
          `${hero.name}: ${pr.hero_games ? `${nGames(pr.hero_games)} в лиге${wr}` : "в лиге не играл"}${pub}`);
      } else if (hero) {
        heroNote = h("div", { class: "hero-note muted" }, hero.name);
      }
      panel.append(h("div", { class: "player" },
        slot,
        h("div", { style: { minWidth: 0 } },
          h("div", { class: "player-name" }, p.name, p.substitute ? h("span", { class: "sub" }, "замена") : null),
          h("div", { class: "player-sub" }, [fmt.num(p.mmr) + " MMR", fmt.roles(p.roles)].filter(Boolean).join(" · ")),
          heroNote),
        h("div", { class: "player-stats" },
          p.games ? [h("div", null, h("b", null, fmt.pct(p.winrate, 0)), ` побед · ${nGames(p.games)}`),
            h("div", null, `Эло ${p.elo} · золото ${p.gold ? p.gold.toFixed(2) : "—"}`)]
            : h("div", null, "нет игр в лиге"))));
    });
    if (team.players.length < 5) {
      panel.append(h("div", { class: "notice", style: { marginTop: "8px" } },
        "В составе меньше пяти человек. Нажмите «Замена» или дождитесь обновления состава."));
    }
    const hasHeroes = team.players.some((p) => draft()[p.account_id]);
    if (hasHeroes) {
      panel.append(h("div", { class: "row", style: { marginTop: "10px", justifyContent: "flex-end" } },
        h("button", { type: "button", class: "btn small ghost", onclick: () => {
          team.players.forEach((p) => setHero(p.account_id, null));
          save();
          paintTeam(panel, side);
          runPredict();
        } }, "Очистить героев")));
    }
  }

  function paintResult() {
    clear(resultBox);
    const ta = teamByKey(sel.a);
    const tb = teamByKey(sel.b);
    if (!result) {
      resultBox.append(h("div", { class: "empty" }, h("span", { class: "spinner" }), " Считаю…"));
      return;
    }
    if (result.error) {
      resultBox.append(h("div", { class: "notice err" }, result.error));
      return;
    }
    const pa = result.p_a;
    const winnerA = pa >= 0.5;
    const swap = h("button", { type: "button", class: "btn small ghost", title: "Поменять команды местами",
      onclick: () => {
        [sel.a, sel.b] = [sel.b, sel.a];
        if (sel.k_a && sel.k_b) [sel.k_a, sel.k_b] = [sel.k_b, sel.k_a];
        save();
        result = null;
        paintAll();
        runPredict();
        loadOdds();
      } }, "⇄ поменять");
    resultBox.append(...[
      liveStrip(),
      h("div", { class: "result-head" },
        h("div", { class: "winner" }, "Победа: ",
          h("span", { style: { color: winnerA ? "var(--team-a)" : "var(--team-b)" } }, winnerA ? ta.name : tb.name),
          ` — ${fmt.pct(winnerA ? pa : 1 - pa)}`),
        swap),
      h("div", { class: "prob-names" },
        h("span", { class: "a" }, ta.name), h("span", { class: "b" }, tb.name)),
      h("div", { class: "prob-bar" },
        h("div", { class: "a", style: { width: `${pa * 100}%` } }, fmt.pct(pa)),
        h("div", { class: "b", style: { width: `${(1 - pa) * 100}%` } }, fmt.pct(1 - pa))),
      h("div", { class: "prob-lines" },
        h("span", null, "До драфта: ", h("b", null, fmt.pct(result.pre_draft.p_a))),
        result.draft
          ? h("span", null, `С драфтом (${result.draft.heroes} из 10 героев): `, h("b", null, fmt.pct(result.draft.p_a)),
            h("span", { class: result.draft.shift >= 0 ? "good" : "bad" }, ` ${fmt.signedPct(result.draft.shift)}`))
          : h("span", null, "Драфт: выберите героев, чтобы учесть пики"),
        h("span", null, "Честные коэф.: ", h("b", null, `${fmt.odds(result.fair_odds.a)} / ${fmt.odds(result.fair_odds.b)}`))),
      draftBlock(ta, tb),
      whyBlock(ta, tb)].filter(Boolean));     // без драфта и LIVE — без пустых блоков
  }

  function liveStrip() {
    if (!liveGame) return null;
    const g = liveGame;
    const clock = (t) => `${t < 0 ? "-" : ""}${Math.floor(Math.abs(t) / 60)}:${String(Math.abs(t) % 60).padStart(2, "0")}`;
    const guesses = g.heroes.length - g.assigned;
    const state = g.before_horn
      ? `драфт: ${g.picks} из 10 пиков, у игроков ${g.assigned}${guesses ? ` (+${guesses} предварительно)` : ""} · до горна`
      : `идёт игра ${clock(g.game_time)}, счёт ${g.score[0]}:${g.score[1]}`;
    const toggle = h("a", { href: "#", onclick: (e) => {
      e.preventDefault();
      sel.autoLive = !sel.autoLive;
      save();
      liveSig = "";
      pollLive();
    } }, sel.autoLive ? "не подставлять героев" : "подставлять героев из игры");
    return h("div", { class: "live-strip" }, h("span", { class: "badge bad" }, "LIVE"), " ", state, " · ", toggle);
  }

  // Из чего сложился драфт: каждый признак — сдвиг вероятности в пунктах.
  function draftBlock(ta, tb) {
    const d = result.draft;
    if (!d || !d.effects) return null;
    const lean = (v) => (v >= 0 ? ta.name : tb.name);
    const color = (v) => (v >= 0 ? "var(--team-a)" : "var(--team-b)");
    const rows = Object.entries(d.effects)
      .filter(([, v]) => Math.abs(v) >= 0.001)
      .sort((x, y) => Math.abs(y[1]) - Math.abs(x[1]))
      .map(([k, v]) => h("div", { class: "factor-row" }, h("span", null, DRAFT_LABELS[k] || k),
        h("span", { style: { color: color(v) } }, `${(Math.abs(v) * 100).toFixed(1)} п.п. за ${lean(v)}`)));
    const notes = (d.matchups || []).map((m) => {
      const a = state.heroById.get(m.a);
      const b = state.heroById.get(m.b);
      return h("span", { class: "badge", style: { color: color(m.adv) } },
        `${a ? a.name : m.a} против ${b ? b.name : m.b}: ${fmt.signedPct(m.adv)}`);
    });
    return h("div", { class: "draft-box" },
      h("h3", null, "Драфт"),
      rows.length ? rows : h("div", { class: "muted" }, "Драфт почти равный"),
      notes.length ? h("div", { class: "row", style: { marginTop: "8px", gap: "6px" } },
        h("span", { class: "muted", style: { fontSize: "12px" } }, "Заметные матчапы:"), notes) : null);
  }

  function whyBlock(ta, tb) {
    const side = (key, team) => {
      const t = result.teams[key];
      const rows = Object.entries(t.contrib).map(([k, v]) =>
        h("div", { class: "factor-row" }, h("span", null, FEATURE_LABELS[k] || k),
          h("span", { class: v >= 0 ? "good" : "bad" }, `${v >= 0 ? "+" : ""}${v.toFixed(2)}`)));
      rows.push(h("div", { class: "factor-row" }, h("span", null, "Форма в кубке"),
        h("span", { class: t.form.adj >= 0 ? "good" : "bad" },
          `${t.form.adj >= 0 ? "+" : ""}${t.form.adj.toFixed(2)} · зачтено игр ${t.form.games.toFixed(1)}`)));
      return h("div", null, h("h3", null, team.name), rows);
    };
    const rating = (t) => result.teams[t].rating;
    return h("details", { class: "why" },
      h("summary", null, "Почему так"),
      h("p", { class: "muted", style: { fontSize: "13px" } },
        "Рейтинг в логитах: вклад признаков состава (по играм в прошлых кубках, относительно соперников этого кубка) ",
        "плюс поправка за результаты в текущем кубке с учётом силы соперников. Разница рейтингов ",
        `${(rating("a") - rating("b")).toFixed(2)} → ${fmt.pct(result.pre_draft.p_a)} до драфта.`),
      h("div", { class: "factors" }, side("a", ta), side("b", tb)),
      h("div", { style: { marginTop: "10px" } },
        h("h3", null, "Серия из трёх игр"),
        h("div", { class: "factor-row" }, h("span", null, `${ta.name} берёт серию до двух побед`), h("span", null, fmt.pct(result.series.bo3_a))),
        ...Object.entries(result.series.three_games).map(([score, p]) =>
          h("div", { class: "factor-row" }, h("span", null, `Счёт ${score} (все три игры)`), h("span", null, fmt.pct(p))))));
  }

  function paintOdds() {
    clear(oddsBox);
    const ta = teamByKey(sel.a);
    const tb = teamByKey(sel.b);
    let source;
    if (event && !sel.manualOdds) {
      const ev = event.ev;
      const opening = ev.opening ? `, открытие ${fmt.odds(event.swapped ? ev.opening.k2 : ev.opening.k1)} / ${fmt.odds(event.swapped ? ev.opening.k1 : ev.opening.k2)}` : "";
      const paused = ev.blocked || !ev.k1 || !ev.k2;
      const same = ev.last && ev.opening && ev.last.k1 === ev.opening.k1 && ev.last.k2 === ev.opening.k2;
      const last = paused && ev.last && !same
        ? ` · последние ${fmt.odds(event.swapped ? ev.last.k2 : ev.last.k1)} / ${fmt.odds(event.swapped ? ev.last.k1 : ev.last.k2)} (${fmt.ago(ev.last.at)})` : "";
      source = h("div", { class: "muted" }, `Из линии PARI: ${ev.segment}${ev.place === "live" ? " · LIVE" : ""}${paused ? " · приём ставок приостановлен" : ""}${opening}${last}`,
        paused ? h("div", { class: "faint", style: { fontSize: "12px" } },
          "Пока приём закрыт, PARI коэффициентов не показывает. Линия обновляется сама раз в 30 секунд — как только приём откроется, коэффициенты подставятся.") : null);
    } else if (oddsCache.error && !(oddsCache.data && oddsCache.data.events && oddsCache.data.events.length)) {
      source = h("div", { class: "notice" }, h("b", null, "Линия PARI недоступна. "), "Введите коэффициенты вручную. ",
        h("span", { class: "faint" }, oddsCache.error.slice(0, 160)));
    } else if (sel.manualOdds) {
      source = h("div", { class: "muted" }, "Коэффициенты введены вручную. ",
        event ? h("a", { href: "#", onclick: (e) => { e.preventDefault(); sel.manualOdds = false; save(); loadOdds(); } }, "Вернуть из PARI") : null);
    } else {
      source = h("div", { class: "muted" }, oddsCache.at ? "Этого матча нет в линии PARI — введите коэффициенты вручную." : "Ищу матч в линии PARI…");
    }
    const input = (key, label) => h("label", { class: "field" }, h("span", null, label),
      h("input", { type: "number", step: "0.01", min: "1.01", inputmode: "decimal", value: sel[key] || "",
        placeholder: "например 1.85",
        oninput: (e) => {
          const v = Number(e.target.value.replace(",", "."));
          sel[key] = v > 1 ? v : null;
          sel.manualOdds = true;
          save();
          runPredict();
        } }));
    oddsBox.append(
      h("div", { class: "row" }, h("h2", { class: "grow", style: { margin: 0 } }, "Сравнение с букмекером"),
        h("button", { type: "button", class: "btn small ghost", onclick: () => loadOdds(true) }, "Обновить линию")),
      h("div", { style: { margin: "8px 0 12px" } }, source),
      h("div", { class: "odds-grid" }, input("k_a", `Коэффициент на ${ta.name} (П1)`), input("k_b", `Коэффициент на ${tb.name} (П2)`)));

    const offer = result && result.offer;
    if (!offer) {
      oddsBox.append(h("p", { class: "muted", style: { marginBottom: 0 } },
        "Введите оба коэффициента — покажу вероятность букмекера без маржи, ожидание и размер ставки по Келли."));
      return;
    }
    const sideBox = (key, team) => {
      const s = offer.sides[key];
      return h("div", { class: `offer-side${s.value ? " value" : ""}` },
        h("div", { class: "row" }, h("b", { class: "grow" }, team.name), s.value ? h("span", { class: "badge good" }, "есть перевес") : null),
        h("div", { class: "big" }, fmt.odds(s.odds)),
        h("div", { class: "kv" }, h("span", null, "Наша вероятность"), h("span", null, fmt.pct(s.model_p))),
        h("div", { class: "kv" }, h("span", null, "Вероятность PARI без маржи"), h("span", null, fmt.pct(s.book_p))),
        h("div", { class: "kv" }, h("span", null, "Ожидание на 100 ₽"),
          h("span", { class: s.ev > 0 ? "good" : "bad" }, `${s.ev > 0 ? "+" : ""}${(s.ev * 100).toFixed(1)} ₽`)),
        h("div", { class: "kv" }, h("span", null, "Минимальный выгодный коэф."), h("span", null, fmt.odds(s.min_odds))),
        h("div", { class: "kv" }, h("span", null, "Ставка по Келли"), h("span", null, s.stake ? `${fmt.money(s.stake)} ₽` : "не ставить")),
        h("button", { type: "button", class: `btn small ${s.value ? "good" : ""}`, style: { marginTop: "8px", width: "100%" },
          onclick: () => betForm(key === "a" ? "A" : "B") }, "Записать ставку"));
    };
    // Element.append(null) вставил бы текст «null»: пустые блоки отбрасываем
    oddsBox.append(...[
      offer.draft_window ? h("div", { class: "notice", style: { marginTop: "12px" } },
        h("b", null, "Окно драфта. "),
        "Драфт полный, а игра ещё не началась: прогноз уже знает героев, линия — ещё не ход игры. ",
        "Перевес считаем до горна; время игры приходит с задержкой до 30 секунд.",
        result.live && result.live.assigned < 10
          ? ` Героев у игроков пока ${result.live.assigned} из 10, остальные назначены предварительно.` : "") : null,
      offer.in_play ? h("div", { class: "notice", style: { marginTop: "12px" } },
        h("b", null, "Игра уже идёт. "),
        "В лайве PARI двигает коэффициенты по ходу игры, а прогноз учитывает только составы и драфт, ",
        "поэтому перевес и ставку не предлагаем. Ожидание ниже верно разве что сразу после драфта.") : null,
      h("div", { class: "muted", style: { fontSize: "13px", marginTop: "12px" } },
        `Маржа PARI ${fmt.pct(offer.margin)}. Перевес считается, если ожидание не меньше порога из настроек ставок.`),
      h("div", { class: "offer" }, sideBox("a", ta), sideBox("b", tb))].filter(Boolean));
  }

  function betForm(pick) {
    const ta = teamByKey(sel.a);
    const tb = teamByKey(sel.b);
    const offer = result.offer;
    openBetForm({
      tournament_id: state.tournamentId, team_a_key: sel.a, team_b_key: sel.b, team_a: ta.name, team_b: tb.name,
      pick, odds: pick === "A" ? sel.k_a : sel.k_b, odds_a: sel.k_a, odds_b: sel.k_b,
      stake: offer ? offer.sides[pick === "A" ? "a" : "b"].stake || "" : "",
      stake_a: offer ? offer.sides.a.stake : null, stake_b: offer ? offer.sides.b.stake : null,
      model_prob_a: result.p_a, book_prob_a: offer ? offer.book_p_a : null,
      stage: result.draft ? "draft" : "prematch",
      pari_event_id: event ? event.ev.event_id : null,
      snapshot: { lineup_a: lineup(ta), lineup_b: lineup(tb), heroes_a: heroesFor(ta), heroes_b: heroesFor(tb),
        p_a: result.p_a, pre_draft_p_a: result.pre_draft.p_a },
    });
  }

  paintAll();
  runPredict();
  loadOdds().catch((e) => toast(e.message, "err"));
}
