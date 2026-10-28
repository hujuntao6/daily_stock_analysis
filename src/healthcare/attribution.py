"""Conservative event-to-move read-through labels."""

from __future__ import annotations

from datetime import date
from typing import Iterable

from .calendar import Session
from .models import Attribution, Evidence, Quote


def _matches_symbol(evidence: Evidence, symbol: str) -> bool:
    def normalise(value: str) -> str:
        text = str(value or "").strip().upper()
        if text.endswith(".SH"):
            text = text[:-3] + ".SS"
        if text.endswith(".HK"):
            base = text[:-3]
            return base.zfill(5) + ".HK" if base.isdigit() else text
        if text.endswith((".SS", ".SZ")):
            base, suffix = text.rsplit(".", 1)
            return base.zfill(6) + "." + suffix if base.isdigit() else text
        return text

    symbol_upper = normalise(symbol)
    return any(symbol_upper == normalise(identifier) for identifier in evidence.identifiers)


def attribute_moves(quotes: Iterable[Quote], evidence: Iterable[Evidence], sessions: dict[str, Session]) -> tuple[Attribution, ...]:
    """Assign only the three allowed labels; never claim causality or percentages."""

    all_evidence = tuple(evidence)
    result: list[Attribution] = []
    for quote in quotes:
        session = sessions[quote.market]
        same_day: list[Evidence] = []
        nearby: list[Evidence] = []
        post_close: list[Evidence] = []
        unknown_time: list[Evidence] = []
        for item in all_evidence:
            if not _matches_symbol(item, quote.symbol):
                continue
            if item.source_kind.startswith("clinicaltrials"):
                # Registry updates and completion milestones are forward
                # planning evidence, not proof of an intraday clinical result.
                continue
            # Attribution is based on when the source was published, not a
            # future completion/readout date embedded in its body.
            if not item.published_at:
                continue
            event_day = item.published_at.astimezone(session.close_at.tzinfo).date()
            if item.published_at > session.close_at:
                if item.published_precision in {"date", "unknown"}:
                    unknown_time.append(item)
                else:
                    post_close.append(item)
                continue
            if event_day == quote.session_date:
                # Date-only/unknown timestamps are never promoted to a
                # pre-close explanation.  Same-day after-close items are
                # retained for next-session observation only.
                if item.published_precision in {"timestamp", "minute"} and item.published_at <= session.close_at:
                    same_day.append(item)
                elif item.published_precision in {"date", "unknown"}:
                    unknown_time.append(item)
                else:
                    post_close.append(item)
            elif abs((event_day - quote.session_date).days) <= 3:
                # A same-day publication after the close is deliberately not
                # included in same_day; it may only be a nearby clue.
                nearby.append(item)
        if same_day:
            note = "发现交易日收盘前的同日官方证据；仅作相关性提示，不作因果判断"
            if any(item.event_status == "metadata_only" for item in same_day):
                note += "；部分公告正文不可用，仅标题/元数据不足以解释具体事件"
            result.append(
                Attribution(
                    symbol=quote.symbol,
                    session_date=quote.session_date,
                    status="相关事件",
                    evidence_ids=tuple(item.id for item in same_day),
                    note=note,
                    timing="pre_close",
                )
            )
        elif nearby:
            result.append(
                Attribution(
                    symbol=quote.symbol,
                    session_date=quote.session_date,
                    status="可能相关",
                    evidence_ids=tuple(item.id for item in nearby),
                    note="存在临近交易日官方证据，未建立因果关系；收盘后证据不解释当日涨跌",
                    timing="nearby",
                )
            )
        elif post_close:
            result.append(
                Attribution(
                    symbol=quote.symbol,
                    session_date=quote.session_date,
                    status="未找到解释",
                    evidence_ids=tuple(item.id for item in post_close),
                    note="发现同日盘后或仅有日期精度的官方消息，列入下一交易日观察，不解释当日涨跌",
                    timing="post_close",
                )
            )
        elif unknown_time:
            result.append(
                Attribution(
                    symbol=quote.symbol,
                    session_date=quote.session_date,
                    status="未找到解释",
                    evidence_ids=tuple(item.id for item in unknown_time),
                    note="同日官方记录仅披露日期或发布时间未知，无法判断收盘前后，不解释当日涨跌",
                    timing="unknown_time",
                )
            )
        else:
            result.append(
                Attribution(
                    symbol=quote.symbol,
                    session_date=quote.session_date,
                    status="未找到解释",
                    note="官方证据源未找到可用于当日解释的事件",
                    timing="none",
                )
            )
    return tuple(result)
