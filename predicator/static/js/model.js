// Страница модели: как устроена, насколько точна на прошлых кубках, состояние источников.

import { api } from "./api.js";
import { clear, fmt, h, nGames } from "./ui.js";
import { state } from "./app.js";

const LABELS = {
  coin: "Монетка",
  doc: "Модель из заметки: состав + зачтённые победы",
  roster: "Только состав (до кубка)",
  pre_draft: "Состав + форма с учётом соперника — до драфта",
  draft: "+ драфт: сила героев и опыт игроков на них",
};
const FEATURES = {
  gold: "Доля золота (средняя по пятёрке)",
  elo_max: "Эло сильнейшего игрока",
  durability: "Доигрываемость (доля игр команды, которые человек отыгрывает)",
  mmr: "Суммарный MMR",
};

export async function render(view, { isCurrent }) {
  const [bt, status] = await Promise.all([api("/api/backtest"), api("/api/status")]);
  if (!isCurrent()) return;
  clear(view);
  view.append(h("div", { class: "page-title" }, h("h1", null, "Модель"),
    h("span", { class: "muted" }, `обучена на ${nGames(bt.model.trained_on)} из ${bt.cups.length} кубков`)));
  view.append(h("div", { class: "stack" }, howCard(bt.model), backtestCard(bt), calibrationCard(bt), sourcesCard(status)));
}

function howCard(model) {
  const w = model.weights;
  return h("section", { class: "card" },
    h("h2", null, "Как считается вероятность"),
    h("ol", { style: { margin: "0 0 0 18px", padding: 0 } },
      h("li", null, "Оценка состава по играм каждого из пятёрки в прошлых кубках. Признаки нормируются внутри кубка, веса подобраны гребневой регрессией по командам прошлых кубков."),
      h("li", null, "Форма в текущем кубке: после каждой игры рейтинг составов сдвигается на величину «результат минус ожидание», поэтому победа над сильным весит больше. Игра засчитывается пропорционально (совпавших игроков / 5)², так что команда с заменами опирается на свой новый состав."),
      h("li", null, "Драфт, если выбраны герои: сила героя в этой лиге и сколько раз игрок на нём играл. Без героев прогноз «до драфта».")),
    h("h3", null, "Веса признаков состава (на одно стандартное отклонение, доля побед)"),
    h("div", { class: "table-wrap" }, h("table", { class: "data" }, h("tbody", null,
      Object.entries(w).map(([k, v]) => h("tr", null, h("td", null, FEATURES[k] || k),
        h("td", { class: `num ${v >= 0 ? "good" : "bad"}` }, `${v >= 0 ? "+" : ""}${v.toFixed(4)}`)))))),
    h("p", { class: "muted", style: { fontSize: "12px", marginBottom: 0 } },
      `Коэффициенты слоя драфта: логит до драфта × ${model.draft_coef[0].toFixed(3)}, сила героев × ${model.draft_coef[1].toFixed(3)}, опыт на героях × ${model.draft_coef[2].toFixed(3)}.`));
}

function backtestCard(bt) {
  const rows = Object.entries(bt.models).map(([k, v]) => h("tr", { class: k === "draft" ? "value-row" : "" },
    h("td", null, LABELS[k] || k), h("td", { class: "num" }, v.matches),
    h("td", { class: "num" }, fmt.pct(v.accuracy)), h("td", { class: "num" }, v.logloss.toFixed(4))));
  const cups = Object.entries(bt.per_cup).map(([t, v]) => h("tr", null,
    h("td", null, cupName(Number(t))),
    h("td", { class: "num" }, fmt.pct(v.pre_draft.accuracy)), h("td", { class: "num" }, v.pre_draft.logloss.toFixed(3)),
    h("td", { class: "num" }, fmt.pct(v.draft.accuracy)), h("td", { class: "num" }, v.draft.logloss.toFixed(3))));
  return h("section", { class: "card" },
    h("h2", null, "Проверка на прошлых кубках"),
    h("p", { class: "muted", style: { fontSize: "13px" } },
      "Каждый кубок предсказан моделью, обученной без него; признаки игроков — только по играм до начала кубка, форма и драфт — только по играм раньше прогнозируемой. Logloss: меньше — лучше, у монетки 0.693."),
    h("div", { class: "table-wrap" }, h("table", { class: "data" },
      h("thead", null, h("tr", null, h("th", null, "Модель"), h("th", { class: "num" }, "Игр"), h("th", { class: "num" }, "Угадано"), h("th", { class: "num" }, "Logloss"))),
      h("tbody", null, rows))),
    h("h3", null, "По кубкам"),
    h("div", { class: "table-wrap" }, h("table", { class: "data" },
      h("thead", null, h("tr", null, h("th", null, "Кубок"), h("th", { class: "num" }, "До драфта"), h("th", { class: "num" }, "logloss"),
        h("th", { class: "num" }, "С драфтом"), h("th", { class: "num" }, "logloss"))),
      h("tbody", null, cups))));
}

function cupName(id) {
  const t = state.tournaments.find((x) => x.id === id);
  if (t) return t.title;
  if (id > 30000) return `PARI Super Mixer #${id - 30000}`;
  if (id > 20000) return `WINLINE Super Mixer #${id - 20000}`;
  return `PARI Mixer Cup #${id}`;
}

function calibrationCard(bt) {
  const buckets = bt.models.draft.calibration;
  const W = 420, H = 300, pad = 40;
  const x = (p) => pad + ((p - 0.5) / 0.5) * (W - 2 * pad);
  const y = (p) => H - pad - ((p - 0.5) / 0.5) * (H - 2 * pad);
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  svg.setAttribute("class", "chart");
  svg.setAttribute("role", "img");
  svg.setAttribute("aria-label", "Калибровка: прогноз против факта");
  const add = (tag, attrs, text) => {
    const el = document.createElementNS("http://www.w3.org/2000/svg", tag);
    for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
    if (text) el.textContent = text;
    svg.append(el);
    return el;
  };
  for (let p = 0.5; p <= 1.001; p += 0.1) {
    add("line", { x1: x(p), y1: y(0.5), x2: x(p), y2: y(1), stroke: "#2a3242" });
    add("line", { x1: x(0.5), y1: y(p), x2: x(1), y2: y(p), stroke: "#2a3242" });
    add("text", { x: x(p), y: H - pad + 16, "text-anchor": "middle" }, `${Math.round(p * 100)}%`);
    add("text", { x: pad - 6, y: y(p) + 4, "text-anchor": "end" }, `${Math.round(p * 100)}%`);
  }
  add("line", { x1: x(0.5), y1: y(0.5), x2: x(1), y2: y(1), stroke: "#5d6678", "stroke-dasharray": "4 4" });
  const pts = buckets.map((b) => [x(b.predicted), y(Math.max(0.5, Math.min(1, b.actual))), b]);
  add("polyline", { points: pts.map(([px, py]) => `${px},${py}`).join(" "), fill: "none", stroke: "#38bdf8", "stroke-width": 2 });
  for (const [px, py, b] of pts) {
    const r = Math.max(3, Math.min(9, Math.sqrt(b.matches) / 3));
    add("circle", { cx: px, cy: py, r, fill: "#38bdf8" }).append(Object.assign(
      document.createElementNS("http://www.w3.org/2000/svg", "title"), { textContent: `${nGames(b.matches)}: прогноз ${fmt.pct(b.predicted)}, факт ${fmt.pct(b.actual)}` }));
  }
  add("text", { x: W / 2, y: H - 6, "text-anchor": "middle" }, "уверенность модели в фаворите");
  return h("section", { class: "card" },
    h("h2", null, "Калибровка"),
    h("p", { class: "muted", style: { fontSize: "13px" } },
      "Если модель говорит «70%», фаворит должен выигрывать примерно 70% таких игр. Точки близко к пунктиру — значит, вероятностям можно доверять при сравнении с коэффициентами."),
    h("div", { class: "row", style: { alignItems: "flex-start", gap: "24px" } }, svg,
      h("div", { class: "table-wrap grow" }, h("table", { class: "data" },
        h("thead", null, h("tr", null, h("th", null, "Уверенность"), h("th", { class: "num" }, "Игр"), h("th", { class: "num" }, "Прогноз"), h("th", { class: "num" }, "Факт"))),
        h("tbody", null, buckets.map((b) => h("tr", null, h("td", null, `${Math.round(b.from * 100)}–${Math.round(b.to * 100)}%`),
          h("td", { class: "num" }, b.matches), h("td", { class: "num" }, fmt.pct(b.predicted)), h("td", { class: "num" }, fmt.pct(b.actual)))))))));
}

function sourcesCard(status) {
  const sync = status.sync;
  const rows = [];
  if (sync) {
    const b = sync.backup || {};
    rows.push(["Бэкап матчей (GitHub)", b.error ? h("span", { class: "bad" }, b.error)
      : b.changed ? `обновлён: ${b.matches} матчей` : "без изменений", fmt.ago(sync.finished_at)]);
    for (const m of sync.mixer || []) {
      rows.push([`mixer-cup: ${m.series}`, m.error ? h("span", { class: "bad" }, m.error)
        : m.active ? `активный турнир ${m.active}${m.status ? ` (${m.status})` : ""}${m.teams !== undefined ? `, команд ${m.teams}, игр ${m.games}` : ""}` : "активного турнира нет", ""]);
    }
  } else {
    rows.push(["Синхронизация", "ещё не запускалась", ""]);
  }
  const p = status.pari || {};
  rows.push(["Линия PARI", p.error ? h("span", { class: "bad" }, p.error.slice(0, 200))
    : p.fetched_at ? `событий Dota: ${p.events}` : "ещё не загружалась", p.fetched_at ? fmt.ago(p.fetched_at) : ""]);
  return h("section", { class: "card" },
    h("h2", null, "Источники данных"),
    h("div", { class: "table-wrap" }, h("table", { class: "data" }, h("tbody", null,
      rows.map(([a, b, c]) => h("tr", null, h("td", null, a), h("td", null, b), h("td", { class: "muted nowrap" }, c)))))));
}
