"""Payouts that failed or came back recently, grouped by why. From PayFlow's API, read with the
incident-agent client (payouts:audit: it reads every merchant's payouts and changes nothing).

A payout is money sent from a merchant's balance to its bank account. It fails when the bank
declines the transfer (the account is wrong or closed: the merchant's details) or the ledger
holds too little; it is returned when the bank sends it back days later. Either way the money is
back in the merchant's balance and the next batch tries again once the cause is fixed.
"""
from collections import defaultdict
from datetime import datetime, timedelta

from pydantic import BaseModel, Field

PAYOUTS = "/api/v1/payouts"
# PayFlow's most per request; the newest first, so a quiet hour's failures are among them.
READ = 100
DEFAULT_HOURS = 1
MAX_HOURS = 48
EXAMPLES = 3
# A failure code is the bank's (the merchant's account) or PayFlow's (its balance) business answer.
MERCHANT_DETAILS = {"invalid_account", "account_closed", "account_frozen"}


class Example(BaseModel):
    id: str
    amount: str
    bank_account: str = Field(description="The destination's token at the bank")
    minutes_ago: int = Field(description="Since it failed or came back")


class FailureGroup(BaseModel):
    status: str = Field(description="FAILED (never paid) or RETURNED (paid, then sent back by the bank)")
    failure_code: str | None
    count: int
    destinations: int = Field(description="How many different destination accounts they went to")
    examples: list[Example]


class FailedPayouts(BaseModel):
    hours: int
    read: int = Field(description="Payouts read, newest first (PayFlow lists at most 100 at a time)")
    failed: int
    returned: int
    paid: int = Field(description="Payouts paid in the window: the bank works for these")
    groups: list[FailureGroup] = Field(description="Most frequent first")
    concerns: list[str] = Field(description="What looks wrong; empty when no payout failed or came back in the window")


def clamp_hours(hours: int) -> int:
    return max(1, min(MAX_HOURS, hours))


def summarise(payouts: list[dict], hours: int, now: datetime) -> FailedPayouts:
    since = now.replace(tzinfo=None) - timedelta(hours=hours)

    def when(payout: dict, field: str) -> datetime | None:
        value = payout.get(field)
        return datetime.fromisoformat(value).replace(tzinfo=None) if value else None

    grouped: dict[tuple[str, str | None], list[dict]] = defaultdict(list)
    paid = 0
    for payout in payouts:
        ended = when(payout, "returnedAt") if payout["status"] == "RETURNED" else when(payout, "failedAt")
        if payout["status"] in ("FAILED", "RETURNED") and ended and ended >= since:
            grouped[(payout["status"], payout.get("failureCode"))].append(payout | {"_ended": ended})
        elif payout["status"] == "PAID" and (when(payout, "paidAt") or since) >= since:
            paid += 1

    groups = []
    for (status, code), items in sorted(grouped.items(), key=lambda pair: -len(pair[1])):
        items.sort(key=lambda item: item["_ended"], reverse=True)
        groups.append(FailureGroup(
            status=status, failure_code=code, count=len(items),
            destinations=len({item.get("bankAccount") for item in items}),
            examples=[Example(id=item["payoutId"], amount=f"{item['amount']:.2f} {item['currency']}",
                              bank_account=item.get("bankAccount") or "", minutes_ago=int((now.replace(tzinfo=None) - item["_ended"]).total_seconds() // 60))
                      for item in items[:EXAMPLES]]))

    failed = sum(group.count for group in groups if group.status == "FAILED")
    returned = sum(group.count for group in groups if group.status == "RETURNED")
    concerns = []
    for group in groups:
        what = "came back from the bank" if group.status == "RETURNED" else "failed"
        about = (" (the merchant's bank details)" if group.failure_code in MERCHANT_DETAILS else "")
        concerns.append(f"{group.count} payout(s) {what} in the last {hours} hour(s) with {group.failure_code or 'no code'}{about}")
    return FailedPayouts(hours=hours, read=len(payouts), failed=failed, returned=returned, paid=paid, groups=groups,
                         concerns=concerns)
