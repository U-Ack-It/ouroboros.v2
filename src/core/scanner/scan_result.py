from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ScanResult:
    ticker: str
    asset_class: str
    fvg: Optional[dict]       # None = no imbalance found
    price: float
    scan_ms: float            # wall-clock time for this ticker's fetch
    error: Optional[str]      # non-None if fetch failed

    @property
    def has_fvg(self) -> bool:
        return self.fvg is not None

    @property
    def fvg_type(self) -> str:
        return self.fvg["type"] if self.fvg else "NONE"

    @property
    def fvg_size(self) -> float:
        return self.fvg["size"] if self.fvg else 0.0

    @property
    def fvg_price(self) -> float:
        return self.fvg["price"] if self.fvg else 0.0

    @property
    def ok(self) -> bool:
        return self.error is None
