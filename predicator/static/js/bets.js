// Страница ставок: линия PARI против модели, настройки банка, журнал.

import { api, qs } from "./api.js";
import { clear, fmt, h, toast } from "./ui.js";
import { navigate, render as rerender, state } from "./app.js";
import { openBetForm } from "./betform.js";

const STATUS = { open: "открыта", won: "выиграла", lost: "проиграла", void: "возврат" };

export async function render(view, { isCurrent, params }) {
  const [journal, odds] = await Promise.all([
    api("/api/bets"),
    api(`/api/odds${qs({ tournament_id: state.tournamentId, refresh: params.get("refresh") ? 1 : null })}`)
      .catch((e) => ({ enabled: true, error: e.message, events: [] })),
  ]);
  if (!isCurrent()) return;
  clear(view);
  view.append(h("div", { class: "page-title" }, h("h1", null, "Ставки")));
  view.append(h("div", { class: "stack" }, summary(journal.summary), lineCard(odds, journal.settings),
    journalCard(journal.bets), settingsCard(journal.settings)));
}

function summary(s) {
  const stat = (label, value, cls) => h("div", { class: "stat" }, h("span", null, label), h("b", { class: cls }, value));
  return h("div", { class: "stats-row" },
    stat("Ставок", `${s.total} (открыто ${s.open})`),
    stat("Выиграно", s.settled ? `${s.won} из ${s.settled}` : "—"),
    stat("Прибыль", `${fmt.money(s.profit)} ₽`, s.profit > 0 ? "good" : s.profit < 0 ? "bad" : ""),
    stat("ROI", s.roi === null ? "—" : fmt.signedPct(s.roi), s.roi > 0 ? "good" : s.roi < 0 ? "bad" : ""),
    stat("Ожидалось по модели", `${fmt.money(s.expected_profit)} ₽`),
    stat("В игре сейчас", `${fmt.money(s.open_stake)} ₽`));
}

function lineCard(odds, settings) {
  const card = h("section", { class: "card" });
  card.append(h("div", { class: "row" },
    h("h2", { class: "grow", style: { margin: 0 } }, "Линия PARI: Dota 2"),
    h("button", { type: "button", class: "btn small ghost", onclick: () => navigate("#/bets?refresh=1") }, "Обновить")));
  if (!odds.enabled) {
    card.append(h("p", { class: "muted" }, "Загрузка линии выключена (PARI_ENABLED=0). Коэффициенты можно вводить вручную на странице прогноза."));
    return card;
  }
  const st = odds.status || {};
  card.append(h("p", { class: "muted", style: { fontSize: "13px" } },
    st.fetched_at ? `Обновлено ${fmt.ago(st.fetched_at)}${st.host ? ` · ${st.host.replace(/^https?:\/\//, "")}` : ""}` : "",
    ` · событий Dota в памяти: ${st.events || 0}`));
  if (odds.error) {
    card.append(h("div", { class: "notice err", style: { marginBottom: "12px" } }, h("b", null, "Линия PARI недоступна. "),
      "Коэффициенты можно ввести вручную на странице прогноза. ", h("span", { class: "faint" }, odds.error.slice(0, 300))));
  }
  const linked = odds.events.filter((e) => e.linked);
  const other = odds.events.filter((e) => !e.linked);
  if (!linked.length) {
    card.append(h("div", { class: "empty" }, odds.events.length
      ? "Матчей с командами этого турнира в линии сейчас нет."
      : "В линии нет матчей по Dota 2."));
  } else {
    const rows = linked.map((ev) => {
      const offer = ev.offer;
      const best = offer && offer.best ? offer.sides[offer.best] : null;
      const evCell = (key) => {
        const label = `Ожидание ${key === "a" ? "П1" : "П2"}`;
        if (!offer) return h("td", { class: "num faint", "data-label": label }, "—");
        const s = offer.sides[key];
        return h("td", { class: `num ${s.ev > 0 ? "ev-pos" : "ev-neg"}`, "data-label": label }, fmt.signedPct(s.ev));
      };
      const opening = ev.opening && (ev.opening.k1 !== ev.k1 || ev.opening.k2 !== ev.k2)
        ? h("div", { class: "faint", style: { fontSize: "12px" } }, `открытие ${fmt.odds(ev.opening.k1)} / ${fmt.odds(ev.opening.k2)}`) : null;
      return h("tr", { class: best ? "value-row" : "" },
        h("td", { class: "nowrap", "data-label": "Начало" }, h("span", null, fmt.time(ev.start_time),
          ev.place === "live" ? h("span", { class: "badge bad", style: { marginLeft: "6px" } }, "LIVE") : null)),
        h("td", { class: "event-teams cell-main" }, h("b", null, ev.team1), h("b", null, ev.team2),
          ev.blocked ? h("span", { class: "badge" }, "приём приостановлен") : null),
        h("td", { class: "num", "data-label": "Коэф. PARI" }, h("span", null, `${fmt.odds(ev.k1)} / ${fmt.odds(ev.k2)}`, opening)),
        h("td", { class: "num", "data-label": "Наша вероятность" }, ev.p_a !== undefined ? `${fmt.pct(ev.p_a, 0)} / ${fmt.pct(1 - ev.p_a, 0)}` : "—"),
        h("td", { class: "num", "data-label": "PARI без маржи" }, offer ? `${fmt.pct(offer.book_p_a, 0)} / ${fmt.pct(1 - offer.book_p_a, 0)}` : "—"),
        evCell("a"), evCell("b"),
        h("td", { class: "num", "data-label": "Ставка по Келли" }, best ? `${fmt.money(best.stake)} ₽ на ${offer.best === "a" ? "П1" : "П2"}` : "—"),
        h("td", { class: "nowrap cell-actions" },
          h("button", { type: "button", class: "btn small ghost", onclick: () =>
            navigate(`#/predict?a=${encodeURIComponent(ev.team1_key)}&b=${encodeURIComponent(ev.team2_key)}&ka=${ev.k1 || ""}&kb=${ev.k2 || ""}`) }, "Прогноз"),
          " ",
          h("button", { type: "button", class: `btn small ${best ? "good" : ""}`, onclick: () => openBetForm({
            tournament_id: odds.tournament_id, team_a_key: ev.team1_key, team_b_key: ev.team2_key,
            team_a: ev.team1, team_b: ev.team2, pick: offer && offer.best === "b" ? "B" : "A",
            odds: offer && offer.best === "b" ? ev.k2 : ev.k1, odds_a: ev.k1, odds_b: ev.k2,
            stake: best ? best.stake : "", stake_a: offer ? offer.sides.a.stake : null, stake_b: offer ? offer.sides.b.stake : null,
            model_prob_a: ev.p_a ?? null, book_prob_a: offer ? offer.book_p_a : null, stage: "prematch",
            pari_event_id: ev.event_id, snapshot: lineupSnapshot(ev) }, () => rerender()) }, "Ставка")));
    });
    card.append(h("div", { class: "table-wrap" }, h("table", { class: "data cards" },
      h("thead", null, h("tr", null, h("th", null, "Начало"), h("th", null, "Матч (П1 / П2)"), h("th", { class: "num" }, "Коэф. PARI"),
        h("th", { class: "num" }, "Наша вер."), h("th", { class: "num" }, "PARI без маржи"), h("th", { class: "num" }, "EV П1"),
        h("th", { class: "num" }, "EV П2"), h("th", { class: "num" }, "Ставка"), h("th", null, ""))),
      h("tbody", null, rows))));
    card.append(h("p", { class: "muted", style: { fontSize: "12px", marginBottom: 0 } },
      `Прогноз здесь — до драфта, по текущим составам. Перевес — ожидание от ${fmt.pct(settings.min_edge, 0)} на рубль. `,
      "После драфта откройте «Прогноз» и выберите героев — вероятность уточнится."));
  }
  if (other.length) {
    card.append(h("details", { style: { marginTop: "12px" } },
      h("summary", { class: "muted" }, `Другие матчи Dota в линии (${other.length}) — не сопоставлены с командами турнира`),
      h("div", { class: "table-wrap" }, h("table", { class: "data" }, h("tbody", null, other.map((ev) => h("tr", null,
        h("td", { class: "nowrap" }, fmt.time(ev.start_time)), h("td", { class: "muted" }, ev.segment),
        h("td", null, `${ev.team1} — ${ev.team2}`), h("td", { class: "num" }, `${fmt.odds(ev.k1)} / ${fmt.odds(ev.k2)}`))))))));
  }
  return card;
}

function lineupSnapshot(ev) {
  return { lineup_a: ev.lineup_a || [], lineup_b: ev.lineup_b || [], p_a: ev.p_a ?? null,
    pari_team1: ev.team1, pari_team2: ev.team2 };
}

function journalCard(bets) {
  const card = h("section", { class: "card" }, h("h2", null, "Журнал ставок"));
  if (!bets.length) {
    card.append(h("div", { class: "empty" }, "Ставок пока нет. Записать ставку можно из прогноза или из линии выше."));
    return card;
  }
  const rows = bets.map((b) => h("tr", null,
    h("td", { class: "nowrap muted", "data-label": "Когда" }, b.created_at.slice(5, 16)),
    h("td", { class: "cell-main" }, h("b", { style: { color: b.pick === "A" ? "var(--text)" : "var(--muted)" } }, b.team_a), " — ",
      h("b", { style: { color: b.pick === "B" ? "var(--text)" : "var(--muted)" } }, b.team_b),
      b.note ? h("div", { class: "faint", style: { fontSize: "12px" } }, b.note) : null),
    h("td", { "data-label": "Выбор" }, h("span", null, h("span", { class: `badge ${b.pick === "A" ? "a" : "b"}` }, b.pick === "A" ? "П1" : "П2"),
      b.stage === "draft" ? h("span", { class: "faint", style: { fontSize: "12px" } }, " после драфта") : null)),
    h("td", { class: "num", "data-label": "Коэффициент" }, fmt.odds(b.odds)),
    h("td", { class: "num", "data-label": "Сумма" }, fmt.money(b.stake)),
    h("td", { class: "num", "data-label": "Наша вероятность" }, b.model_prob ? fmt.pct(b.model_prob, 0) : "—"),
    h("td", { "data-label": "Статус" }, h("select", { class: "input", style: { width: "auto" }, onchange: async (e) => {
      try {
        await api(`/api/bets/${b.id}`, { method: "PATCH", body: { status: e.target.value } });
        rerender();
      } catch (err) {
        toast(err.message, "err");
      }
    } }, Object.entries(STATUS).map(([k, label]) => h("option", { value: k, selected: k === b.status }, label)))),
    h("td", { class: `num ${b.profit > 0 ? "good" : b.profit < 0 ? "bad" : ""}`, "data-label": "Итог" }, b.profit === null ? "—" : fmt.money(b.profit)),
    h("td", { class: "cell-actions" }, h("button", { type: "button", class: "btn small ghost danger", title: "Удалить запись", onclick: async () => {
      if (!confirm("Удалить ставку из журнала?")) return;
      await api(`/api/bets/${b.id}`, { method: "DELETE" });
      rerender();
    } }, "×"))));
  card.append(h("div", { class: "table-wrap" }, h("table", { class: "data cards" },
    h("thead", null, h("tr", null, h("th", null, "Когда"), h("th", null, "Матч"), h("th", null, "Выбор"),
      h("th", { class: "num" }, "Коэф."), h("th", { class: "num" }, "Сумма"), h("th", { class: "num" }, "Наша вер."),
      h("th", null, "Статус"), h("th", { class: "num" }, "Итог"), h("th", null, ""))),
    h("tbody", null, rows))));
  card.append(h("p", { class: "muted", style: { fontSize: "12px", marginBottom: 0 } },
    "Ставки, записанные со страницы прогноза, закрываются сами, когда в данных появится результат матча с этими составами."));
  return card;
}

function settingsCard(settings) {
  const fields = [
    ["bankroll", "Банк, ₽", 1, 1],
    ["kelly_fraction", "Доля Келли (0.25 = четверть)", 0.05, 1],
    ["max_stake_pct", "Максимум на одну ставку, доля банка", 0.01, 1],
    ["min_edge", "Минимальное ожидание на рубль", 0.01, 1],
  ];
  const inputs = {};
  const card = h("section", { class: "card" }, h("h2", null, "Настройки ставок"));
  card.append(h("div", { class: "odds-grid" }, fields.map(([key, label, step]) => {
    inputs[key] = h("input", { type: "number", step, min: 0, value: settings[key] });
    return h("label", { class: "field" }, h("span", null, label), inputs[key]);
  })));
  card.append(h("div", { class: "row", style: { marginTop: "12px", justifyContent: "space-between" } },
    h("span", { class: "muted", style: { fontSize: "12px" } },
      "Модель ошибается, поэтому ставка — доля от полного Келли и не больше заданной доли банка."),
    h("button", { type: "button", class: "btn primary", onclick: async () => {
      const body = {};
      for (const [key] of fields) body[key] = Number(inputs[key].value);
      try {
        await api("/api/settings", { method: "PUT", body });
        toast("Настройки сохранены", "ok");
        rerender();
      } catch (e) {
        toast(e.message, "err");
      }
    } }, "Сохранить")));
  return card;
}
