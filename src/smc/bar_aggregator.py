from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from typing import Any, Iterator, List, Optional, Protocol, Sequence


# ---------------------------------------------------------------------------
# Domain types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Bar:
    """A single OHLCV bar."""
    timestamp: datetime.datetime
    open: float
    high: float
    low: float
    close: float
    volume: float

    def __post_init__(self) -> None:
        if self.high < self.low:
            raise ValueError(
                f"Bar invariant violated: high ({self.high}) < low ({self.low})"
            )


@dataclass
class BarSeries:
    """An ordered, typed collection of Bar objects for one symbol."""
    symbol: str
    timeframe: str
    bars: List[Bar] = field(default_factory=list)

    # ------------------------------------------------------------------ #
    # Convenience helpers                                                  #
    # ------------------------------------------------------------------ #

    def __len__(self) -> int:
        return len(self.bars)

    def __iter__(self) -> Iterator[Bar]:
        return iter(self.bars)

    def __getitem__(self, index: int) -> Bar:
        return self.bars[index]

    @property
    def is_empty(self) -> bool:
        return len(self.bars) == 0

    @property
    def opens(self) -> List[float]:
        return [b.open for b in self.bars]

    @property
    def highs(self) -> List[float]:
        return [b.high for b in self.bars]

    @property
    def lows(self) -> List[float]:
        return [b.low for b in self.bars]

    @property
    def closes(self) -> List[float]:
        return [b.close for b in self.bars]

    @property
    def volumes(self) -> List[float]:
        return [b.volume for b in self.bars]

    @property
    def timestamps(self) -> List[datetime.datetime]:
        return [b.timestamp for b in self.bars]


# ---------------------------------------------------------------------------
# Client protocol (structural subtyping – no hard Alpaca dependency)
# ---------------------------------------------------------------------------

class BarsResponse(Protocol):
    """
    Minimal protocol that the raw Alpaca StockBarsResponse (or any fixture)
    must satisfy.  Only the ``__iter__`` method is required so that callers
    can iterate over bar-like objects.
    """

    def __iter__(self) -> Iterator[Any]:  # yields bar-like objects
        ...


class BarClient(Protocol):
    """
    Structural protocol for any client that can return bars.

    Compatible with the real ``alpaca.data.historical.StockHistoricalDataClient``
    as well as hand-rolled fixture objects used in offline tests.
    """

    def get_stock_bars(self, request: Any) -> BarsResponse:
        ...


# ---------------------------------------------------------------------------
# Request helper (avoids hard-importing alpaca in test environments)
# ---------------------------------------------------------------------------

def _build_alpaca_request(
    symbol: str,
    timeframe: str,
    start: datetime.datetime,
    end: datetime.datetime,
    limit: Optional[int],
) -> Any:
    """
    Build a ``StockBarsRequest`` when the alpaca SDK is available,
    otherwise raise ``ImportError`` with a helpful message.
    """
    try:
        from alpaca.data.requests import StockBarsRequest  # type: ignore
        from alpaca.data.timeframe import TimeFrame, TimeFrameUnit  # type: ignore
    except ImportError as exc:
        raise ImportError(
            "alpaca-trade-api / alpaca-py is not installed. "
            "Either install it or inject a fixture client."
        ) from exc

    # Map common shorthand strings to Alpaca TimeFrame objects
    _TF_MAP: dict[str, Any] = {
        "1Min": TimeFrame(1, TimeFrameUnit.Minute),
        "5Min": TimeFrame(5, TimeFrameUnit.Minute),
        "15Min": TimeFrame(15, TimeFrameUnit.Minute),
        "30Min": TimeFrame(30, TimeFrameUnit.Minute),
        "1Hour": TimeFrame(1, TimeFrameUnit.Hour),
        "4Hour": TimeFrame(4, TimeFrameUnit.Hour),
        "1Day": TimeFrame(1, TimeFrameUnit.Day),
        "1Week": TimeFrame(1, TimeFrameUnit.Week),
        "1Month": TimeFrame(1, TimeFrameUnit.Month),
    }

    tf_obj = _TF_MAP.get(timeframe)
    if tf_obj is None:
        raise ValueError(
            f"Unknown timeframe '{timeframe}'. "
            f"Supported values: {sorted(_TF_MAP.keys())}"
        )

    kwargs: dict[str, Any] = dict(
        symbol_or_symbols=symbol,
        timeframe=tf_obj,
        start=start,
        end=end,
    )
    if limit is not None:
        kwargs["limit"] = limit

    return StockBarsRequest(**kwargs)


# ---------------------------------------------------------------------------
# Bar-object normalisation
# ---------------------------------------------------------------------------

def _normalise_bar(raw: Any, tz_fallback: datetime.timezone) -> Optional[Bar]:
    """
    Convert a raw bar object from either the Alpaca SDK or a fixture dict/object
    into a domain ``Bar``.

    Returns ``None`` if the raw bar is ``None`` or cannot be parsed, so that the
    aggregator can skip null bars without crashing.
    """
    if raw is None:
        return None

    # --- timestamp -----------------------------------------------------------
    ts: Optional[datetime.datetime] = None

    if isinstance(raw, dict):
        raw_ts = raw.get("timestamp") or raw.get("t")
        o = float(raw.get("open", raw.get("o", 0)))
        h = float(raw.get("high", raw.get("h", 0)))
        l = float(raw.get("low", raw.get("l", 0)))
        c = float(raw.get("close", raw.get("c", 0)))
        v = float(raw.get("volume", raw.get("v", 0)))
    else:
        # Alpaca SDK model or custom fixture object
        raw_ts = getattr(raw, "timestamp", None) or getattr(raw, "t", None)
        o = float(getattr(raw, "open", getattr(raw, "o", 0)))
        h = float(getattr(raw, "high", getattr(raw, "h", 0)))
        l = float(getattr(raw, "low", getattr(raw, "l", 0)))
        c = float(getattr(raw, "close", getattr(raw, "c", 0)))
        v = float(getattr(raw, "volume", getattr(raw, "v", 0)))

    if raw_ts is None:
        return None

    if isinstance(raw_ts, datetime.datetime):
        ts = raw_ts if raw_ts.tzinfo else raw_ts.replace(tzinfo=tz_fallback)
    elif isinstance(raw_ts, (int, float)):
        ts = datetime.datetime.fromtimestamp(raw_ts, tz=tz_fallback)
    elif isinstance(raw_ts, str):
        for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d"):
            try:
                ts = datetime.datetime.strptime(raw_ts, fmt)
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=tz_fallback)
                break
            except ValueError:
                continue
        if ts is None:
            return None
    else:
        return None

    # Guard against invalid OHLC
    try:
        return Bar(timestamp=ts, open=o, high=h, low=l, close=c, volume=v)
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_bars(
    symbol: str,
    timeframe: str,
    start: datetime.datetime,
    end: datetime.datetime,
    *,
    client: Optional[BarClient] = None,
    limit: Optional[int] = None,
    api_key: str = "",
    secret_key: str = "",
    feed: str = "iex",
) -> BarSeries:
    """
    Fetch a ``BarSeries`` for *symbol* over the half-open interval [start, end).

    Parameters
    ----------
    symbol:
        Ticker symbol, e.g. ``"AAPL"``.
    timeframe:
        One of ``"1Min"``, ``"5Min"``, ``"15Min"``, ``"30Min"``,
        ``"1Hour"``, ``"4Hour"``, ``"1Day"``, ``"1Week"``, ``"1Month"``.
    start:
        Inclusive start of the window (timezone-aware recommended).
    end:
        Exclusive end of the window (timezone-aware recommended).
    client:
        A ``BarClient``-compatible object.  When ``None`` the function
        will attempt to construct a real ``StockHistoricalDataClient``
        from the supplied *api_key* / *secret_key*.
    limit:
        Optional maximum number of bars to return.
    api_key / secret_key:
        Alpaca credentials; only used when *client* is ``None``.
    feed:
        Market data feed identifier (``"iex"`` or ``"sip"``).

    Returns
    -------
    BarSeries
        A typed, ordered collection of ``Bar`` objects.  An empty
        ``BarSeries`` is returned when there are no bars in the window
        rather than raising an exception.
    """
    if client is None:
        try:
            from alpaca.data.historical import StockHistoricalDataClient  # type: ignore
        except ImportError as exc:
            raise ImportError(
                "alpaca-py is not installed and no fixture client was injected."
            ) from exc
        client = StockHistoricalDataClient(api_key or None, secret_key or None)

    request = _build_alpaca_request(symbol, timeframe, start, end, limit)

    try:
        response = client.get_stock_bars(request)
    except Exception as exc:  # pragma: no cover – live network errors
        raise RuntimeError(
            f"Failed to fetch bars for '{symbol}' [{timeframe}]: {exc}"
        ) from exc

    tz_fallback = datetime.timezone.utc
    bars: List[Bar] = []

    try:
        raw_iter: Sequence[Any] = list(response)
    except Exception:
        raw_iter = []

    for raw in raw_iter:
        bar = _normalise_bar(raw, tz_fallback)
        if bar is not None:
            bars.append(bar)

    # Sort ascending by timestamp (most APIs already do this, but be defensive)
    bars.sort(key=lambda b: b.timestamp)

    return BarSeries(symbol=symbol, timeframe=timeframe, bars=bars)


# ---------------------------------------------------------------------------
# Offline fixture helpers (importable by tests)
# ---------------------------------------------------------------------------

class FixtureBarClient:
    """
    A drop-in ``BarClient`` that returns pre-canned bar data without any
    network access.  Useful for unit tests and CI environments.

    Usage
    -----
    ::

        client = FixtureBarClient(bars=[
            {"timestamp": "2024-01-02T09:30:00Z", "open": 100, "high": 101,
             "low": 99, "close": 100.5, "volume": 1000},
            None,   # null bars are silently skipped
            {"timestamp": "2024-01-02T09:31:00Z", "open": 100.5, "high": 102,
             "low": 100, "close": 101.0, "volume": 1200},
        ])
        series = get_bars("AAPL", "1Min", start, end, client=client)
    """

    def __init__(self, bars: Optional[List[Any]] = None) -> None:
        self._bars: List[Any] = bars if bars is not None else []

    def get_stock_bars(self, request: Any) -> List[Any]:  # noqa: ARG002
        return self._bars
