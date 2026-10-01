"""Немного линейной алгебры на чистом Python: признаков у нас единицы, numpy не нужен."""

from __future__ import annotations

import math
from typing import Sequence


def solve(a: list[list[float]], b: list[float]) -> list[float]:
    """Решить A x = b методом Гаусса с выбором главного элемента."""
    n = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(m[r][col]))
        if abs(m[piv][col]) < 1e-12:
            raise ValueError("вырожденная матрица")
        m[col], m[piv] = m[piv], m[col]
        for r in range(n):
            if r != col and m[r][col]:
                f = m[r][col] / m[col][col]
                for c in range(col, n + 1):
                    m[r][c] -= f * m[col][c]
    return [m[i][n] / m[i][i] for i in range(n)]


def ridge(x: Sequence[Sequence[float]], y: Sequence[float], lam: float,
          weights: Sequence[float] | None = None) -> list[float]:
    """Гребневая регрессия без свободного члена: (XᵀWX + λI) w = XᵀWy."""
    k = len(x[0])
    w = weights or [1.0] * len(y)
    a = [[sum(wi * row[i] * row[j] for row, wi in zip(x, w)) + (lam if i == j else 0.0)
          for j in range(k)] for i in range(k)]
    b = [sum(wi * row[i] * yi for row, yi, wi in zip(x, y, w)) for i in range(k)]
    return solve(a, b)


def _sigmoid(z: float) -> float:
    if z >= 0:
        return 1.0 / (1.0 + math.exp(-z))
    e = math.exp(z)
    return e / (1.0 + e)


def logistic(x: Sequence[Sequence[float]], y: Sequence[float], l2: float = 1.0,
             weights: Sequence[float] | None = None, intercept: bool = False,
             iters: int = 50) -> list[float]:
    """Логистическая регрессия методом Ньютона с L2 (свободный член не штрафуется).

    Если intercept=True, последний коэффициент — свободный член.
    """
    rows = [list(r) + [1.0] for r in x] if intercept else [list(r) for r in x]
    k = len(rows[0])
    sw = weights or [1.0] * len(rows)
    beta = [0.0] * k
    pen = [l2] * k
    if intercept:
        pen[-1] = 0.0
    for _ in range(iters):
        grad = [-pen[i] * beta[i] for i in range(k)]
        hess = [[(pen[i] if i == j else 0.0) for j in range(k)] for i in range(k)]
        for row, yi, wi in zip(rows, y, sw):
            p = _sigmoid(sum(b * v for b, v in zip(beta, row)))
            g = wi * (yi - p)
            h = wi * p * (1 - p)
            for i in range(k):
                grad[i] += g * row[i]
                hi = h * row[i]
                for j in range(i, k):
                    hess[i][j] += hi * row[j]
        for i in range(k):
            for j in range(i):
                hess[i][j] = hess[j][i]
        step = solve(hess, grad)
        beta = [b + s for b, s in zip(beta, step)]
        if max(abs(s) for s in step) < 1e-9:
            break
    return beta


def mean_std(values: Sequence[float]) -> tuple[float, float]:
    n = len(values)
    if n == 0:
        return 0.0, 1.0
    mu = sum(values) / n
    var = sum((v - mu) ** 2 for v in values) / max(n - 1, 1)
    return mu, math.sqrt(var) or 1.0
