// Эксперимент: виртуальные ставки по нескольким стратегиям, у каждой свой банк.

import { api } from "./api.js";
import { clear, fmt, h, toast } from "./ui.js";
import { render as rerender } from "./app.js";

const STAGE = { open: "первая цена в линии", close: "цена перед началом", draft: "окно драфта" };
const SVG = "http://www.w3.org/2000/svg";

export async function render(view, { isCurrent }) {
  const data = await api("/api/paper");
  if (!isCurrent()) return;
  clear(view);
  view.append(h("div", { class: "page-title" }, h("h1", null, "Эксперимент"),
    h("span", { class: "muted" }, `виртуальные ставки с ${fmt.time(data.started_at)}, у каждой стратегии свой банк ${fmt.money(data.start_bank)} ₽`)));
  view.append(h("div", { class: "stack" }, board(data), betsCard(data), rulesCard(data)));
}

// Общая шкала для всех графиков банка — чтобы размах стратегий сравнивался честно.
function domain(data) {
  let lo = data.start_bank;
  let hi = data.start_bank;
  for (const s of data.strategies) for (const [, v] of s.history) { lo = Math.min(lo, v); hi = Math.max(hi, v); }
  const pad = Math.max((hi - lo) * 0.08, data.start_bank * 0.01);
  return [lo - pad, hi + pad];
}

function spark(s, data, dom) {
  const W = 132;
  const H = 34;
  const pts = [[data.started_at, data.start_bank], ...s.history];
  const x = (i) => 4 + (pts.length > 1 ? (i / (pts.length - 1)) * (W - 8) : (W - 8) / 2);
  const y = (v) => H - 4 - ((v - dom[0]) / (dom[1] - dom[0])) * (H - 8);
  const svg = document.createElementNS(SVG, "svg");
  const add = (tag, attrs, title) => {
    const el = document.createElementNS(SVG, tag);
    for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
    if (title) el.append(Object.assign(document.createElementNS(SVG, "title"), { textContent: title }));
    svg.append(el);
    return el;
  };
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  svg.setAttribute("class", "spark");
  svg.setAttribute("role", "img");
  svg.setAttribute("aria-label", `Банк стратегии «${s.name}»: ${fmt.money(Math.round(s.bank))} ₽`);
  add("line", { x1: 4, x2: W - 4, y1: y(data.start_bank), y2: y(data.start_bank), stroke: "var(--muted)",
    "stroke-width": 1, "stroke-dasharray": "3 3", opacity: 0.6 });
  if (pts.length > 1) {
    add("polyline", { points: pts.map(([, v], i) => `${x(i)},${y(v)}`).join(" "), fill: "none",
      stroke: "var(--team-a)", "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round" });
  }
  const last = pts.length - 1;
  add("circle", { cx: x(last), cy: y(pts[last][1]), r: 4, fill: "var(--team-a)", stroke: "var(--surface)", "stroke-width": 2 });
  // зоны наведения крупнее точек: подсказка с банком после каждой закрытой ставки
  pts.forEach(([ts, v], i) => add("circle", { cx: x(i), cy: y(v), r: 7, fill: "transparent" },
    i === 0 ? `старт: ${fmt.money(v)} ₽` : `${fmt.time(ts)}: ${fmt.money(Math.round(v))} ₽`));
  return svg;
}

function board(data) {
  const dom = domain(data);
  const rows = [...data.strategies].sort((a, b) => b.bank - a.bank);
  const signed = (v) => `${v > 0 ? "+" : v < 0 ? "−" : ""}${fmt.money(Math.abs(Math.round(v)))} ₽`;
  return h("section", { class: "card" },
    h("h2", null, "Кто сколько заработал"),
    h("div", { class: "table-wrap" }, h("table", { class: "data cards" },
      h("thead", null, h("tr", null, h("th", null, "Стратегия"), h("th", null, "Банк"), h("th", { class: "num" }, "Прибыль"),
        h("th", { class: "num" }, "ROI"), h("th", { class: "num" }, "Ставки"), h("th", { class: "num" }, "Просадка"))),
      h("tbody", null, rows.map((s) => h("tr", null,
        h("td", { class: "cell-main" }, h("b", null, s.name), h("div", { class: "faint", style: { fontSize: "12px" } }, s.note)),
        h("td", { "data-label": "Банк" }, h("div", { class: "row", style: { gap: "10px", flexWrap: "nowrap" } },
          spark(s, data, dom), h("b", { class: "nowrap" }, `${fmt.money(Math.round(s.bank))} ₽`))),
        h("td", { class: `num ${s.profit > 0 ? "good" : s.profit < 0 ? "bad" : ""}`, "data-label": "Прибыль" }, signed(s.profit)),
        h("td", { class: "num", "data-label": "ROI" }, s.roi === null ? "—" : fmt.signedPct(s.roi)),
        h("td", { class: "num", "data-label": "Ставки" }, h("div", null, `${s.won}–${s.lost}`,
          s.open ? h("div", { class: "faint", style: { fontSize: "12px" } }, `ещё ${s.open} в игре, ${fmt.money(s.open_stake)} ₽`) : null)),
        h("td", { class: "num", "data-label": "Просадка" }, s.max_drawdown ? `−${fmt.pct(s.max_drawdown, 1)}` : "—")))))),
    h("p", { class: "muted", style: { fontSize: "12px", marginBottom: 0 } },
      "Ставки — выигранные–проигранные. ROI — прибыль на поставленный рубль. Просадка — самое большое падение банка от пика. ",
      "Пунктир на графике — стартовый банк, шкала у всех стратегий общая; наведите на точку, чтобы увидеть банк после ставки."));
}

const PAGE = 20;

function betsCard(data) {
  const names = Object.fromEntries(data.strategies.map((s) => [s.code, s.name]));
  const body = h("tbody");
  const more = h("button", { type: "button", class: "btn small ghost" });
  let code = "";
  let shown = PAGE;
  const paint = () => {
    const rows = data.bets.filter((b) => !code || b.strategy === code);
    body.replaceChildren(...rows.slice(0, shown).map(row));
    more.style.display = rows.length > shown ? "" : "none";       // у .btn свой display — hidden не сработает
    more.textContent = `Показать ещё (${rows.length - shown})`;
  };
  more.onclick = () => { shown += PAGE * 2; paint(); };
  const row = (b) => {
    const pick = b.pick === "a" ? b.team_a : b.team_b;
    const result = b.status === "open" ? h("span", { class: "faint" }, "ждём")
      : b.status === "void" ? h("span", { class: "faint" }, "возврат")
        : h("span", { class: b.status === "won" ? "good" : "bad" },
          `${b.status === "won" ? "выигрыш +" : "проигрыш −"}${fmt.money(Math.abs(Math.round(b.profit)))} ₽`);
    return h("tr", null,
      h("td", { class: "nowrap", "data-label": "Когда" }, fmt.time(b.placed_at)),
      h("td", { "data-label": "Стратегия" }, names[b.strategy] || b.strategy),
      h("td", { class: "cell-main" }, `${b.team_a} — ${b.team_b}`, h("div", { class: "faint", style: { fontSize: "12px" } }, STAGE[b.stage] || b.stage)),
      h("td", { "data-label": "Ставка" }, h("span", null, h("b", null, pick), ` по ${fmt.odds(b.odds)}`)),
      h("td", { class: "num", "data-label": "Сумма" }, `${fmt.money(b.stake)} ₽`),
      h("td", { class: "num", "data-label": "Наша / PARI" }, `${fmt.pct(b.prob, 0)} / ${fmt.pct(b.book_prob, 0)}`),
      h("td", { class: "num", "data-label": "Итог" }, result));
  };
  paint();
  const filter = h("select", { class: "input", style: { width: "auto" }, "aria-label": "Стратегия",
    onchange: (e) => { code = e.target.value; shown = PAGE; paint(); } },
  h("option", { value: "" }, "все стратегии"), data.strategies.map((s) => h("option", { value: s.code }, s.name)));
  return h("section", { class: "card" },
    h("div", { class: "row" }, h("h2", { class: "grow", style: { margin: 0 } }, "Виртуальные ставки"), filter),
    data.bets.length
      ? h("div", { class: "table-wrap", style: { marginTop: "10px" } }, h("table", { class: "data cards" },
        h("thead", null, h("tr", null, h("th", null, "Когда"), h("th", null, "Стратегия"), h("th", null, "Матч"), h("th", null, "Ставка"),
          h("th", { class: "num" }, "Сумма"), h("th", { class: "num" }, "Наша / PARI"), h("th", { class: "num" }, "Итог"))), body))
      : h("div", { class: "empty" }, "Ставок пока нет: стратегии ждут игр с коэффициентами PARI."),
    h("div", { class: "row", style: { justifyContent: "center", marginTop: "8px" } }, more));
}

function rulesCard(data) {
  const reset = h("button", { type: "button", class: "btn small ghost danger", onclick: async () => {
    if (!confirm("Начать эксперимент заново? Все виртуальные ставки удалятся, банки вернутся к 5 000 ₽.")) return;
    await api("/api/paper/reset", { method: "POST" });
    toast("Эксперимент начат заново", "ok");
    rerender();
  } }, "Начать заново");
  return h("section", { class: "card" },
    h("h2", null, "Правила"),
    h("ul", { style: { margin: "0 0 10px 18px", padding: 0 } },
      data.strategies.map((s) => h("li", null, h("b", null, s.name), ` — ${s.note}.`))),
    h("p", { class: "muted", style: { fontSize: "13px" } },
      "Когда ставят: «На открытии» — по первой цене, с которой игра появилась в линии PARI; «Окно драфта» — по текущей цене ",
      "после полного драфта, когда герои игроков подтверждены, и до горна; остальные — по последней цене перед началом игры. ",
      "Прогноз у всех, кроме «Окна драфта», — до драфта, по составам."),
    h("p", { class: "muted", style: { fontSize: "13px" } },
      "Решение по каждой игре принимается один раз — по тому, что сайт знает в этот момент, — и больше не меняется. ",
      "Ставка на одну игру — одна; размер считается от свободного банка (без денег в незакрытых ставках). ",
      "Деньги виртуальные: принял бы PARI такую ставку по этой цене, эксперимент не знает."),
    h("div", { class: "row", style: { justifyContent: "flex-end" } }, reset));
}
