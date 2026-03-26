from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Optional
from urllib import request


USD_TO_JPY = Decimal("150")
EVENTS_FILE = "events.json"


@dataclass
class Subscription:
    service: str
    price: Decimal
    currency: str
    cycle: str
    category: str
    start_date: date

    def monthly_jpy(self) -> Decimal:
        jpy = self._price_in_jpy()
        if self.cycle == "monthly":
            return jpy
        if self.cycle == "yearly":
            return (jpy / Decimal("12")).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        raise ValueError(f"Unsupported cycle: {self.cycle}")

    def yearly_jpy(self) -> Decimal:
        jpy = self._price_in_jpy()
        if self.cycle == "monthly":
            return jpy * Decimal("12")
        if self.cycle == "yearly":
            return jpy
        raise ValueError(f"Unsupported cycle: {self.cycle}")

    def _price_in_jpy(self) -> Decimal:
        if self.currency.upper() == "JPY":
            return self.price
        if self.currency.upper() == "USD":
            return (self.price * USD_TO_JPY).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        raise ValueError(f"Unsupported currency: {self.currency}")


def parse_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def load_events(path: str) -> List[Dict[str, Any]]:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError("events.json must contain a list")
    return sorted(data, key=lambda x: x["date"])


def build_current_subscriptions(events: List[Dict[str, Any]], as_of: date) -> Dict[str, Subscription]:
    active: Dict[str, Subscription] = {}

    for event in events:
        event_date = parse_date(event["date"])
        if event_date > as_of:
            continue

        action = event["action"]
        service = event["service"]

        if action == "subscribe":
            active[service] = Subscription(
                service=service,
                price=Decimal(str(event["price"])),
                currency=event["currency"],
                cycle=event["cycle"],
                category=event.get("category", "Other"),
                start_date=event_date,
            )
        elif action == "cancel":
            if service in active:
                del active[service]
        elif action == "change":
            if service not in active:
                continue
            sub = active[service]
            if "price" in event:
                sub.price = Decimal(str(event["price"]))
            if "currency" in event:
                sub.currency = event["currency"]
            if "cycle" in event:
                sub.cycle = event["cycle"]
            if "category" in event:
                sub.category = event["category"]
        else:
            raise ValueError(f"Unsupported action: {action}")

    return active


def months_remaining_inclusive(target: date) -> int:
    return 12 - target.month + 1


def calculate_year_to_date_actual(events: List[Dict[str, Any]], target: date) -> Decimal:
    total = Decimal("0")
    year = target.year

    for month in range(1, target.month + 1):
        if month == 12:
            month_end = date(year, 12, 31)
        else:
            month_end = date(year, month + 1, 1) - timedelta(days=1)

        active = build_current_subscriptions(events, month_end)

        for sub in active.values():
            if sub.cycle == "monthly":
                total += sub.monthly_jpy()
            elif sub.cycle == "yearly":
                if sub.start_date.year == year and sub.start_date.month == month:
                    total += sub.yearly_jpy()

    return total


def calculate_projection(active: Dict[str, Subscription], target: date) -> tuple[Decimal, Decimal, Decimal]:
    monthly_total = sum((sub.monthly_jpy() for sub in active.values()), Decimal("0"))
    remaining_months = months_remaining_inclusive(target)
    planned_until_dec = monthly_total * Decimal(str(remaining_months))
    annual_forecast = planned_until_dec
    return monthly_total, planned_until_dec, annual_forecast


def format_yen(value: Decimal) -> str:
    return f"¥{int(value):,}"


def build_report(events: List[Dict[str, Any]], target: date) -> str:
    active = build_current_subscriptions(events, target)
    monthly_total, planned_until_dec, annual_forecast = calculate_projection(active, target)
    ytd_actual = calculate_year_to_date_actual(events, target)

    category_totals: Dict[str, Decimal] = {}
    for sub in active.values():
        category_totals[sub.category] = category_totals.get(sub.category, Decimal("0")) + sub.monthly_jpy()

    lines: List[str] = []
    lines.append(f"📡 SubGuard レポート ({target.year}-{target.month:02d})")
    lines.append("")
    lines.append("■ 現在契約中")

    if active:
        for sub in sorted(active.values(), key=lambda x: x.monthly_jpy(), reverse=True):
            original = f"{sub.price} {sub.currency}/{sub.cycle}"
            lines.append(
                f"- {sub.service}: "
                f"{format_yen(sub.monthly_jpy())}/月換算 / "
                f"{format_yen(sub.yearly_jpy())}/年概算 "
                f"({original}, {sub.category})"
            )
    else:
        lines.append("- なし")

    lines.append("")
    lines.append("■ 合計")
    lines.append(f"- 月額換算合計: {format_yen(monthly_total)}")

    lines.append("")
    lines.append("■ カテゴリ別合計（月額換算）")
    for category, total in sorted(category_totals.items(), key=lambda x: x[1], reverse=True):
        lines.append(f"- {category}: {format_yen(total)}")

    lines.append("")
    lines.append("■ 年間")
    lines.append(f"- 今年の累計実績: {format_yen(ytd_actual)}")
    lines.append(f"- 12月までの予定金額: {format_yen(planned_until_dec)}")
    lines.append(f"- 年間合計見込み: {format_yen(ytd_actual + planned_until_dec)}")

    return "\n".join(lines)


def send_discord(webhook_url: str, content: str) -> None:
    payload = json.dumps({"content": content}).encode("utf-8")
    req = request.Request(
        webhook_url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with request.urlopen(req) as resp:
        if resp.status >= 300:
            raise RuntimeError(f"Discord webhook failed: {resp.status}")


def main() -> None:
    events = load_events(EVENTS_FILE)
    today = date.today()
    report = build_report(events, today)
    print(report)

    webhook_url = os.getenv("DISCORD_WEBHOOK_URL")
    if webhook_url:
        send_discord(webhook_url, report)
        print("Discord sent.")
    else:
        print("DISCORD_WEBHOOK_URL not set. Skipped Discord send.")


if __name__ == "__main__":
    main()
