// Страница составов: все команды турнира, замены и их отмена.

import { api } from "./api.js";
import { clear, fmt, h, nGames, toast } from "./ui.js";
import { loadTeams, navigate, render as rerender, state } from "./app.js";
import { openSubDialog } from "./subdialog.js";

export async function render(view, { isCurrent }) {
  const data = await loadTeams(true);
  if (!isCurrent()) return;
  clear(view);
  const teams = [...data.teams].sort((a, b) => (a.rank || 999) - (b.rank || 999) || a.name.localeCompare(b.name, "ru"));
  const changes = teams.reduce((n, t) => n + t.changes.length, 0);
  view.append(
    h("div", { class: "page-title" }, h("h1", null, "Составы"),
      h("span", { class: "muted" }, `${teams.length} команд · сыграно: ${nGames(data.games_played)} · ручных замен: ${changes}`)),
    h("p", { class: "muted", style: { marginTop: 0 } },
      "Состав берётся из данных турнира и обновляется сам. Если знаете о замене раньше — отметьте её здесь: ",
      "прогнозы сразу считаются по новой пятёрке. Когда источник узнает о той же замене, запись просто станет пустой."));

  const grid = h("div", { class: "teams-grid" });
  view.append(grid);
  for (const team of teams) grid.append(teamCard(team));
}

function teamCard(team) {
  const card = h("section", { class: "card team-card" });
  card.append(h("div", { class: "row", style: { marginBottom: "8px" } },
    h("h2", { class: "grow", style: { margin: 0 } }, team.rank ? h("span", { class: "rank" }, `#${team.rank}`) : null, team.name),
    team.letter ? h("span", { class: "badge" }, `команда ${team.letter}`) : null));
  if (team.complete) {
    card.append(h("div", { class: "team-meta" },
      h("span", { class: "badge" }, `оценка ${team.score.toFixed(3)}`),
      team.form.games > 0.05 ? h("span", { class: `badge ${team.form.adj >= 0 ? "good" : "bad"}` },
        `форма ${team.form.adj >= 0 ? "+" : ""}${team.form.adj.toFixed(2)} · ${team.form.wins.toFixed(1)} из ${team.form.games.toFixed(1)}`) : null));
  } else {
    card.append(h("div", { class: "notice", style: { marginBottom: "8px" } }, "Неполный состав — прогноз недоступен"));
  }
  const rows = h("div");
  for (const p of team.players) {
    rows.append(h("div", { class: "roster-row" },
      h("div", { style: { minWidth: 0 } },
        h("b", null, p.name), p.substitute ? h("span", { class: "badge b", style: { marginLeft: "6px" } }, "замена") : null,
        h("div", { class: "muted", style: { fontSize: "12px" } }, [fmt.roles(p.roles), p.games ? `${nGames(p.games)}, ${fmt.pct(p.winrate, 0)} побед` : "нет игр в лиге"].filter(Boolean).join(" · "))),
      h("span", { class: "num muted nowrap" }, `${fmt.num(p.mmr)} MMR`)));
  }
  card.append(rows);
  card.append(h("div", { class: "row", style: { marginTop: "12px" } },
    h("button", { type: "button", class: "btn small", onclick: () => openSubDialog({
      team, tournamentId: state.tournamentId, onDone: () => rerender() }) }, "Замена"),
    team.complete ? h("button", { type: "button", class: "btn small ghost",
      onclick: () => navigate(`#/predict?a=${encodeURIComponent(team.key)}`) }, "Прогноз") : null));
  if (team.changes.length) {
    card.append(h("div", { class: "changes" }, team.changes.map((c) => h("div", { class: `change${c.applied ? "" : " void"}` },
      h("span", null, `${c.out_name || c.out_account} → ${c.in_name || c.in_account}`,
        c.note ? ` (${c.note})` : "", h("span", { class: "faint" }, ` · ${c.created_at.slice(5, 16)}`),
        c.applied ? null : h("span", { class: "faint" }, " · уже не действует")),
      h("button", { type: "button", class: "btn small ghost danger", onclick: async () => {
        try {
          await api(`/api/rosters/change/${c.id}`, { method: "DELETE" });
          toast("Замена отменена", "ok");
          rerender();
        } catch (e) {
          toast(e.message, "err");
        }
      } }, "Отменить")))));
  }
  return card;
}
