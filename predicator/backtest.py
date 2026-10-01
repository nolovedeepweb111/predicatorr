"""Проверка модели протоколом из заметки.

1. Признаки состава каждой команды — строго по играм до начала её кубка.
2. Обучение без одного кубка, прогноз этого кубка (leave-one-cup-out).
3. Форма и драфт — только по играм раньше прогнозируемой.
4. Метрики: доля угаданных победителей, logloss, калибровка.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from .dataset import Dataset
from .external import ExternalData
from .features import credited_form, sigmoid
from .linalg import logistic
from .model import (LEAGUE_FEATURES, Params, active_features, fit_roster_weights, finished_cups,
                    prepare_cup, roster_score, training_rows)


@dataclass
class Score:
    n: int = 0
    hits: int = 0
    loss: float = 0.0
    buckets: dict[int, list[float]] = field(default_factory=dict)

    def add(self, p: float, y: float) -> None:
        p = min(max(p, 1e-6), 1 - 1e-6)
        self.n += 1
        self.hits += int((p > 0.5) == (y == 1.0))
        self.loss += -(y * math.log(p) + (1 - y) * math.log(1 - p))
        # калибровка по вероятности фаворита
        fav = max(p, 1 - p)
        won = y if p >= 0.5 else 1 - y
        b = self.buckets.setdefault(min(int(fav * 10), 9), [0, 0.0, 0.0])
        b[0] += 1
        b[1] += fav
        b[2] += won

    @property
    def accuracy(self) -> float:
        return self.hits / self.n if self.n else 0.0

    @property
    def logloss(self) -> float:
        return self.loss / self.n if self.n else 0.0

    def as_dict(self) -> dict:
        return {
            "matches": self.n, "accuracy": round(self.accuracy, 4),
            "logloss": round(self.logloss, 4),
            "calibration": [
                {"from": k / 10, "to": (k + 1) / 10, "matches": int(v[0]),
                 "predicted": round(v[1] / v[0], 3), "actual": round(v[2] / v[0], 3)}
                for k, v in sorted(self.buckets.items()) if v[0]],
        }


def run_backtest(ds: Dataset, now: int, params: Params = Params(),
                 ext: ExternalData | None = None) -> dict:
    mmr = {a: p.mmr for a, p in ds.players.items()}
    cups = {c.tournament_id: prepare_cup(ds, c, params, mmr) for c in finished_cups(ds, now)}
    loco = {t: fit_roster_weights([c for u, c in cups.items() if u != t], params) for t in cups}
    rows = training_rows(ds, cups, params, loco, ext)
    full = active_features(params, ext)
    variants = {"draft_league": [f for f in full if f in LEAGUE_FEATURES], "draft": list(full)}

    total = {"coin": Score(), "doc": Score(), "roster": Score(), "pre_draft": Score(),
             "draft_league": Score(), "draft": Score()}
    if variants["draft_league"] == variants["draft"]:
        del total["draft_league"], variants["draft_league"]
    per_cup: dict[int, dict[str, Score]] = {}
    for held, c in cups.items():
        cup_scores = per_cup.setdefault(held, {k: Score() for k in total})
        train = [r for r in rows.values() if r[0] != held]
        coefs = {k: logistic([[z, *(d[f] for f in feats)] for (_, z, d, _) in train],
                             [r[3] for r in train], l2=params.draft_l2)
                 for k, feats in variants.items()}
        for i, m in enumerate(c.cup.matches):
            _, z, d, y = rows[m.match_id]
            fs = c.match_feats[i]
            s_r = roster_score(loco[held], c.norm, fs[True])
            s_d = roster_score(loco[held], c.norm, fs[False])
            # модель из заметки: зачтённые победы как «виртуальные игры»
            blend = {}
            for radiant, s in ((True, s_r), (False, s_d)):
                wins, games = credited_form(c.cup.matches[:i], m.lineup(radiant), 3.0)
                blend[radiant] = (s * 10 + wins) / (10 + games)
            preds = {
                "coin": 0.5,
                "doc": sigmoid(6 * (blend[True] - blend[False])),
                "roster": sigmoid(params.scale * (s_r - s_d)),
                "pre_draft": sigmoid(z),
                **{k: sigmoid(coefs[k][0] * z + sum(a * d[f] for a, f in zip(coefs[k][1:], feats)))
                   for k, feats in variants.items()},
            }
            for k, p in preds.items():
                total[k].add(p, y)
                cup_scores[k].add(p, y)

    return {
        "cups": sorted(cups),
        "draft_features": list(full),
        "models": {k: v.as_dict() for k, v in total.items()},
        "per_cup": {t: {k: {"accuracy": round(s.accuracy, 4), "logloss": round(s.logloss, 4)}
                        for k, s in v.items()} for t, v in per_cup.items()},
    }


LABELS = {
    "coin": "монетка",
    "doc": "модель из заметки (состав + зачтённые победы)",
    "roster": "только состав",
    "pre_draft": "состав + форма с учётом соперника (до драфта)",
    "draft_league": "+ драфт только по лиге",
    "draft": "+ драфт: лига, мета, матчапы, опыт в рейтинге",
}


def format_report(rep: dict) -> str:
    lines = [f"Кубки: {', '.join(map(str, rep['cups']))}", ""]
    lines.append(f"{'модель':<50} {'матчей':>7} {'угадано':>8} {'logloss':>8}")
    for k, v in rep["models"].items():
        # без внешних данных полный драфт совпадает с драфтом по лиге
        label = LABELS["draft_league" if k == "draft" and "draft_league" not in rep["models"] else k]
        lines.append(f"{label:<50} {v['matches']:>7} {v['accuracy']:>8.1%} {v['logloss']:>8.4f}")
    lines.append("")
    lines.append("По кубкам (угадано / logloss): до драфта | с драфтом")
    for t, v in rep["per_cup"].items():
        lines.append(f"  {t:>6}: {v['pre_draft']['accuracy']:.1%} / {v['pre_draft']['logloss']:.3f}"
                     f" | {v['draft']['accuracy']:.1%} / {v['draft']['logloss']:.3f}")
    lines.append("")
    lines.append("Калибровка с драфтом (вероятность фаворита → как часто он выигрывал):")
    for b in rep["models"]["draft"]["calibration"]:
        lines.append(f"  {b['from']:.0%}–{b['to']:.0%}: {b['matches']:>4} игр,"
                     f" прогноз {b['predicted']:.1%}, факт {b['actual']:.1%}")
    return "\n".join(lines)
