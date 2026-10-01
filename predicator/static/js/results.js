// Результаты: сыгранные игры кубка — наш прогноз до драфта и с драфтом против того, что вышло.

import { api, qs } from "./api.js";
import { clear, fmt, h, heroImg } from "./ui.js";
import { state } from "./app.js";

export async function render(view, { isCurrent }) {
  const cup = state.tournaments.find((t) => t.id === state.tournamentId);
  if (cup && !cup.live) {
    clear(view).append(
      h("div", { class: "page-title" }, h("h1", null, "Результаты")),
      h("div", { class: "notice" }, "Здесь — игры идущего кубка. Прошлые кубки модель видела при обучении, ",
        "честная проверка на них — на странице ", h("a", { href: "#/model" }, "«Модель»"), "."));
    return;
  }
  const data = await api(`/api/games${qs({ tournament_id: state.tournamentId, kind: "played" })}`);
  if (!isCurrent()) return;
  clear(view);
  view.append(h("div", { class: "page-title" }, h("h1", null, "Результаты"),
    h("a", { href: "#/schedule", class: "muted", style: { marginLeft: "auto", fontSize: "13px" } }, "Расписание →")));
  if (!data.games.length) {
    view.append(h("div", { class: "empty" }, "В этом кубке ещё нет сыгранных игр."));
    return;
  }
  view.append(h("div", { class: "stack" }, summary(data.summary), h("section", { class: "card" },
    h("div", { class: "table-wrap" }, h("table", { class: "data cards" },
      h("thead", null, h("tr", null, h("th", null, "Когда"), h("th", null, "Матч"), h("th", null, "До драфта"),
        h("th", null, "С драфтом"), h("th", null, "PARI перед началом"), h("th", null, ""))),
      h("tbody", null, data.games.map(row)))))));
}

function summary(s) {
  const line = (label, hits, n) => h("div", { class: "factor-row" }, h("span", null, label),
    h("span", null, n ? h("b", null, `${hits} из ${n}`) : "—", n ? ` · ${fmt.pct(hits / n, 0)}` : ""));
  return h("section", { class: "card" },
    h("h2", null, "Как сыграл прогноз"),
    line("Наш прогноз до драфта угадал", s.pre_hits, s.games),
    line("Наш прогноз с драфтом угадал", s.draft_hits, s.games),
    line("Фаворит PARI перед началом угадал", s.pari_hits, s.pari_games),
    h("p", { class: "muted", style: { fontSize: "12px", marginBottom: 0 } },
      "Прогнозы посчитаны только по тому, что было известно до каждой игры: составы и форма — по прошлым играм кубка, ",
      "драфт — по реальным героям. Модель обучена на прошлых кубках, этот она не видела. ",
      s.pari_games < s.games ? "Цены PARI есть только для игр, которые сайт застал в линии." : ""));
}

function verdict(p, winner, a, b) {
  const favA = p >= 0.5;
  const ok = favA === (winner === "a");
  return h("span", null, `${favA ? a.name : b.name} ${fmt.pct(favA ? p : 1 - p, 0)} `,
    h("span", { class: ok ? "good" : "bad", title: ok ? "угадал" : "не угадал" }, ok ? "✓" : "✗"));
}

function heroes(ids) {
  return h("span", { class: "hero-row" }, ids.map((id) => {
    const hero = state.heroById.get(id);
    return hero ? heroImg(hero, "hero-mini") : null;
  }));
}

function row(g) {
  const a = g.team_a;
  const b = g.team_b;
  const team = (t, side) => h("div", { class: "result-team" },
    h(g.winner === side ? "b" : "span", null, t.name), g.winner === side ? h("span", { class: "badge good" }, "победа") : null,
    heroes(side === "a" ? g.heroes_a : g.heroes_b));
  const close = g.pari && g.pari.close;
  const pari = !close ? h("span", { class: "faint" }, g.pari ? "только в лайве" : "—")
    : h("span", null, `${fmt.odds(close[0])} / ${fmt.odds(close[1])} `,
      h("span", { class: (close[0] < close[1]) === (g.winner === "a") ? "good" : "bad" },
        (close[0] < close[1]) === (g.winner === "a") ? "✓" : "✗"));
  return h("tr", null,
    h("td", { class: "nowrap", "data-label": "Когда" }, fmt.time(g.start_time)),
    h("td", { class: "cell-main" }, team(a, "a"), team(b, "b")),
    h("td", { "data-label": "До драфта" }, verdict(g.pre, g.winner, a, b)),
    h("td", { "data-label": "С драфтом" }, verdict(g.draft, g.winner, a, b)),
    h("td", { "data-label": "PARI перед началом" }, pari),
    h("td", { class: "nowrap cell-actions" }, h("a", { href: `https://www.opendota.com/matches/${g.match_id}`,
      target: "_blank", rel: "noopener", class: "muted", style: { fontSize: "12px" } }, "матч ↗")));
}
