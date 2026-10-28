"""Configuration loading and validation for the healthcare report."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .models import Security, ensure_tuple


class ConfigError(ValueError):
    pass


SUPPORTED_MARKETS = {"cn", "hk", "us"}
SESSION_MARKETS = {"us": ("us",), "ah": ("cn", "hk")}
DEFAULT_BENCHMARKS = {"cn": "000300.SS", "hk": "^HSI", "us": "SPY"}


@dataclass(frozen=True)
class HealthcareConfig:
    timezone: str
    universe: tuple[Security, ...]
    provider: dict[str, Any]
    source_policy: dict[str, Any]
    report: dict[str, Any]
    # Raw rows retain audit metadata (original_symbol/source/source_asof) that
    # is intentionally not added to the small Security domain model.
    raw_universe: tuple[dict[str, Any], ...] = ()
    universe_files: tuple[str, ...] = ()

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    def markets_for_session(self, session: str) -> tuple[str, ...]:
        try:
            return SESSION_MARKETS[session]
        except KeyError as exc:
            raise ConfigError("session must be us or ah") from exc

    def securities_for_session(self, session: str) -> tuple[Security, ...]:
        markets = set(self.markets_for_session(session))
        return tuple(s for s in self.universe if s.market in markets)

    def benchmark_for(self, market: str) -> str:
        benchmarks = self.report.get("benchmarks", {})
        return str(benchmarks.get(market, DEFAULT_BENCHMARKS[market]))


def _string(value: Any, field: str, *, required: bool = True) -> str:
    text = "" if value is None else str(value).strip()
    if required and not text:
        raise ConfigError(f"{field} is required")
    return text


def _canonical_symbol(symbol: Any, market: str) -> str:
    value = str(symbol or "").strip().upper()
    if market == "cn":
        if value.endswith(".SH"):
            value = value[:-3] + ".SS"
        if "." not in value and value.isdigit():
            value += ".SS" if value.startswith(("5", "6")) else ".SZ"
        return value
    if market == "hk":
        code = value.split(".", 1)[0]
        if code.isdigit():
            return f"{int(code):04d}.HK"
        return value
    if value.endswith((".O", ".N", ".A")):
        value = value[:-2]
    return re.sub(r"\.([A-Z])$", r"-\1", value)


def _security(raw: Any, index: int) -> Security:
    if not isinstance(raw, dict):
        raise ConfigError(f"universe[{index}] must be an object")
    market = _string(raw.get("market"), f"universe[{index}].market").lower()
    if market not in SUPPORTED_MARKETS:
        raise ConfigError(f"universe[{index}].market must be cn, hk or us")
    symbol = _canonical_symbol(_string(raw.get("symbol"), f"universe[{index}].symbol"), market)
    name = _string(raw.get("name"), f"universe[{index}].name")
    def list_field(name: str) -> tuple[str, ...]:
        value = raw.get(name)
        if value is None:
            return ()
        if not isinstance(value, list):
            raise ConfigError(f"universe[{index}].{name} must be a list")
        return ensure_tuple(value)

    domains = list_field("official_domains")
    feeds = list_field("ir_feeds")
    cik = _string(raw.get("cik"), f"universe[{index}].cik", required=False) or None
    return Security(
        symbol=symbol,
        name=name,
        market=market,
        subsector=_string(raw.get("subsector"), f"universe[{index}].subsector", required=False) or "未分类",
        official_domains=domains,
        ir_feeds=feeds,
        cik=cik,
        nct_ids=list_field("nct_ids"),
        sponsor_aliases=list_field("sponsor_aliases"),
    )


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"universe file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"invalid JSON in universe file {path}: {exc}") from exc


def _resolve_universe_file(config_path: Path, raw_path: Any) -> Path:
    value = str(raw_path or "").strip()
    if not value:
        raise ConfigError("universe_files entries must be non-empty paths")
    candidate = Path(value)
    if candidate.is_absolute():
        if candidate.is_file():
            return candidate
        raise ConfigError(f"universe file not found: {candidate}")
    # Support both config-relative (healthcare-universe.json) and repository
    # relative (config/healthcare-universe.json) references.  CWD is last so a
    # config copied into a different checkout cannot silently pick a sibling.
    candidates = [
        config_path.parent / candidate,
        config_path.parent.parent / candidate,
        Path.cwd() / candidate,
    ]
    seen: set[Path] = set()
    for item in candidates:
        resolved = item.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        if resolved.is_file():
            return resolved
    raise ConfigError(f"universe file not found: {value} (relative to {config_path.parent} or repository root)")


def _rows_from_universe_file(path: Path) -> list[dict[str, Any]]:
    payload = _read_json(path)
    if isinstance(payload, dict):
        rows = payload.get("universe")
    else:
        rows = payload
    if not isinstance(rows, list):
        raise ConfigError(f"{path} must contain a JSON list or an object with a universe list")
    result: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ConfigError(f"{path}: universe[{index}] must be an object")
        market = str(row.get("market") or "").strip().lower()
        if market not in SUPPORTED_MARKETS:
            raise ConfigError(f"{path}: universe[{index}].market must be cn, hk or us")
        symbol = _canonical_symbol(row.get("symbol"), market)
        if not symbol:
            raise ConfigError(f"{path}: universe[{index}].symbol is required")
        normalised = dict(row)
        normalised["market"] = market
        normalised["symbol"] = symbol
        result.append(normalised)
    return result


def load_config(path: str | Path) -> HealthcareConfig:
    config_path = Path(path)
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"config not found: {config_path}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"invalid JSON in {config_path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError("config root must be an object")

    timezone = _string(raw.get("timezone"), "timezone")
    try:
        ZoneInfo(timezone)
    except Exception as exc:
        raise ConfigError(f"unknown timezone: {timezone}") from exc
    inline_raw = raw.get("universe", [])
    if not isinstance(inline_raw, list):
        raise ConfigError("universe must be a list")

    universe_file_values = raw.get("universe_files", [])
    if universe_file_values is None:
        universe_file_values = []
    if not isinstance(universe_file_values, list) or any(not isinstance(item, (str, Path)) for item in universe_file_values):
        raise ConfigError("universe_files must be a list of paths")

    snapshot_rows: list[dict[str, Any]] = []
    if universe_file_values:
        for value in universe_file_values:
            resolved = _resolve_universe_file(config_path, value)
            snapshot_rows.extend(_rows_from_universe_file(resolved))
        keyed_snapshot: dict[str, dict[str, Any]] = {}
        for row in snapshot_rows:
            key = f"{row['market']}:{row['symbol']}"
            if key in keyed_snapshot:
                raise ConfigError(f"duplicate canonical security in universe_files: {key}")
            keyed_snapshot[key] = row

        merged_rows = [dict(row) for row in snapshot_rows]
        merged_by_key = {f"{row['market']}:{row['symbol']}": row for row in merged_rows}
        focus_symbols: list[str] = []
        for index, item in enumerate(inline_raw):
            if not isinstance(item, dict):
                raise ConfigError(f"universe[{index}] must be an object")
            market = _string(item.get("market"), f"universe[{index}].market").lower()
            if market not in SUPPORTED_MARKETS:
                raise ConfigError(f"universe[{index}].market must be cn, hk or us")
            symbol = _canonical_symbol(_string(item.get("symbol"), f"universe[{index}].symbol"), market)
            key = f"{market}:{symbol}"
            if key not in merged_by_key:
                raise ConfigError(f"inline focus security {key} is not present in universe_files")
            target = merged_by_key[key]
            # Focus metadata may refine the snapshot, but source/as-of and the
            # original vendor symbol remain audit fields from the full export.
            for field, value in item.items():
                if field in {"source", "source_asof", "original_symbol"}:
                    continue
                target[field] = value
            target["market"] = market
            target["symbol"] = symbol
            target["focus"] = True
            if symbol not in focus_symbols:
                focus_symbols.append(symbol)
        universe_raw = merged_rows
    else:
        universe_raw = []
        focus_symbols = []
        for index, item in enumerate(inline_raw):
            if not isinstance(item, dict):
                raise ConfigError(f"universe[{index}] must be an object")
            row = dict(item)
            market = _string(row.get("market"), f"universe[{index}].market").lower()
            if market not in SUPPORTED_MARKETS:
                raise ConfigError(f"universe[{index}].market must be cn, hk or us")
            row["market"] = market
            row["symbol"] = _canonical_symbol(_string(row.get("symbol"), f"universe[{index}].symbol"), market)
            universe_raw.append(row)
            if row.get("focus"):
                focus_symbols.append(row["symbol"])

    # Fail clearly if a config accidentally resolves to an empty snapshot.
    if universe_file_values and not universe_raw:
        raise ConfigError("universe_files resolved but contained no securities")
    universe = tuple(_security(item, i) for i, item in enumerate(universe_raw))
    provider = raw.get("provider") or {}
    source_policy = raw.get("source_policy") or {}
    report = raw.get("report") or {}
    if not isinstance(provider, dict) or not isinstance(source_policy, dict) or not isinstance(report, dict):
        raise ConfigError("provider, source_policy and report must be objects")
    report = dict(report)
    configured_focus = [str(value).strip() for value in report.get("focus_symbols", []) if str(value).strip()]
    report["focus_symbols"] = list(dict.fromkeys([*configured_focus, *focus_symbols]))
    return HealthcareConfig(
        timezone=timezone,
        universe=universe,
        provider=dict(provider),
        source_policy=dict(source_policy),
        report=report,
        raw_universe=tuple(dict(item) for item in universe_raw),
        universe_files=tuple(str(item) for item in universe_file_values),
    )
