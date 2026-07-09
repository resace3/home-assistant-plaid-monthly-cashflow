from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime
from typing import Any, Iterable

from dateutil.relativedelta import relativedelta


def classify_direction(amount: float) -> str:
    if amount > 0:
        return "outflow"
    if amount < 0:
        return "inflow"
    return "neutral"


def _parse_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _month_key(value: date) -> str:
    return f"{value.year:04d}-{value.month:02d}"


def _month_range(start: date, end: date) -> list[str]:
    current = start.replace(day=1)
    last = end.replace(day=1)
    months: list[str] = []
    while current <= last:
        months.append(_month_key(current))
        current = current + relativedelta(months=1)
    return months


def monthly_cashflow(
    transactions: Iterable[dict[str, Any]],
    *,
    months_back: int | None = None,
    today: date | None = None,
) -> list[dict[str, Any]]:
    today = today or date.today()
    grouped: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"inflow": 0.0, "outflow": 0.0, "net": 0.0, "transaction_count": 0}
    )
    seen_month_dates: list[date] = []

    for transaction in transactions:
        if int(transaction.get("pending") or 0):
            continue
        if int(transaction.get("removed") or 0):
            continue

        txn_date = _parse_date(transaction.get("date"))
        if txn_date is None:
            continue

        if months_back is not None and months_back > 0:
            first_month = (today.replace(day=1) - relativedelta(months=months_back - 1))
            if txn_date < first_month:
                continue

        amount = float(transaction.get("amount") or 0)
        month = _month_key(txn_date)
        seen_month_dates.append(txn_date)

        if amount < 0:
            grouped[month]["inflow"] += abs(amount)
        elif amount > 0:
            grouped[month]["outflow"] += amount
        grouped[month]["transaction_count"] += 1

    if months_back is not None and months_back > 0:
        range_start = today.replace(day=1) - relativedelta(months=months_back - 1)
        range_end = today.replace(day=1)
    elif seen_month_dates:
        range_start = min(seen_month_dates).replace(day=1)
        range_end = max(seen_month_dates).replace(day=1)
    else:
        return []

    months: list[dict[str, Any]] = []
    for month in _month_range(range_start, range_end):
        values = grouped[month]
        inflow = round(values["inflow"], 2)
        outflow = round(values["outflow"], 2)
        net = round(inflow - outflow, 2)
        months.append(
            {
                "month": month,
                "inflow": inflow,
                "outflow": outflow,
                "net": net,
                "transaction_count": int(values["transaction_count"]),
            }
        )

    return months


def summarize_months(months: list[dict[str, Any]]) -> dict[str, float]:
    count = len(months) or 1
    total_inflow = round(sum(float(month["inflow"]) for month in months), 2)
    total_outflow = round(sum(float(month["outflow"]) for month in months), 2)
    net = round(total_inflow - total_outflow, 2)
    return {
        "total_inflow": total_inflow,
        "total_outflow": total_outflow,
        "net": net,
        "average_monthly_inflow": round(total_inflow / count, 2),
        "average_monthly_outflow": round(total_outflow / count, 2),
        "average_monthly_net": round(net / count, 2),
    }


def top_merchants(
    transactions: Iterable[dict[str, Any]],
    *,
    direction: str = "outflow",
    limit: int = 10,
) -> list[dict[str, Any]]:
    totals: dict[str, dict[str, Any]] = defaultdict(lambda: {"amount": 0.0, "transaction_count": 0})

    for transaction in transactions:
        if int(transaction.get("pending") or 0):
            continue
        if int(transaction.get("removed") or 0):
            continue

        amount = float(transaction.get("amount") or 0)
        txn_direction = classify_direction(amount)
        if direction in {"inflow", "outflow"} and txn_direction != direction:
            continue
        if txn_direction == "neutral":
            continue

        merchant = (
            transaction.get("merchant_name")
            or transaction.get("name")
            or "Unknown merchant"
        )
        totals[str(merchant)]["amount"] += abs(amount)
        totals[str(merchant)]["transaction_count"] += 1

    rows = [
        {
            "merchant": merchant,
            "amount": round(values["amount"], 2),
            "transaction_count": int(values["transaction_count"]),
        }
        for merchant, values in totals.items()
    ]
    rows.sort(key=lambda item: item["amount"], reverse=True)
    return rows[: max(limit, 0)]
