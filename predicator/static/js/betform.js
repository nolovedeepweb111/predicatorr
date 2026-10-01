// Окно записи ставки в журнал.

import { api } from "./api.js";
import { fmt, h, modalHead, openModal, toast } from "./ui.js";

// bet: {tournament_id, team_a_key, team_b_key, team_a, team_b, pick, odds, stake, model_prob_a,
//       book_prob_a, stage, pari_event_id, snapshot}
export function openBetForm(bet, onSaved) {
  let pick = bet.pick || "A";

  openModal((close) => {
    const odds = h("input", { type: "number", step: "0.01", min: "1.01", value: bet.odds || "" });
    const stake = h("input", { type: "number", step: "1", min: "0", value: bet.stake || "" });
    const note = h("input", { type: "text", placeholder: "необязательно" });
    const info = h("div", { class: "muted", style: { fontSize: "13px", marginTop: "8px" } });
    const pickRow = h("div", { class: "chips" });

    const prob = () => (pick === "A" ? bet.model_prob_a : bet.model_prob_a === null || bet.model_prob_a === undefined ? null : 1 - bet.model_prob_a);
    const bookProb = () => (bet.book_prob_a === null || bet.book_prob_a === undefined ? null
      : pick === "A" ? bet.book_prob_a : 1 - bet.book_prob_a);

    function paintPick() {
      pickRow.replaceChildren(...[["A", bet.team_a], ["B", bet.team_b]].map(([side, name]) =>
        h("button", { type: "button", class: `chip${pick === side ? " active" : ""}`, onclick: () => {
          pick = side;
          const k = side === "A" ? bet.odds_a : bet.odds_b;
          if (k) odds.value = k;
          const s = side === "A" ? bet.stake_a : bet.stake_b;
          stake.value = s || "";
          paintPick();
        } }, `${side === "A" ? "П1" : "П2"}: ${name}`)));
      paintInfo();
    }

    function paintInfo() {
      const p = prob();
      const k = Number(odds.value);
      info.replaceChildren(p === null ? "" : `Наша вероятность ${fmt.pct(p)}${k > 1 ? `, ожидание ${fmt.signedPct(p * k - 1)} на рубль` : ""}`);
    }
    odds.addEventListener("input", paintInfo);

    async function submit() {
      const k = Number(odds.value);
      const s = Number(stake.value);
      if (!(k > 1) || !(s > 0)) {
        toast("Укажите коэффициент больше 1 и сумму ставки", "err");
        return;
      }
      try {
        await api("/api/bets", { method: "POST", body: {
          tournament_id: bet.tournament_id, team_a_key: bet.team_a_key, team_b_key: bet.team_b_key,
          team_a: bet.team_a, team_b: bet.team_b, pick, odds: k, stake: s,
          model_prob: prob(), book_prob: bookProb(), stage: bet.stage,
          pari_event_id: bet.pari_event_id || null, note: note.value || null, snapshot: bet.snapshot || {} } });
        toast("Ставка записана в журнал", "ok");
        close();
        if (onSaved) onSaved();
      } catch (e) {
        toast(e.message, "err");
      }
    }

    paintPick();
    return h("div", null,
      modalHead("Записать ставку", close),
      h("div", { class: "muted", style: { marginBottom: "10px" } }, `${bet.team_a} — ${bet.team_b}`),
      pickRow,
      h("div", { class: "odds-grid", style: { marginTop: "12px" } },
        h("label", { class: "field" }, h("span", null, "Коэффициент"), odds),
        h("label", { class: "field" }, h("span", null, "Сумма"), stake)),
      h("label", { class: "field", style: { marginTop: "10px" } }, h("span", null, "Заметка"), note),
      info,
      h("div", { class: "row", style: { marginTop: "14px", justifyContent: "flex-end" } },
        h("button", { type: "button", class: "btn ghost", onclick: close }, "Отмена"),
        h("button", { type: "button", class: "btn primary", onclick: submit }, "Записать")));
  });
}
