"""Deterministic order reconstruction, capacity checks, and approval ledger."""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from .data import stable_hash
from .models import Alternative, Business, Issue, Message, Order, OrderEvent, Reservation


def product_map(business: Business):
    return {p.id: p for p in business.products}


def validate_business(business: Business, reservations: dict[str, Reservation] | None = None) -> None:
    if not business.products:
        raise ValueError("Add at least one product.")
    if len(business.products) > 12:
        raise ValueError("The MVP supports up to 12 catalog products.")
    ids = [p.id for p in business.products]
    if len(ids) != len(set(ids)) or any(not i.strip() for i in ids):
        raise ValueError("Product IDs must be unique and non-empty.")
    ZoneInfo(business.timezone)
    if business.pickup_start >= business.pickup_end:
        raise ValueError("Pickup start must be earlier than pickup end.")
    if len(business.policy_text) > 8000:
        raise ValueError("Keep the policy below 8,000 characters.")
    totals: dict[tuple[str, str], int] = defaultdict(int)
    products = product_map(business)
    for key, reservation in (reservations or {}).items():
        if reservation.order_id != key or reservation.product_id not in products:
            raise ValueError("A reservation references a missing product or invalid order ID.")
        date.fromisoformat(reservation.pickup_date)
        time.fromisoformat(reservation.pickup_time)
        totals[(reservation.product_id, reservation.pickup_date)] += reservation.quantity
    for (pid, _), quantity in totals.items():
        if quantity > products[pid].daily_capacity:
            raise ValueError(f"Capacity for {products[pid].name} cannot be below its existing reservations.")


def reconstruct_orders(messages: list[Message], events: list[OrderEvent], business: Business) -> list[Order]:
    """Rebuild from evidence every time; repeated processing cannot add quantities."""
    lookup = {m.id: m for m in messages}
    catalog = product_map(business)
    by_message: dict[str, OrderEvent] = {}
    for event in events:
        source = lookup.get(event.source_message_id)
        if not source or source.conversation_id != event.conversation_id:
            raise ValueError("The Interpreter returned an invalid source message reference.")
        if event.source_message_id in by_message:
            raise ValueError("The Interpreter returned multiple events for one message.")
        if source.sender != "customer" and event.action != "ignore":
            raise ValueError("A seller message cannot authorize a customer order change.")
        if event.product_id is not None and event.product_id not in catalog:
            raise ValueError("The Interpreter returned a product outside the catalog.")
        if event.quantity is not None and not 1 <= event.quantity <= 10000:
            raise ValueError("Order quantities must be whole numbers between 1 and 10,000.")
        for value, parser in [(event.pickup_date, date.fromisoformat), (event.pickup_time, time.fromisoformat)]:
            if value is not None:
                try:
                    parser(value)
                except ValueError as exc:
                    raise ValueError("The Interpreter returned an invalid pickup date or time.") from exc
        by_message[event.source_message_id] = event
    orders: dict[str, Order] = {}
    for message in messages:
        if message.sender != "customer":
            continue
        event = by_message.get(message.id)
        if event is not None and event.action == "ignore":
            continue
        order = orders.setdefault(message.conversation_id, Order(id="O-" + message.conversation_id[2:], conversation_id=message.conversation_id, customer=message.customer))
        if event is None:
            order.questions.append(f"Please review the unprocessed message {message.id}.")
            order.source_ids.append(message.id)
            continue
        order.source_ids.append(message.id)
        if event.action == "cancel":
            order.cancelled = True
            order.questions = []
            continue
        if event.action == "create" and order.product_id is not None and not order.cancelled:
            order.questions.append("This conversation may contain a second order. Separate the orders before approval.")
            continue
        if order.cancelled and event.action != "create":
            order.questions.append("This order was cancelled. Please confirm whether a new order is intended.")
            continue
        if event.action == "create":
            if order.cancelled:
                # A new request after cancellation must not inherit the old
                # quantity, date, or product as if the customer supplied them.
                order.product_id = order.quantity = order.pickup_date = order.pickup_time = None
            order.cancelled = False
        for field in ["product_id", "quantity", "pickup_date", "pickup_time"]:
            value = getattr(event, field)
            if value is not None:
                setattr(order, field, value)
        if event.action == "clarify" and event.question:
            order.questions.append(event.question)
        elif event.action in {"create", "update"}:
            # A later substantive update resolves the earlier semantic question.
            # Missing-field checks below still enforce completeness.
            order.questions = [q for q in order.questions if q.startswith("This conversation may contain a second order.")]
        if event.explanation:
            order.notes.append(event.explanation)
    return list(orders.values())


def matches_reservation(order: Order, reservation: Reservation) -> bool:
    return not order.cancelled and all(getattr(order, field) == getattr(reservation, field) for field in ["product_id", "quantity", "pickup_date", "pickup_time"])


def order_datetime(order: Order, business: Business) -> datetime | None:
    if not order.pickup_date or not order.pickup_time:
        return None
    try:
        return datetime.combine(date.fromisoformat(order.pickup_date), time.fromisoformat(order.pickup_time), tzinfo=ZoneInfo(business.timezone))
    except ValueError:
        return None


def available_capacity(pid: str, day: str, business: Business, reservations: dict[str, Reservation], exclude_id: str | None = None) -> int:
    used = sum(r.quantity for oid, r in reservations.items() if oid != exclude_id and r.product_id == pid and r.pickup_date == day)
    return max(0, product_map(business)[pid].daily_capacity - used)


def evaluate_orders(orders: list[Order], business: Business, reservations: dict[str, Reservation], now: datetime) -> list[Order]:
    products = product_map(business)
    result = [o.model_copy(deep=True) for o in orders]
    included_ids = {o.id for o in result}
    for reservation in reservations.values():
        if reservation.order_id not in included_ids:
            # Replacing an inbox batch never hides or deletes existing approvals.
            result.append(Order(id=reservation.order_id, conversation_id=reservation.conversation_id, customer=reservation.customer, product_id=reservation.product_id, quantity=reservation.quantity, pickup_date=reservation.pickup_date, pickup_time=reservation.pickup_time, notes=["Existing approved order retained from the reservation ledger."]))
    pending: dict[tuple[str, str], list[Order]] = defaultdict(list)
    for order in result:
        order.issues = []
        existing = reservations.get(order.id)
        if order.cancelled:
            order.status = "cancel_requested" if existing else "cancelled"
            continue
        if existing and matches_reservation(order, existing):
            order.status = "confirmed"
            continue
        missing = [name.replace("_", " ") for name in ["product_id", "quantity", "pickup_date", "pickup_time"] if getattr(order, name) is None]
        if missing:
            order.issues.append(Issue(code="missing_details", text="Missing: " + ", ".join(missing).replace("product id", "product") + "."))
        for question in dict.fromkeys(order.questions):
            order.issues.append(Issue(code="ambiguous", text=question))
        if order.product_id is not None and order.product_id not in products:
            order.issues.append(Issue(code="unknown_product", text="Choose a catalog product."))
        if order.quantity is not None and not 1 <= order.quantity <= 10000:
            order.issues.append(Issue(code="quantity", text="Quantity must be between 1 and 10,000."))
        pickup = order_datetime(order, business)
        if order.pickup_date and order.pickup_time and pickup is None:
            order.issues.append(Issue(code="invalid_datetime", text="Please provide a valid pickup date and time."))
        if pickup:
            if not business.pickup_start <= pickup.strftime("%H:%M") <= business.pickup_end:
                order.issues.append(Issue(code="pickup_window", text=f"Pickup must be between {business.pickup_start} and {business.pickup_end}."))
            if pickup < now + timedelta(hours=business.lead_hours):
                order.issues.append(Issue(code="lead_time", text=f"Pickup needs at least {business.lead_hours} hours of lead time."))
        if order.product_id in products and order.quantity and order.pickup_date:
            available = available_capacity(order.product_id, order.pickup_date, business, reservations, order.id)
            if order.quantity > available:
                order.issues.append(Issue(code="capacity", text=f"Requests {order.quantity}; only {available} available after approved orders."))
            if not order.issues:
                pending[(order.product_id, order.pickup_date)].append(order)
        order.status = "needs_clarification" if order.issues else ("change_requested" if existing else "ready")
        if any(issue.code == "capacity" for issue in order.issues):
            order.status = "capacity_conflict"
    # Catch competing requests before the owner chooses what to approve.
    for (pid, day), group in pending.items():
        changing_ids = {o.id for o in group}
        used = sum(r.quantity for oid, r in reservations.items() if oid not in changing_ids and r.product_id == pid and r.pickup_date == day)
        total = sum(o.quantity or 0 for o in group)
        available = max(0, products[pid].daily_capacity - used)
        if total > available:
            for order in group:
                order.issues.append(Issue(code="competing_requests", text=f"Pending requests total {total}; {available} available. The owner must choose an allocation or offer an alternative."))
                order.status = "capacity_conflict"
    return result


def alternatives_for(order: Order, business: Business, reservations: dict[str, Reservation], now: datetime) -> list[Alternative]:
    if order.cancelled or not order.quantity or not order.pickup_date or not order.pickup_time:
        return []
    pickup = order_datetime(order, business)
    if pickup is None or pickup < now + timedelta(hours=business.lead_hours):
        return []
    if not business.pickup_start <= order.pickup_time <= business.pickup_end:
        return []
    alternatives = []
    for product in business.products:
        if product.id == order.product_id:
            continue
        available = available_capacity(product.id, order.pickup_date, business, reservations, order.id)
        if available >= order.quantity:
            aid = "ALT-" + stable_hash([order.id, product.id, order.quantity, order.pickup_date, order.pickup_time])[:10]
            alternatives.append(Alternative(id=aid, product_id=product.id, product_name=product.name, quantity=order.quantity, pickup_date=order.pickup_date, pickup_time=order.pickup_time, unit_price=product.price, available=available))
    return alternatives[:3]


def approve_order(order: Order, business: Business, reservations: dict[str, Reservation], now: datetime) -> Reservation:
    """A fresh individual capacity check is authoritative, never an agent's claim."""
    if order.cancelled:
        raise ValueError("Use the cancellation action for a cancelled request.")
    checked = evaluate_orders([order], business, reservations, now)[0]
    if checked.issues:
        raise ValueError("Approval blocked: " + " ".join(issue.text for issue in checked.issues))
    product = product_map(business)[order.product_id]
    previous = reservations.get(order.id)
    return Reservation(order_id=order.id, customer=order.customer, conversation_id=order.conversation_id, product_id=product.id, quantity=order.quantity, pickup_date=order.pickup_date, pickup_time=order.pickup_time, unit_price=product.price, approved_at=now.isoformat(), version=previous.version + 1 if previous else 1)


def order_total(order: Order, business: Business) -> int | None:
    product = product_map(business).get(order.product_id)
    return product.price * order.quantity if product and order.quantity is not None and not order.cancelled else None


def summary_rows(orders: list[Order], business: Business, reservations: dict[str, Reservation]) -> list[dict]:
    products = product_map(business)
    return [{"order_id": o.id, "customer": o.customer, "product": products[o.product_id].name if o.product_id in products else "", "quantity": o.quantity, "pickup_date": o.pickup_date, "pickup_time": o.pickup_time, "status": o.status, "requested_total": order_total(o, business), "approved_quantity": reservations[o.id].quantity if o.id in reservations else "", "issues": " | ".join(i.text for i in o.issues)} for o in orders]
