// Окно замены: кто выбыл → кто пришёл (поиск по всем игрокам и очереди) → сохранить.

import { api, qs } from "./api.js";
import { debounce, fmt, h, modalHead, nGames, openModal, toast } from "./ui.js";

const playerKey = (p) => String(p.account_id ?? `q:${p.queue_uuid}`);

export function openSubDialog({ team, tournamentId, presetOut = null, onDone }) {
  let outAccount = presetOut;
  let incoming = null;

  openModal((close) => {
    const outList = h("div", { class: "pick-list" });
    const results = h("div", { class: "search-results pick-list" });
    const chosen = h("div", { class: "muted", style: { marginTop: "8px" } });
    const note = h("input", { type: "text", placeholder: "Например: «болеет, на 2 игры»" });
    const save = h("button", { type: "button", class: "btn primary", disabled: true, onclick: submit }, "Сохранить замену");

    function paintOut() {
      outList.replaceChildren(...team.players.map((p) => h("button", {
        type: "button", class: `pick-item${p.account_id === outAccount ? " selected" : ""}`,
        onclick: () => { outAccount = p.account_id; paintOut(); validate(); } },
        h("div", { class: "grow" }, h("b", null, p.name),
          h("span", { class: "muted" }, `${fmt.num(p.mmr)} MMR · ${fmt.roles(p.roles) || "роли не указаны"}`)),
        h("span", { class: "badge" }, p.games ? nGames(p.games) : "нет игр"))));
    }

    function validate() {
      save.disabled = !(outAccount && incoming);
      chosen.replaceChildren(incoming
        ? h("span", null, "Пришёл: ", h("b", { style: { color: "var(--text)" } }, incoming.name),
          ` · ${fmt.num(incoming.mmr)} MMR`)
        : "Найдите и выберите игрока, который пришёл на замену");
    }

    const search = debounce(async (q) => {
      if (q.trim().length < 2) { results.replaceChildren(); return; }
      try {
        const data = await api(`/api/players/search${qs({ q, tournament_id: tournamentId })}`);
        const lineup = new Set(team.players.map((p) => p.account_id));
        results.replaceChildren(...data.players.map((p) => {
          const inTeam = p.account_id !== null && lineup.has(p.account_id);
          const meta = [`${fmt.num(p.mmr)} MMR`, p.games ? `${nGames(p.games)} в истории` : "нет истории"];
          if (p.queue_position) meta.push(`очередь №${p.queue_position}`);
          return h("button", { type: "button", disabled: inTeam, dataset: { key: playerKey(p) },
            class: `pick-item${incoming && playerKey(incoming) === playerKey(p) ? " selected" : ""}`,
            onclick: () => { incoming = p; validate(); paintSelected(); } },
            h("div", { class: "grow" }, h("b", null, p.name), h("span", { class: "muted" }, meta.join(" · "))),
            inTeam ? h("span", { class: "badge" }, "уже в составе")
              : p.team ? h("span", { class: "badge b" }, p.team) : null);
        }));
        if (!data.players.length) results.append(h("div", { class: "empty" }, "Никого не нашлось"));
      } catch (e) {
        toast(e.message, "err");
      }
    }, 250);

    function paintSelected() {
      const key = incoming ? playerKey(incoming) : null;
      results.querySelectorAll(".pick-item").forEach((el) => el.classList.toggle("selected", el.dataset.key === key));
    }

    async function submit() {
      save.disabled = true;
      try {
        await api("/api/rosters/change", { method: "POST", body: {
          tournament_id: tournamentId, team_key: team.key, out_account: outAccount,
          in_account: incoming.account_id, in_name: incoming.account_id ? null : incoming.name,
          in_mmr: incoming.mmr, note: note.value || null } });
        const outName = team.players.find((p) => p.account_id === outAccount).name;
        toast(`${team.name}: ${outName} → ${incoming.name}`, "ok");
        close();
        if (onDone) onDone();
      } catch (e) {
        toast(e.message, "err");
        save.disabled = false;
      }
    }

    paintOut();
    validate();
    return h("div", null,
      modalHead(`Замена в ${team.name}`, close),
      h("h3", { class: "muted", style: { fontSize: "12px", margin: "0 0 8px" } }, "КТО ВЫБЫЛ"),
      outList,
      h("h3", { class: "muted", style: { fontSize: "12px", margin: "16px 0 8px" } }, "КТО ПРИШЁЛ"),
      h("input", { type: "search", placeholder: "Ник игрока (от 2 букв)", oninput: (e) => search(e.target.value) }),
      results, chosen,
      h("label", { class: "field", style: { marginTop: "12px" } }, h("span", null, "Комментарий"), note),
      h("div", { class: "row", style: { marginTop: "14px", justifyContent: "flex-end" } },
        h("button", { type: "button", class: "btn ghost", onclick: close }, "Отмена"), save));
  });
}
