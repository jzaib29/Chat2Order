"""Deterministic demo adapters; intentionally not a general language parser."""
from __future__ import annotations

import re

from .models import Business, CustomerReply, EventBatch, Message, Order, OrderEvent, ReplyBatch, Resolution, ResolutionBatch


def demo_events(messages: list[Message], business: Business) -> EventBatch:
    events = []
    started: set[str] = set()
    for message in messages:
        text = message.text.lower()
        fields = dict(conversation_id=message.conversation_id, source_message_id=message.id, product_id=None, quantity=None, pickup_date=None, pickup_time=None, question=None, explanation="")
        if message.sender == "seller":
            events.append(OrderEvent(action="ignore", **fields))
            continue
        if re.search(r"\b(cancel|cancelled|canceled)\b", text):
            events.append(OrderEvent(action="cancel", **fields,))
            continue
        if re.search(r"\b(delivery|deliver|shipping|ship)\b", text):
            fields["question"] = "This MVP supports pickup orders. Would pickup work for you?"
            events.append(OrderEvent(action="clarify", **fields))
            continue
        # The offline demo refuses features it cannot safely interpret.
        if any(word in text for word in ["ignore previous", "system prompt", "instead of", "another order", "second order"]):
            fields.update(question="Please review this request manually; the demo parser cannot interpret it safely.")
            events.append(OrderEvent(action="clarify", **fields))
            continue
        matched = [p for p in business.products if any(re.search(r"\b" + re.escape(alias.lower()) + r"\b", text) for alias in [p.name, *p.aliases])]
        if len(matched) > 1:
            fields.update(question="Please separate the products into individual orders in this MVP.")
            events.append(OrderEvent(action="clarify", **fields))
            continue
        number = None
        if matched:
            fields["product_id"] = matched[0].id
            aliases = sorted([matched[0].name, *matched[0].aliases], key=len, reverse=True)
            for alias in aliases:
                match = re.search(r"\b(\d+)\s+" + re.escape(alias.lower()) + r"\b", text)
                if match:
                    number = int(match.group(1))
                    break
        change = re.search(r"(?:make (?:that|it)|change (?:that|it) to|quantity (?:is|to))\s+(\d+)\b", text)
        if change:
            number = int(change.group(1))
        fields["quantity"] = number
        date_match = re.search(r"\b(\d{4}-\d{2}-\d{2})\b", text)
        time_match = re.search(r"\b([01]?\d|2[0-3]):([0-5]\d)\b", text)
        fields["pickup_date"] = date_match.group(1) if date_match else None
        fields["pickup_time"] = f"{int(time_match.group(1)):02}:{time_match.group(2)}" if time_match else None
        if matched or change or date_match or time_match:
            action = "update" if message.conversation_id in started else "create"
            started.add(message.conversation_id)
            fields["explanation"] = "Updated only the details explicitly present in this message." if action == "update" else "Extracted a pickup request from the customer message."
        elif re.fullmatch(r"(?:thanks|thank you|okay|ok|great)[.! ]*", text.strip()):
            action = "ignore"
        else:
            action = "clarify"
            fields["question"] = "Please provide the product, quantity, pickup date (YYYY-MM-DD), and time (HH:MM), or switch to Live AI for natural language interpretation."
        events.append(OrderEvent(action=action, **fields))
    return EventBatch(events=events)


def demo_resolutions(orders: list[Order], alternatives: dict, policy_ids: list[str]) -> ResolutionBatch:
    cases = []
    for order in orders:
        if not order.issues:
            continue
        options = alternatives.get(order.id, [])
        questions = list(dict.fromkeys(order.questions))
        if order.product_id is None:
            questions.append("Which catalog product would you like?")
        if order.quantity is None:
            questions.append("How many would you like?")
        if order.pickup_date is None:
            questions.append("What is your pickup date?")
        if order.pickup_time is None:
            questions.append("What time will you pick up your order?")
        if any(i.code in {"pickup_window", "lead_time", "invalid_datetime"} for i in order.issues):
            questions.append("Could you choose a pickup date and time within our lead time and pickup hours?")
        explanation = " ".join(issue.text for issue in order.issues)
        cases.append(Resolution(order_id=order.id, explanation=explanation, alternative_ids=[o.id for o in options[:1]], questions=list(dict.fromkeys(questions)), policy_ids=policy_ids[:3]))
    return ResolutionBatch(cases=cases)


def demo_replies(orders: list[Order], resolutions: list[Resolution], alternatives: dict, business: Business) -> ReplyBatch:
    lookup = {o.id: o for o in orders}
    replies = []
    for case in resolutions:
        order = lookup[case.order_id]
        text = f"Hi {order.customer}! "
        chosen = [o for o in alternatives.get(order.id, []) if o.id in case.alternative_ids]
        if chosen:
            option = chosen[0]
            text += f"We need to check availability for your requested item. Would {option.quantity} {option.product_name.lower()} for pickup on {option.pickup_date} at {option.pickup_time} work instead? The total would be {business.currency} {option.quantity * option.unit_price:,}. We will keep the request pending until you agree and the owner approves."
        elif case.questions:
            text += " ".join(case.questions) + " Your request is still pending."
        else:
            text += "We are reviewing availability for your request. Could you confirm whether a different product or pickup date would work? Your request is still pending."
        replies.append(CustomerReply(order_id=order.id, text=text))
    return ReplyBatch(messages=replies)
