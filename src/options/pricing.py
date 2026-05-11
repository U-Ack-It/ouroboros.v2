"""
Black-Scholes option pricer — pure math, no scipy dependency.

Supports European calls and puts, full Greeks, and implied-vol
extraction via bisection method.
"""

import math


# ---------------------------------------------------------------------------
# Normal distribution helpers
# ---------------------------------------------------------------------------

def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


# ---------------------------------------------------------------------------
# Core Black-Scholes
# ---------------------------------------------------------------------------

def _d1_d2(S: float, K: float, T: float, r: float, sigma: float):
    """Returns (d1, d2) or raises ValueError if inputs are degenerate."""
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        raise ValueError(f"Degenerate BS inputs: S={S} K={K} T={T} sigma={sigma}")
    d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    return d1, d2


def bs_call(S: float, K: float, T: float, r: float, sigma: float) -> float:
    """European call price."""
    d1, d2 = _d1_d2(S, K, T, r, sigma)
    return S * _norm_cdf(d1) - K * math.exp(-r * T) * _norm_cdf(d2)


def bs_put(S: float, K: float, T: float, r: float, sigma: float) -> float:
    """European put price."""
    d1, d2 = _d1_d2(S, K, T, r, sigma)
    return K * math.exp(-r * T) * _norm_cdf(-d2) - S * _norm_cdf(-d1)


# ---------------------------------------------------------------------------
# Greeks
# ---------------------------------------------------------------------------

def greeks(S: float, K: float, T: float, r: float, sigma: float, flag: str = "call") -> dict:
    """
    Returns {delta, gamma, theta, vega, rho}.
    theta is per calendar day (divide by 365 from the annual rate).
    vega is per 1% change in IV.
    flag: 'call' or 'put'
    """
    d1, d2 = _d1_d2(S, K, T, r, sigma)
    nd1  = _norm_cdf(d1)
    npd1 = _norm_pdf(d1)
    Ke_rt = K * math.exp(-r * T)
    sqrt_T = math.sqrt(T)

    if flag == "call":
        delta = nd1
        rho   = K * T * math.exp(-r * T) * _norm_cdf(d2) / 100
        theta = (
            -S * npd1 * sigma / (2 * sqrt_T)
            - r * Ke_rt * _norm_cdf(d2)
        ) / 365
    else:
        delta = nd1 - 1.0
        rho   = -K * T * math.exp(-r * T) * _norm_cdf(-d2) / 100
        theta = (
            -S * npd1 * sigma / (2 * sqrt_T)
            + r * Ke_rt * _norm_cdf(-d2)
        ) / 365

    gamma = npd1 / (S * sigma * sqrt_T)
    vega  = S * npd1 * sqrt_T / 100

    return {
        "delta": round(delta, 6),
        "gamma": round(gamma, 6),
        "theta": round(theta, 6),
        "vega":  round(vega, 6),
        "rho":   round(rho, 6),
    }


# ---------------------------------------------------------------------------
# Implied volatility
# ---------------------------------------------------------------------------

def implied_vol(
    market_price: float,
    S: float,
    K: float,
    T: float,
    r: float,
    flag: str = "call",
    tol: float = 1e-6,
    max_iter: int = 200,
) -> float:
    """
    Bisection-method implied volatility.
    Returns IV as a decimal (e.g. 0.25 = 25%).
    Returns 0.0 if it cannot converge.
    """
    if T <= 0 or market_price <= 0:
        return 0.0

    pricer = bs_call if flag == "call" else bs_put
    lo, hi = 1e-6, 10.0  # sigma search range

    # Quick sanity check
    try:
        if pricer(S, K, T, r, lo) > market_price:
            return lo
        if pricer(S, K, T, r, hi) < market_price:
            return hi
    except ValueError:
        return 0.0

    for _ in range(max_iter):
        mid = (lo + hi) / 2.0
        try:
            price = pricer(S, K, T, r, mid)
        except ValueError:
            return 0.0
        if abs(price - market_price) < tol:
            return round(mid, 6)
        if price < market_price:
            lo = mid
        else:
            hi = mid

    return round((lo + hi) / 2.0, 6)
