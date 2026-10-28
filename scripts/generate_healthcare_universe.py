"""Build a dated A/H/US healthcare universe from an authorised iFinD export.

The script deliberately requires input paths.  It never reads a local iFinD
configuration or secret implicitly, so a refresh can be reproduced from an
explicit export without publishing private data.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any


AS_OF = "2026-10-27"
EXPECTED = {"cn": 515, "hk": 302, "us": 1031}


def _field(row: dict[str, Any], *needles: str) -> str:
    for key, value in row.items():
        key_text = str(key)
        if all(needle in key_text for needle in needles):
            return str(value or "").strip()
    return ""


def _normalise_symbol(symbol: str, market: str) -> str:
    value = symbol.strip().upper()
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
    # iFinD exchange suffixes are not Yahoo Finance suffixes.  Keep the
    # security ticker and drop .O/.N/.A.  Yahoo uses a hyphen for US share
    # classes (e.g. BIO.B.N -> BIO-B); preserve the original export below.
    if value.endswith((".O", ".N", ".A")):
        value = value[:-2]
    value = re.sub(r"\.([A-Z])$", r"-\1", value)
    return value


def _market_defaults(market: str) -> list[str]:
    return {
        "cn": ["cninfo.com.cn", "sse.com.cn", "szse.cn"],
        "hk": ["hkexnews.hk"],
        "us": ["sec.gov"],
    }[market]


def _focus_overrides(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    rows = raw.get("universe", []) if isinstance(raw, dict) else []
    overrides: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict) or row.get("market") not in {"cn", "hk", "us"}:
            continue
        market = row["market"]
        symbol = _normalise_symbol(str(row.get("symbol", "")), market)
        if not symbol:
            continue
        overrides[f"{market}:{symbol}"] = {
            "official_domains": row.get("official_domains") or _market_defaults(market),
            "ir_feeds": row.get("ir_feeds") or [],
            "cik": row.get("cik"),
            "sponsor_aliases": row.get("sponsor_aliases") or [],
            "sponsor_alias_sources": row.get("sponsor_alias_sources") or [],
            "nct_ids": row.get("nct_ids") or [],
            "focus": True,
        }
    return overrides


def _read(path: Path, market: str, focus: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    rows = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(rows, list):
        raise ValueError(f"{path} must contain a JSON list")
    result: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ValueError(f"{path}: row {index} is not an object")
        original = _field(row, "股票代码") or _field(row, "代码")
        name = _field(row, "股票简称") or _field(row, "简称") or _field(row, "名称")
        industry = _field(row, "行业") or _field(row, "industry") or "未分类"
        if not original or not name:
            raise ValueError(f"{path}: row {index} lacks stock code or name")
        symbol = _normalise_symbol(original, market)
        key = f"{market}:{symbol}"
        override = focus.get(key, {})
        result.append(
            {
                "market": market,
                "symbol": symbol,
                "original_symbol": original,
                "name": name,
                "subsector": industry,
                "source": path.name,
                "source_asof": AS_OF,
                "official_domains": override.get("official_domains") or _market_defaults(market),
                "ir_feeds": override.get("ir_feeds") or [],
                "cik": override.get("cik"),
                "sponsor_aliases": override.get("sponsor_aliases") or [],
                "sponsor_alias_sources": override.get("sponsor_alias_sources") or [],
                "nct_ids": override.get("nct_ids") or [],
                "focus": bool(override.get("focus", False)),
            }
        )
    return result


def build(args: argparse.Namespace) -> dict[str, Any]:
    focus = _focus_overrides(Path(args.focus_config)) if args.focus_config else {}
    inputs = {"cn": Path(args.cn), "hk": Path(args.hk), "us_large": Path(args.us_large), "us_small": Path(args.us_small)}
    rows = _read(inputs["cn"], "cn", focus) + _read(inputs["hk"], "hk", focus)
    us_rows = _read(inputs["us_large"], "us", focus) + _read(inputs["us_small"], "us", focus)
    seen: set[str] = set()
    for row in us_rows:
        if row["symbol"] in seen:
            raise ValueError(f"duplicate normalised US symbol: {row['symbol']}")
        seen.add(row["symbol"])
    if len(us_rows) != EXPECTED["us"]:
        raise ValueError(f"expected {EXPECTED['us']} US rows after union, got {len(us_rows)}")
    rows += us_rows
    counts = {market: sum(row["market"] == market for row in rows) for market in ("cn", "hk", "us")}
    if counts["cn"] != EXPECTED["cn"] or counts["hk"] != EXPECTED["hk"]:
        raise ValueError(f"unexpected market counts: {counts}")
    rows.sort(key=lambda row: (row["market"], row["symbol"]))
    return {
        "as_of": AS_OF,
        "source": "iFinD authorised healthcare industry export",
        "classification": {
            "cn": "iFinD 同花顺行业分类，保留原始行业路径",
            "hk": "iFinD 港股行业分类，保留原始行业字段",
            "us": "iFinD 美股同花顺全球行业分类，合并 large/small export",
        },
        "counts": {**counts, "total": len(rows), "us_large": len(_read(inputs["us_large"], "us", focus)), "us_small": len(_read(inputs["us_small"], "us", focus))},
        "inputs": {key: path.name for key, path in inputs.items()},
        "normalisation": {
            "cn": ".SH converted to .SS; .SZ retained",
            "hk": "numeric code converted to int().zfill(4)+.HK",
            "us": ".O/.N/.A exchange suffix removed; share-class dot converted to Yahoo hyphen (BIO.B.N -> BIO-B)",
        },
        "universe": rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cn", required=True, help="authorised A-share JSON export")
    parser.add_argument("--hk", required=True, help="authorised HK JSON export")
    parser.add_argument("--us-large", required=True, help="authorised US large-cap JSON export")
    parser.add_argument("--us-small", required=True, help="authorised US small-cap JSON export")
    parser.add_argument("--output", required=True, help="output healthcare-universe.json")
    parser.add_argument("--focus-config", help="optional config/healthcare.json for domains/CIKs/focus overrides")
    args = parser.parse_args()
    result = build(args)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(output), "counts": result["counts"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
