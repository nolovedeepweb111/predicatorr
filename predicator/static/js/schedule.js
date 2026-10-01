// Расписание: будущие игры кубка с нашим прогнозом до драфта и линией PARI.

import { api, qs } from "./api.js";
import { clear, fmt, h } from "./ui.js";
import { navigate, state } from "./app.js";

export async function render(view, { isCurrent }) {
  const data = await api(`/api/games${qs({ tournament_id: state.tournamentId, kind: "upcoming", limit: 80 })}`);
  if (!isCurrent()) return;
  clear(view);
  view.append(h("div", { class: "page-title" }, h("h1", null, "Расписание"),
    h("span", { class: "muted" }, data.games.length ? `ближайшие игры: ${data.games.length}` : ""),
    h("a", { href: "#/results", class: "muted", style: { marginLeft: "auto", fontSize: "13px" } }, "Результаты →")));
  if (!data.games.length) {
    view.append(h("div", { class: "empty" }, "Будущих игр нет: кубок не идёт или расписание ещё не опубликовано."));
    return;
  }
  const groups = new Map();
  for (const g of data.games) {
    const key = g.status === "ACTIVE" ? "Сейчас" : fmt.day(g.planned_time);
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(g);
  }
  const stack = h("div", { class: "stack" });
  for (const [day, games] of groups) {
    stack.append(h("section", { class: "card" }, h("h2", null, day), table(games)));
  }
  stack.append(h("p", { class: "muted", style: { fontSize: "12px" } },
    "Прогноз — до драфта, по текущим составам и форме в кубке. Перевес — ожидание по линии PARI от порога из настроек ставок. ",
    "Во время игры перевес не считаем: линия уже учитывает ход игры."));
  view.append(stack);
}

function table(games) {
  return h("div", { class: "table-wrap" }, h("table", { class: "data cards" },
    h("thead", null, h("tr", null, h("th", null, "Время"), h("th", null, "Матч"), h("th", { class: "num" }, "Наш прогноз"),
      h("th", { class: "num" }, "PARI"), h("th", { class: "num" }, "Перевес"), h("th", null, ""))),
    h("tbody", null, games.map(row))));
}

function row(g) {
  const pa = g.p_a;
  const favA = pa !== null && pa >= 0.5;
  const name = (t, fav) => h("div", null, fav ? h("b", null, t.name) : t.name);   // фаворит — жирным
  const when = g.status === "ACTIVE"
    ? h("span", null, h("span", { class: "badge bad" }, "LIVE"), g.live
      ? h("div", { class: "faint", style: { fontSize: "12px" } }, g.live.game_time === null ? "лобби" : g.live.before_horn
        ? `драфт ${g.live.picks}/10` : `игра ${Math.floor(g.live.game_time / 60)} мин, ${g.live.score[0]}:${g.live.score[1]}`)
      : null)
    : fmt.hm(g.planned_time);
  const o = g.odds;
  const paused = o && (o.blocked || !o.k_a || !o.k_b);
  const pari = !o ? h("span", { class: "faint" }, "нет в линии")
    : paused ? h("span", { class: "faint" }, "приостановлено") : `${fmt.odds(o.k_a)} / ${fmt.odds(o.k_b)}`;
  const offer = g.offer;
  const best = offer && offer.best ? offer.sides[offer.best] : null;
  const edge = !offer ? "—" : offer.in_play ? h("span", { class: "faint" }, "лайв")
    : best ? h("span", { class: "badge good" }, `${fmt.signedPct(best.ev)} на ${offer.best === "a" ? g.team_a.name : g.team_b.name}`)
      : h("span", { class: "faint" }, "нет");
  const ka = o && !paused ? `&ka=${o.k_a}&kb=${o.k_b}` : "";
  return h("tr", { class: best ? "value-row" : "" },
    h("td", { class: "nowrap", "data-label": "Время" }, when),
    h("td", { class: "event-teams cell-main" }, name(g.team_a, favA), name(g.team_b, pa !== null && !favA)),
    h("td", { class: "num", "data-label": "Наш прогноз" }, pa === null ? "—" : `${fmt.pct(pa, 0)} / ${fmt.pct(1 - pa, 0)}`),
    h("td", { class: "num", "data-label": "PARI" }, pari),
    h("td", { class: "num", "data-label": "Перевес" }, edge),
    h("td", { class: "nowrap cell-actions" }, h("button", { type: "button", class: "btn small ghost",
      onclick: () => navigate(`#/predict?a=${encodeURIComponent(g.team_a.key)}&b=${encodeURIComponent(g.team_b.key)}${ka}`) }, "Прогноз")));
}
