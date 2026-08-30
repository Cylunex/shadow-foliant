"""Normalization shared by the Mairui-compatible public API family."""

from __future__ import annotations

from datetime import date, timedelta
import math
from typing import Dict, Iterable, List, Optional

import pandas as pd

from . import _common as C
from ._path_token_api import PathTokenApi


_INTERVALS = {
    "1m": "1", "1min": "1",
    "5m": "5", "5min": "5",
    "15m": "15", "15min": "15",
    "30m": "30", "30min": "30",
    "60m": "60", "60min": "60", "1h": "60",
}


def _num(value, default: float = 0.0) -> float:
    try:
        if value in (None, "", "--"):
            return default
        number = float(value)
        return number if math.isfinite(number) else default
    except (TypeError, ValueError):
        return default


def _first_num(row: dict, names: Iterable[str], default: float = 0.0) -> float:
    for name in names:
        if row.get(name) not in (None, "", "--"):
            return _num(row.get(name), default)
    return default


def _int_num(value, default: int = 0) -> int:
    number = _num(value, float(default))
    return int(number) if math.isfinite(number) else default


def _ts_code(code: str) -> str:
    value = C.norm_code(code)
    if not value.isdigit() or len(value) != 6:
        return ""
    if value.startswith(("4", "8", "92")):
        suffix = "BJ"
    elif value.startswith(("0", "2", "3")):
        suffix = "SZ"
    else:
        suffix = "SH"
    return f"{value}.{suffix}"


class MairuiCompatibleSource:
    def __init__(self, api: PathTokenApi, *, pro_env: Optional[str] = None):
        self.api = api
        self.pro_env = pro_env

    @property
    def provider(self) -> str:
        return self.api.provider

    def available(self) -> bool:
        return self.api.available()

    def _pro_enabled(self) -> bool:
        import os
        if not self.pro_env:
            return False
        return str(os.getenv(self.pro_env, "false")).lower() in {
            "1", "true", "yes", "on",
        }

    @staticmethod
    def _chunks(values: List[str], size: int = 20):
        for offset in range(0, len(values), size):
            yield values[offset:offset + size]

    def quotes(self, codes: List[str]) -> Dict[str, dict]:
        wanted = list(dict.fromkeys(C.norm_code(code) for code in (codes or [])))
        wanted = [code for code in wanted if code.isdigit() and len(code) == 6]
        out: Dict[str, dict] = {}
        for chunk in self._chunks(wanted, 20):
            rows = self.api.get(
                "realtime", ["hsrl", "ssjy_more"],
                {"stock_codes": ",".join(chunk)},
            )
            for index, row in enumerate(rows):
                code = C.norm_code(row.get("dm") or (chunk[index] if len(rows) == len(chunk) else ""))
                if code not in chunk:
                    continue
                price = _first_num(row, ("p", "price", "zxj"))
                previous = _first_num(row, ("yc", "last_close", "pre_close"))
                high = _first_num(row, ("h", "high"))
                low = _first_num(row, ("l", "low"))
                if price <= 0:
                    continue
                change = _first_num(row, ("ud", "change_amt"), price - previous)
                change_pct = _first_num(row, ("pc", "change_pct"))
                if not change_pct and previous:
                    change_pct = change / previous * 100
                out[code] = {
                    "name": str(row.get("mc") or row.get("name") or ""),
                    "price": price,
                    "last_close": previous,
                    "open": _first_num(row, ("o", "open")),
                    "high": high,
                    "low": low,
                    "change_amt": change,
                    "change_pct": change_pct,
                    "amount_wan": _first_num(row, ("cje", "amount")) / 10_000,
                    "turnover_pct": _first_num(row, ("tr", "hs", "turnover_pct")),
                    "pe_ttm": _first_num(row, ("pe", "pe_ttm")),
                    "pb": _first_num(row, ("pb_ratio", "sjl", "pb")),
                    "mcap_yi": _first_num(row, ("sz", "zsz", "mcap")) / 100_000_000,
                    "float_mcap_yi": _first_num(row, ("lt", "float_mcap")) / 100_000_000,
                    "vol_ratio": _first_num(row, ("lb", "vol_ratio")),
                    "amplitude_pct": _first_num(row, ("zf", "amplitude_pct")),
                    "source": self.provider,
                }
        return out

    @staticmethod
    def _volume_multiplier(frame: pd.DataFrame) -> float | None:
        try:
            volume = pd.to_numeric(frame["v"], errors="coerce")
            amount = pd.to_numeric(frame["a"], errors="coerce")
            close = pd.to_numeric(frame["c"], errors="coerce")
            valid = (volume > 0) & (amount > 0) & (close > 0)
            if int(valid.sum()) < 3:
                return None
            ratio = float((amount[valid] / volume[valid] / close[valid]).median())
            if 0.5 <= ratio <= 2.0:
                return 1.0
            if 50.0 <= ratio <= 200.0:
                return 100.0
        except Exception:
            pass
        return None

    def kline(self, code: str, *, interval: str = "5m", count: int = 1000) -> pd.DataFrame:
        frequency = _INTERVALS.get(str(interval).strip().lower())
        ts_code = _ts_code(code)
        if not frequency or not ts_code:
            return pd.DataFrame()
        if frequency == "1" and not self._pro_enabled():
            return pd.DataFrame()
        try:
            wanted = min(max(int(count), 1), 3200)
        except (TypeError, ValueError):
            wanted = 1000
        parts = (["hsstock", "pro", "history", ts_code, "1", "n"]
                 if frequency == "1"
                 else ["hsstock", "history", ts_code, frequency, "n"])
        rows = self.api.get("kline", parts, {"lt": wanted})
        if not rows:
            return pd.DataFrame()
        frame = pd.DataFrame(rows)
        required = {"t", "o", "h", "l", "c", "v"}
        if not required.issubset(frame.columns):
            return pd.DataFrame()
        multiplier = self._volume_multiplier(frame)
        if multiplier is None:
            return pd.DataFrame()
        out = pd.DataFrame({
            "date": pd.to_datetime(frame["t"], errors="coerce"),
            "open": pd.to_numeric(frame["o"], errors="coerce"),
            "high": pd.to_numeric(frame["h"], errors="coerce"),
            "low": pd.to_numeric(frame["l"], errors="coerce"),
            "close": pd.to_numeric(frame["c"], errors="coerce"),
            "volume": pd.to_numeric(frame["v"], errors="coerce") * multiplier,
        })
        if "a" in frame.columns:
            out["amount"] = pd.to_numeric(frame["a"], errors="coerce")
        out = (out.dropna(subset=["date", "close"])
               .drop_duplicates(subset=["date"], keep="last")
               .sort_values("date").tail(wanted).reset_index(drop=True))
        out.attrs.update({
            "provider": self.provider,
            "volume_unit": "shares",
            "quality_status": "ok",
        })
        return out

    def capital_flow(self, code: str, days: int = 120) -> List[dict]:
        try:
            span = min(max(int(days), 1), 365)
        except (TypeError, ValueError):
            span = 120
        end = date.today()
        start = end - timedelta(days=span - 1)
        rows = self.api.get(
            "capital_flow", ["hsstock", "history", "transaction", C.norm_code(code)],
            {"st": start.strftime("%Y%m%d"), "et": end.strftime("%Y%m%d"),
             "lt": span},
        )
        out = []
        for row in rows:
            super_net = _first_num(row, ("super_net",),
                                   _num(row.get("zmbtdcje")) - _num(row.get("zmstdcje")))
            large_net = _first_num(row, ("large_net",),
                                   _num(row.get("zmbddcje")) - _num(row.get("zmsddcje")))
            mid_net = _first_num(row, ("mid_net",),
                                 _num(row.get("zmbzdcje")) - _num(row.get("zmszdcje")))
            small_net = _first_num(row, ("small_net",),
                                   _num(row.get("zmbxdcje")) - _num(row.get("zmsxdcje")))
            out.append({
                "date": str(row.get("t") or row.get("date") or "")[:10],
                "main_net": _first_num(row, ("main_net",), super_net + large_net),
                "small_net": small_net,
                "mid_net": mid_net,
                "large_net": large_net,
                "super_net": super_net,
                "source": self.provider,
            })
        return [row for row in out if row["date"]][-span:]

    def announcements(self, code: str, limit: int = 30) -> List[dict]:
        bounded = min(max(int(limit), 1), 200)
        rows = self.api.get(
            "announcements", ["hsstock", "announcement", C.norm_code(code)],
            {"lt": bounded},
        )
        out = []
        for row in rows[:bounded]:
            title = str(row.get("zt") or row.get("title") or "").strip()
            if not title:
                continue
            out.append({
                "title": title,
                "type": str(row.get("lx") or row.get("type") or ""),
                "date": str(row.get("t") or row.get("date") or "")[:10],
                "url": "",
                "content": str(row.get("nr") or row.get("content") or "")[:8000],
                "company": str(row.get("gs") or row.get("company") or ""),
                "level": str(row.get("jb") or row.get("level") or ""),
                "source": self.provider,
                "official": False,
            })
        return out

    def interactive_qa(self, code: str, limit: int = 20) -> List[dict]:
        bounded = min(max(int(limit), 1), 200)
        rows = self.api.get(
            "interactive_qa", ["hsstock", "interactiveqa", C.norm_code(code)],
            {"lt": bounded},
        )
        out = []
        for row in rows[:bounded]:
            question = str(row.get("q") or row.get("question") or "").strip()
            answer = str(row.get("a") or row.get("answer") or "").strip()
            if not question and not answer:
                continue
            out.append({
                "date": str(row.get("t") or row.get("date") or "")[:10],
                "id": str(row.get("id") or ""),
                "question_time": str(row.get("qt") or ""),
                "question": question[:2000],
                "answer_time": str(row.get("at") or ""),
                "answer": answer[:4000],
                "source": self.provider,
                "reference_only": True,
            })
        return out

    def limit_performance(self, code: str, days: int = 30) -> List[dict]:
        rows = self._feature_history("limit_performance", "limit", code, days)
        return [{
            "date": row.get("date", ""),
            "limit_up_start": str(row.get("su") or ""),
            "limit_up_end": str(row.get("eu") or ""),
            "break_count": _int_num(row.get("bu")),
            "limit_up_amount": _num(row.get("ua")),
            "limit_down_start": str(row.get("sd") or ""),
            "limit_down_end": str(row.get("ed") or ""),
            "limit_down_open_count": _int_num(row.get("bd")),
            "limit_down_amount": _num(row.get("da")),
            "direction": _int_num(row.get("dr")),
            "seal_ratio": _num(row.get("vr")),
            "seal_to_float_ratio": _num(row.get("fr")),
            "board_count": _int_num(row.get("sc")),
            "within_days": _int_num(row.get("dy")),
            "interruption_days": _int_num(row.get("sb")),
            "source": self.provider,
            "reference_only": True,
        } for row in rows]

    def auction_performance(self, code: str, days: int = 30) -> List[dict]:
        rows = self._feature_history("auction_performance", "auction", code, days)
        return [{
            "date": row.get("date", ""),
            "open_auction_volume_lots": _num(row.get("ov")),
            "close_auction_volume_lots": _num(row.get("cv")),
            "after_hours_volume": _num(row.get("fv")),
            "auction_vs_previous_ratio": _num(row.get("bp")),
            "source": self.provider,
            "reference_only": True,
        } for row in rows]

    def _feature_history(self, capability: str, leaf: str,
                         code: str, days: int) -> List[dict]:
        try:
            span = min(max(int(days), 1), 365)
        except (TypeError, ValueError):
            span = 30
        rows = self.api.get(
            capability, ["hsstock", "lup", leaf, C.norm_code(code)],
            # The live endpoint currently rejects st/et despite an earlier
            # documentation note; ``lt`` is the stable bounded history contract.
            {"lt": span},
        )
        out = []
        for raw in rows:
            row = dict(raw)
            row["date"] = str(row.pop("t", row.get("date") or ""))[:19]
            row["source"] = self.provider
            row["reference_only"] = True
            out.append(row)
        return out[-span:]

    def budget_status(self) -> dict:
        return self.api.budget_status()

    def reset_for_tests(self) -> None:
        self.api.reset_for_tests()
