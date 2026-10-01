// Окно выбора героя для игрока: поиск (англ./рус./сленг), атрибуты, любимые герои игрока.

import { fmt, h, heroImg, modalHead, nGames, openModal } from "./ui.js";
import { state } from "./app.js";

const ATTRS = [["any", "Все"], ["str", "Сила"], ["agi", "Ловкость"], ["int", "Интеллект"], ["all", "Универсал"]];

const norm = (s) => (s || "").toLowerCase().replace(/ё/g, "е").trim();

function haystack(hero) {
  return norm([hero.name, hero.name_ru, hero.key.replace(/_/g, " "), ...(hero.aliases || [])].join(" | "));
}

function matchRank(hero, query) {
  if (!query) return 3;
  const names = [hero.name, hero.name_ru, ...(hero.aliases || [])].map(norm);
  if (names.includes(query)) return 0;
  if (names.some((n) => n.startsWith(query) || n.split(/\s+/).some((w) => w.startsWith(query)))) return 1;
  const hay = haystack(hero);
  return query.split(/\s+/).every((t) => hay.includes(t)) ? 2 : -1;
}

function heroStat(hero) {
  if (!hero.games) return "нет игр";
  return `${fmt.pct(hero.wins / hero.games, 0)} · ${nGames(hero.games)}`;
}

export function openHeroPicker({ player, current, taken, onPick }) {
  let attr = "any";
  let query = "";
  let first = null;

  openModal((close) => {
    const grid = h("div", { class: "hero-grid" });
    const search = h("input", { type: "search", placeholder: "Поиск: invoker, инвокер, войд…", autofocus: true,
      oninput: (e) => { query = norm(e.target.value); paint(); },
      onkeydown: (e) => { if (e.key === "Enter" && first) { e.preventDefault(); choose(first); } } });
    const chips = h("div", { class: "chips" }, ATTRS.map(([key, label]) =>
      h("button", { type: "button", class: `chip${key === attr ? " active" : ""}`, onclick: (e) => {
        attr = key;
        chips.querySelectorAll(".chip").forEach((c) => c.classList.remove("active"));
        e.currentTarget.classList.add("active");
        paint();
      } }, label)));

    const choose = (hero) => {
      if (taken.has(hero.id) && hero.id !== current) return;
      onPick(hero.id);
      close();
    };

    const tile = (hero, note) => {
      const isTaken = taken.has(hero.id) && hero.id !== current;
      return h("button", { type: "button", class: `hero-tile${isTaken ? " taken" : ""}`,
        title: `${hero.name} (${hero.name_ru})${isTaken ? " — уже выбран" : ""}`,
        onclick: () => choose(hero) },
        heroImg(hero),
        h("span", { class: "hn" }, hero.name),
        h("span", { class: "hs" }, note || heroStat(hero)));
    };

    const favs = (player.top_heroes || []).map((t) => {
      const hero = state.heroById.get(t.hero_id);
      return hero ? tile(hero, `${nGames(t.games)} · ${fmt.pct(t.wins / t.games, 0)}`) : null;
    }).filter(Boolean);

    function paint() {
      const items = [];
      for (const hero of state.heroes) {
        if (attr !== "any" && hero.attr !== attr) continue;
        const rank = matchRank(hero, query);
        if (rank < 0) continue;
        items.push([rank, hero]);
      }
      items.sort((a, b) => a[0] - b[0] || a[1].name.localeCompare(b[1].name));
      grid.replaceChildren(...items.map(([, hero]) => tile(hero)));
      const firstFree = items.find(([, hero]) => !(taken.has(hero.id) && hero.id !== current));
      first = query && firstFree ? firstFree[1] : null;
      grid.querySelectorAll(".hero-tile").forEach((el, i) => {
        el.classList.toggle("first", Boolean(first) && items[i][1] === first);
      });
      if (!items.length) grid.append(h("div", { class: "empty" }, "Ничего не нашлось"));
    }
    paint();

    return h("div", null,
      modalHead(`Герой для ${player.name}`, close),
      h("div", { class: "row" }, h("div", { class: "grow" }, search),
        current ? h("button", { type: "button", class: "btn small danger", onclick: () => { onPick(null); close(); } }, "Убрать героя") : null),
      favs.length ? h("div", null, h("h3", { class: "muted", style: { fontSize: "12px", margin: "14px 0 0" } },
        "ЧАЩЕ ВСЕГО ИГРАЕТ"), h("div", { class: "fav-heroes" }, favs)) : null,
      h("div", { style: { marginTop: "14px" } }, chips),
      grid);
  }, { wide: true });
}
