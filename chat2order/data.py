"""Sample data, text/CSV import, and safe JSON exports. No external services."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .models import Business, Message, Workspace

ROOT = Path(__file__).resolve().parents[1]
MAX_MESSAGES = 25
MAX_INPUT_CHARS = 18000


def default_business() -> Business:
    return Business.model_validate_json((ROOT / "demo_data" / "business.json").read_text())


def now_local(business: Business) -> datetime:
    return datetime.now(ZoneInfo(business.timezone))


def demo_inbox(business: Business, resolved: bool = False) -> str:
    today = now_local(business).date()
    target = today + timedelta(days=3)
    day, pickup = today.isoformat(), target.isoformat()
    answer = f"\n{day} 11:45 | customer | Yes, change my order to 4 vanilla cupcakes for {pickup} at 16:00, pickup please." if resolved else ""
    hina_answer = f"\n{day} 11:47 | customer | My pickup time is 15:00." if resolved else ""
    return f"""[Ayesha]
{day} 10:00 | customer | I'd like 12 chocolate cupcakes for {pickup} at 16:00, pickup please.
{day} 10:03 | customer | Actually, make that 6.

[Bilal]
{day} 10:12 | customer | Can I order 4 chocolate cupcakes for {pickup} at 16:00, pickup please?{answer}

[Hina]
{day} 10:25 | customer | Please book 2 vanilla cupcakes for {pickup}, pickup please.{hina_answer}

[Dani]
{day} 10:40 | customer | I need 4 brownies for {pickup} at 14:00, pickup please.

[Zain]
{day} 10:50 | customer | I want 3 cookies for {pickup} at 12:00, pickup please.
{day} 10:55 | customer | Please cancel my order.
"""


def _message(customer: str, timestamp: str, sender: str, text: str, sequence: int, business: Business) -> Message:
    customer, sender, text = customer.strip(), sender.strip().lower(), text.strip()
    if not customer or len(customer) > 80:
        raise ValueError("Each conversation needs a customer name of 1–80 characters.")
    if sender not in {"customer", "seller"}:
        raise ValueError("Sender must be 'customer' or 'seller'.")
    try:
        stamp = datetime.fromisoformat(timestamp.strip())
    except ValueError as exc:
        raise ValueError("Use timestamps like 2026-09-30 10:00.") from exc
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=ZoneInfo(business.timezone))
    stamp = stamp.astimezone(ZoneInfo(business.timezone)).isoformat()
    cid = "C-" + hashlib.sha256(customer.casefold().encode()).hexdigest()[:8]
    digest = hashlib.sha256(f"{cid}|{stamp}|{sender}|{text}".encode()).hexdigest()[:12]
    return Message(id=f"M-{digest}", conversation_id=cid, customer=customer, timestamp=stamp, sender=sender, text=text)


def parse_inbox(text: str, business: Business) -> list[Message]:
    if len(text) > MAX_INPUT_CHARS:
        raise ValueError(f"Please keep a batch below {MAX_INPUT_CHARS:,} characters.")
    messages: list[Message] = []
    customer = ""
    for number, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        heading = re.fullmatch(r"\[([^\]]+)\]", line)
        if heading:
            customer = heading.group(1)
            continue
        if not customer:
            raise ValueError(f"Line {number}: add a [Customer name] heading first.")
        parts = line.split("|", 2)
        if len(parts) != 3:
            raise ValueError(f"Line {number}: use timestamp | customer or seller | message.")
        messages.append(_message(customer, parts[0], parts[1], parts[2], number, business))
    if not messages:
        raise ValueError("Paste at least one customer conversation.")
    unique = {m.id: m for m in messages}
    if len(unique) > MAX_MESSAGES:
        raise ValueError(f"A batch can contain up to {MAX_MESSAGES} unique messages.")
    return sorted(unique.values(), key=lambda m: m.timestamp)


def csv_to_inbox(content: str, business: Business) -> str:
    reader = csv.DictReader(io.StringIO(content.lstrip("\ufeff")))
    expected = {"customer", "timestamp", "sender", "text"}
    if not expected.issubset(set(reader.fieldnames or [])):
        raise ValueError("CSV columns must include customer, timestamp, sender, text.")
    groups: dict[str, list[Message]] = {}
    for index, row in enumerate(reader):
        if index >= MAX_MESSAGES:
            raise ValueError(f"CSV import is limited to {MAX_MESSAGES} messages.")
        message = _message(row["customer"], row["timestamp"], row["sender"], row["text"], index, business)
        groups.setdefault(message.customer, []).append(message)
    blocks = []
    for customer, messages in groups.items():
        lines = [f"[{customer}]"]
        for message in sorted(messages, key=lambda m: m.timestamp):
            safe_text = message.text.replace("\n", " ").replace("\r", " ")
            lines.append(f"{message.timestamp} | {message.sender} | {safe_text}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def workspace_json(workspace: Workspace) -> str:
    return workspace.model_dump_json(indent=2)


def safe_csv(rows: list[dict]) -> str:
    if not rows:
        return ""
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(rows[0]))
    writer.writeheader()
    for row in rows:
        cleaned = {}
        for key, value in row.items():
            text = str(value if value is not None else "")
            cleaned[key] = "'" + text if text.lstrip().startswith(("=", "+", "-", "@")) else text
        writer.writerow(cleaned)
    return buffer.getvalue()


def stable_hash(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str, ensure_ascii=False).encode()).hexdigest()
