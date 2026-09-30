"""One small offline check: no keys, no network, no live LLM requests."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from chat2order.agents import CallBudget
from chat2order.data import default_business, demo_inbox, now_local, parse_inbox
from chat2order.rules import approve_order, evaluate_orders, reconstruct_orders, validate_business
from chat2order.workflow import process_orders


def main():
    business = default_business()
    validate_business(business)
    now = now_local(business)
    messages = parse_inbox(demo_inbox(business), business)
    budget = CallBudget()
    snapshot = process_orders(messages=messages, business=business, reservations={}, now=now, budget=budget)
    by_name = {o.customer: o for o in snapshot.orders}
    assert len(snapshot.orders) == 5
    assert by_name["Ayesha"].quantity == 6
    assert by_name["Ayesha"].status == "capacity_conflict"
    assert by_name["Bilal"].status == "capacity_conflict"
    assert by_name["Hina"].status == "needs_clarification"
    assert by_name["Dani"].status == "ready"
    assert by_name["Zain"].status == "cancelled"
    assert budget.calls == 0

    # Approval rechecks available capacity and blocks a competing overbooking.
    reservation = approve_order(by_name["Ayesha"], business, {}, now)
    ledger = {reservation.order_id: reservation}
    try:
        approve_order(by_name["Bilal"], business, ledger, now)
    except ValueError:
        pass
    else:
        raise AssertionError("Overbooking should be blocked.")
    rebuilt = reconstruct_orders(messages, snapshot.events, business)
    assert next(o for o in rebuilt if o.customer == "Ayesha").quantity == 6
    evaluated = evaluate_orders(rebuilt, business, ledger, now)
    assert next(o for o in evaluated if o.customer == "Ayesha").status == "confirmed"

    # Customer agreement resolves the conflict; no reservation is made by AI.
    resolved = process_orders(messages=parse_inbox(demo_inbox(business, resolved=True), business), business=business, reservations={}, now=now, budget=budget)
    assert all(o.status in {"ready", "cancelled"} for o in resolved.orders)
    assert budget.calls == 0
    print("PASS: offline workflow, order changes, capacity guard, and customer clarification. Groq calls: 0.")


if __name__ == "__main__":
    main()
