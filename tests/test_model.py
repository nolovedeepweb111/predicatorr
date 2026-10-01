from __future__ import annotations

import math
import random
import time

from predicator.backtest import run_backtest
from predicator.dataset import load_dataset
from predicator.features import ELO_START, History, credited_form
from predicator.linalg import logistic, ridge, solve
from predicator.model import train
from predicator.service import series_probs


def test_solve_and_ridge():
    assert [round(v, 6) for v in solve([[2, 1], [1, 3]], [3, 5])] == [0.8, 1.4]
    x = [[i, i % 3] for i in range(30)]
    y = [2 * a - b for a, b in x]
    w = ridge(x, y, lam=1e-9)
    assert abs(w[0] - 2) < 1e-6 and abs(w[1] + 1) < 1e-6


def test_logistic_recovers_signal():
    rng = random.Random(1)
    x, y = [], []
    for _ in range(3000):
        a = rng.gauss(0, 1)
        x.append([a])
        y.append(1.0 if rng.random() < 1 / (1 + math.exp(-1.5 * a)) else 0.0)
    beta = logistic(x, y, l2=0.1)
    assert 1.3 < beta[0] < 1.7


def test_elo_is_zero_sum(conn):
    ds = load_dataset(conn)
    hist = History.build(ds.matches)
    total = sum(st.elo - ELO_START for st in hist.players.values())
    assert abs(total) < 1e-6


def test_credited_form_weights_by_overlap(conn):
    ds = load_dataset(conn)
    m = ds.cups[26].matches[0]
    lineup = list(m.lineup(True))
    wins, games = credited_form([m], lineup)
    assert games == 1.0 and wins == (1.0 if m.radiant_win else 0.0)
    # четыре из пяти: вес (4/5)^3
    wins, games = credited_form([m], lineup[:4] + [-1])
    assert abs(games - 0.8 ** 3) < 1e-9


def test_backtest_beats_coin(conn):
    rep = run_backtest(load_dataset(conn), int(time.time()))
    models = rep["models"]
    assert rep["cups"] == [26, 27, 28]
    assert models["pre_draft"]["accuracy"] > 0.6
    assert models["pre_draft"]["logloss"] < 0.67
    assert models["coin"]["logloss"] > models["pre_draft"]["logloss"]


def test_train_weights_have_expected_signs(conn):
    model = train(load_dataset(conn), int(time.time()))
    gold, elo_max, _, mmr = model.weights
    assert gold > 0 and mmr > 0 and elo_max > 0
    assert model.cups_used == [26, 27, 28]


def test_series_probs_sum_to_one():
    s = series_probs(0.6)
    assert abs(sum(s["three_games"].values()) - 1) < 1e-3
    assert abs(s["bo3_a"] - (0.36 + 2 * 0.36 * 0.4)) < 1e-3
