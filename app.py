"""Chat2Order Streamlit entry point. Launch: streamlit run app.py."""
from __future__ import annotations

import html
import os
from datetime import date, datetime, time
from pathlib import Path

os.environ.setdefault("CREWAI_TELEMETRY_DISABLED", "true")
os.environ.setdefault("OTEL_SDK_DISABLED", "true")

import streamlit as st
from pydantic import ValidationError

from chat2order.agents import BudgetExceeded, CallBudget, ProviderFailure
from chat2order.data import csv_to_inbox, default_business, demo_inbox, now_local, parse_inbox, safe_csv, stable_hash, workspace_json
from chat2order.models import Business, Product, Snapshot, Workspace
from chat2order.retrieval import policy_sections
from chat2order.rules import alternatives_for, approve_order, available_capacity, evaluate_orders, order_total, product_map, summary_rows, validate_business
from chat2order.workflow import process_orders, routine_reply

ROOT = Path(__file__).resolve().parent
st.set_page_config(page_title="Chat2Order · Your order desk", page_icon="💬", layout="wide", initial_sidebar_state="expanded")
st.markdown("<style>" + (ROOT / "assets/styles.css").read_text() + "</style>", unsafe_allow_html=True)


def esc(value) -> str:
    return html.escape(str(value if value is not None else ""))


def secret(name: str, default=""):
    value = os.getenv(name)
    if value is not None:
        return value
    try:
        return st.secrets.get(name, default)
    except (FileNotFoundError, KeyError):
        return default


def navigate(page: str):
    st.session_state.nav = page


def add_audit(action: str, detail: str):
    workspace = st.session_state.workspace
    workspace.audit.append({"time": now_local(workspace.business).isoformat(), "action": action, "detail": detail})


def load_demo(resolved: bool = False, reset: bool = False):
    business = default_business() if reset else st.session_state.workspace.business
    text = demo_inbox(business, resolved)
    reservations = {} if reset else st.session_state.workspace.reservations
    cache = st.session_state.get("agent_cache", {})
    snapshot = process_orders(messages=parse_inbox(text, business), business=business, reservations=reservations, now=now_local(business), mode="demo", cache=cache)
    if reset or "workspace" not in st.session_state:
        st.session_state.workspace = Workspace(business=business, inbox_text=text, snapshot=snapshot, reservations=reservations)
    else:
        st.session_state.workspace.inbox_text = text
        st.session_state.workspace.snapshot = snapshot
    st.session_state.inbox_editor = text
    st.session_state.flash = "Demo loaded. All data is synthetic; no LLM requests were made."
    st.session_state.selected_order = None


def init():
    if "workspace" not in st.session_state:
        business = default_business()
        st.session_state.workspace = Workspace(business=business, inbox_text="", snapshot=Snapshot())
        st.session_state.agent_cache = {}
        st.session_state.session_calls = 0
        st.session_state.session_tokens = 0
        st.session_state.last_attempt_calls = 0
        st.session_state.nav = "Overview"
        st.session_state.selected_order = None
        load_demo(reset=True)


init()
if pending_text := st.session_state.pop("pending_inbox", None):
    st.session_state.inbox_editor = pending_text
ws: Workspace = st.session_state.workspace
business = ws.business
now = now_local(business)
orders = evaluate_orders(ws.snapshot.orders, business, ws.reservations, now)
products = product_map(business)

STATUS = {"ready": ("Ready for review", "green"), "change_requested": ("Change requested", "orange"), "capacity_conflict": ("Capacity conflict", "orange"), "needs_clarification": ("Needs details", "red"), "confirmed": ("Approved", "green"), "cancel_requested": ("Cancel requested", "orange"), "cancelled": ("Cancelled", "grey")}


def badge(status: str) -> str:
    label, color = STATUS.get(status, (status.replace("_", " ").title(), "grey"))
    return f'<span class="badge {color}">{esc(label)}</span>'


def section(title: str, subtitle: str = ""):
    st.markdown(f'<div class="section-top"><h3>{esc(title)}</h3><small>{esc(subtitle)}</small></div>', unsafe_allow_html=True)


def metrics():
    active = [o for o in orders if o.status not in {"cancelled", "cancel_requested"}]
    attention = [o for o in orders if o.issues or o.status == "cancel_requested"]
    approved_value = sum(r.quantity * r.unit_price for r in ws.reservations.values())
    cards = [("Active requests", len(active), "Current requests, including changes"), ("Need your attention", len(attention), "Details or capacity to resolve"), ("Approved order value", f"{business.currency} {approved_value:,}", "Recorded approvals only"), ("Live LLM requests", st.session_state.session_calls, "This browser session · demo uses 0")]
    for col, (label, value, foot) in zip(st.columns(4), cards):
        col.markdown(f'<div class="metric-card"><div class="metric-label">{esc(label)}</div><div class="metric-value">{esc(value)}</div><div class="metric-foot">{esc(foot)}</div></div>', unsafe_allow_html=True)


with st.sidebar:
    st.markdown('<div class="brand"><div class="brand-icon">✓</div><div><div class="brand-name">Chat2Order</div><div class="brand-sub">From chat to clarity</div></div></div>', unsafe_allow_html=True)
    st.caption("YOUR WORKSPACE")
    st.markdown(f"**{esc(business.name)}**")
    page = st.radio("Navigation", ["Overview", "Inbox", "Order desk", "Agent studio", "Business settings"], key="nav", label_visibility="collapsed")
    st.divider()
    mode_label = st.radio("Processing mode", ["Offline demo", "Live AI"], key="processing_mode")
    mode = "live" if mode_label == "Live AI" else "demo"
    api_key = secret("GROQ_API_KEY")
    model_id = secret("GROQ_MODEL", "openai/gpt-oss-20b")
    if mode == "demo":
        st.caption("Deterministic sample parser. No API key or LLM calls. Use ISO dates and 24-hour times.")
    else:
        st.caption("Groq runs only when you click Process messages. Up to 3 requests per run; no automatic retries.")
        st.caption("API key configured" if api_key else "Add GROQ_API_KEY in deployment secrets.")
    access_code = secret("LIVE_AI_ACCESS_CODE")
    entered_code = st.text_input("Live AI access code", type="password") if mode == "live" and access_code else ""
    st.divider()
    st.button("Reset sample workspace", on_click=load_demo, kwargs={"reset": True}, use_container_width=True)
    st.caption("Single-owner hackathon workspace. Export a backup before refreshing the browser.")

if message := st.session_state.pop("flash", None):
    st.success(message)

last_mode = "Offline demo" if ws.snapshot.mode == "demo" else "Live AI"
st.markdown(f'<div class="pill-line"><span class="badge">{esc(business.name)}</span><span class="badge green">Last analysis: {last_mode}</span><span class="badge grey">Pickup orders</span></div>', unsafe_allow_html=True)


def order_card(order, compact=False):
    product = products.get(order.product_id)
    title = f"{order.quantity or '?'} × {product.name if product else 'Product to clarify'}"
    total = order_total(order, business)
    with st.container(border=True):
        st.markdown(f'<div class="order-head"><div class="customer"><div class="avatar">{esc(order.customer[:1].upper())}</div><div><b>{esc(order.customer)}</b><small>{esc(order.id)}</small></div></div>{badge(order.status)}</div><div class="order-product">{esc(title)}</div><div class="order-meta">Pickup: {esc(order.pickup_date or "Date needed")} · {esc(order.pickup_time or "Time needed")}<br>{esc(business.currency)} {f"{total:,}" if total is not None else "—"}</div>', unsafe_allow_html=True)
        if order.issues:
            st.markdown(f'<div class="issue-note">{esc(order.issues[0].text)}</div>', unsafe_allow_html=True)
        if order.id in ws.reservations and order.status != "confirmed":
            st.caption("The existing approved reservation remains active until you record a change.")
        if not compact:
            if st.button("Review order →", key=f"review_{order.id}", use_container_width=True):
                st.session_state.selected_order = order.id
                st.rerun()


def overview():
    st.markdown('<div class="hero"><div><div class="eyebrow">A little less chaos. A lot more clarity.</div><h1>Great orders start<br>with <span>clear conversations.</span></h1><p>Bring your customer messages together. Catch changes, resolve missing details, and review what you can promise—all from one calm order desk.</p></div><div class="hero-art"><div class="mini-order"><div class="mini-avatar">A</div><div><b>12 cupcakes → 6 cupcakes</b><small>Latest customer change understood</small></div></div><div class="mini-order"><div class="mini-avatar">✓</div><div><b>Capacity checked</b><small>A promise worth reviewing</small></div></div><div class="mini-order"><div class="mini-avatar">↗</div><div><b>A thoughtful reply, ready</b><small>You make the final decision</small></div></div></div></div>', unsafe_allow_html=True)
    metrics()
    left, right = st.columns([1.65, 1], gap="large")
    with left:
        section("Your next decisions", "Start with what needs attention")
        highlighted = sorted(orders, key=lambda o: (not bool(o.issues), o.status == "cancelled"))[:3]
        for order in highlighted:
            order_card(order, compact=True)
        st.button("Open order desk →", type="primary", on_click=navigate, args=("Order desk",))
    with right:
        section("Demand at a glance", "Requests are not reservations")
        with st.container(border=True):
            requested_days = sorted({o.pickup_date for o in orders if o.pickup_date and not o.cancelled})
            selected_day = st.selectbox("Pickup date", requested_days) if requested_days else None
            if selected_day:
                for product in business.products:
                    demand = sum(o.quantity or 0 for o in orders if not o.cancelled and o.product_id == product.id and o.pickup_date == selected_day)
                    reserved = sum(r.quantity for r in ws.reservations.values() if r.product_id == product.id and r.pickup_date == selected_day)
                    percent = min(100, round(demand / max(1, product.daily_capacity) * 100))
                    over = "over" if demand > product.daily_capacity else ""
                    st.markdown(f'<div class="capacity-row"><div class="capacity-label"><span>{esc(product.name)}</span><b>{demand} / {product.daily_capacity}</b></div><div class="track"><div class="fill {over}" style="width:{percent}%"></div></div><div class="order-meta">{reserved} approved · {max(0, product.daily_capacity-reserved)} unreserved</div></div>', unsafe_allow_html=True)
            st.caption("Daily capacity per product. Pending requests may compete for the same availability.")
        section("Try the story", "Zero LLM requests")
        with st.container(border=True):
            st.write("**1.** Ayesha changes 12 cupcakes to 6.")
            st.write("**2.** Bilal's request creates a capacity conflict.")
            st.write("**3.** Hina hasn't supplied a pickup time.")
            st.button("Load customer answers", on_click=load_demo, kwargs={"resolved": True}, use_container_width=True)
            st.caption("Adds Bilal's consent to vanilla and Hina's pickup time, then reruns the offline workflow.")


def inbox():
    section("Turn conversations into next steps", "One active, single-product order per customer")
    st.caption("Customer names identify conversations. Use timestamp | customer or seller | message. IDs are assigned automatically; repeated identical messages are deduplicated.")
    editor_tab, import_tab = st.tabs(["Conversation editor", "Import CSV / restore workspace"])
    with editor_tab:
        text = st.text_area("Customer conversations", key="inbox_editor", height=380, label_visibility="collapsed")
        try:
            preview = parse_inbox(text, business)
            st.caption(f"{len(preview)} messages · {len({m.conversation_id for m in preview})} conversations · {business.timezone}")
        except ValueError as exc:
            preview = None
            st.caption(str(exc))
        remaining = max(0, int(secret("MAX_SESSION_CALLS", "12")) - st.session_state.session_calls)
        blocked_live = mode == "live" and (not api_key or remaining < 3 or (access_code and entered_code != access_code))
        if mode == "live":
            st.info(f"Live run: at most 3 requests. {remaining} requests remain in this browser session. A smaller batch is more economical.")
        if st.button("Process messages →", type="primary", disabled=preview is None or bool(blocked_live), use_container_width=True):
            budget = CallBudget(limit=min(3, remaining) if mode == "live" else 3)
            try:
                with st.spinner("Reading changes, checking capacity, and preparing next steps…"):
                    snapshot = process_orders(messages=preview, business=business, reservations=ws.reservations, now=now_local(business), mode=mode, api_key=api_key, model_id=model_id, budget=budget, cache=st.session_state.agent_cache)
                ws.inbox_text = text
                ws.snapshot = snapshot
                st.session_state.flash = f"Processed {len(snapshot.orders)} requests. {budget.calls} Groq requests made. Open Order desk to review."
            except (ProviderFailure, BudgetExceeded, ValueError, ValidationError) as exc:
                st.error(str(exc))
            except Exception as exc:
                st.error(f"The workflow stopped safely ({type(exc).__name__}). The previous board is preserved. Check the app logs for configuration details.")
            finally:
                st.session_state.session_calls += budget.calls
                st.session_state.session_tokens += budget.input_tokens + budget.output_tokens
                st.session_state.last_attempt_calls = budget.calls
            if st.session_state.get("flash"):
                st.rerun()
    with import_tab:
        uploaded = st.file_uploader("Upload a conversation CSV", type=["csv"])
        if uploaded and st.button("Import CSV into editor"):
            try:
                if uploaded.size > 100_000:
                    raise ValueError("CSV files must be below 100 KB.")
                text = csv_to_inbox(uploaded.getvalue().decode("utf-8-sig"), business)
                # The editor widget was already rendered: defer assigning it to the next run.
                st.session_state.pending_inbox = text
                st.rerun()
            except (ValueError, UnicodeDecodeError) as exc:
                st.error(str(exc))
        st.download_button("Download CSV template", (ROOT / "demo_data/messages_template.csv").read_text(), "messages_template.csv", "text/csv")
        st.divider()
        backup = st.file_uploader("Restore an exported workspace", type=["json"], key="backup_upload")
        if backup and st.button("Restore workspace"):
            try:
                if backup.size > 2_000_000:
                    raise ValueError("Workspace backups must be below 2 MB.")
                restored = Workspace.model_validate_json(backup.getvalue())
                validate_business(restored.business, restored.reservations)
                if len(restored.snapshot.messages) > 25 or len(restored.snapshot.orders) > 25:
                    raise ValueError("Backup exceeds the MVP batch limit.")
                st.session_state.workspace = restored
                st.session_state.agent_cache = {}
                st.session_state.pending_inbox = restored.inbox_text
                st.session_state.flash = "Workspace restored. Live credentials and call counters are not imported."
                st.rerun()
            except (ValueError, ValidationError) as exc:
                st.error(str(exc))
    section("Latest message evidence", "Original messages remain available")
    for message in ws.snapshot.messages[-6:]:
        with st.expander(f"{message.customer} · {message.sender} · {message.timestamp[:16]}"):
            st.write(message.text)
            st.caption(message.id)


def inspect_order(order):
    st.divider()
    section(f"Review {order.customer}'s order", order.id)
    left, right = st.columns([1.2, 1], gap="large")
    with left:
        with st.container(border=True):
            st.markdown(badge(order.status), unsafe_allow_html=True)
            for issue in order.issues:
                st.warning(issue.text)
            existing = ws.reservations.get(order.id)
            if existing:
                st.info(f"Approved reservation: {existing.quantity} {products[existing.product_id].name.lower()}, {existing.pickup_date} at {existing.pickup_time} (version {existing.version}).")
            if order.status == "cancel_requested":
                if st.button("Record cancellation and release capacity", type="primary"):
                    ws.reservations.pop(order.id, None)
                    add_audit("cancel", order.id)
                    ws.snapshot.replies[order.id] = f"Hi {order.customer}, your order cancellation has been recorded."
                    st.session_state.flash = "Cancellation recorded. Capacity released."
                    st.rerun()
            elif not order.cancelled and order.product_id and order.quantity and order.pickup_date and order.pickup_time:
                # An owner may select one of competing requests; individual checks
                # still prevent overbooking. The other requests then re-evaluate.
                individual = evaluate_orders([order], business, ws.reservations, now_local(business))[0]
                label = "Approve replacement" if existing and order.status != "confirmed" else "Approve order"
                if st.button(label, type="primary", disabled=bool(individual.issues) or order.status == "confirmed"):
                    try:
                        reservation = approve_order(order, business, ws.reservations, now_local(business))
                        ws.reservations[order.id] = reservation
                        add_audit("approve", f"{order.id} · version {reservation.version}")
                        product = products[reservation.product_id]
                        ws.snapshot.replies[order.id] = f"Hi {order.customer}! Your order is approved: {reservation.quantity} {product.name.lower()}, pickup {reservation.pickup_date} at {reservation.pickup_time}. Total: {business.currency} {reservation.quantity * reservation.unit_price:,}."
                        st.session_state.flash = "Order approved after a fresh capacity check."
                        st.rerun()
                    except ValueError as exc:
                        st.error(str(exc))
                if any(i.code == "competing_requests" for i in order.issues) and not individual.issues:
                    st.caption("Approving this request uses capacity that other pending requests may need. They will be checked again.")
            options = alternatives_for(order, business, ws.reservations, now_local(business)) if order.issues else []
            if options:
                st.write("**Available alternatives to discuss**")
                for option in options:
                    st.caption(f"{option.quantity} {option.product_name} · {business.currency} {option.quantity * option.unit_price:,}")
                st.caption("These are offers, not reservations. Add the customer's agreement to the conversation before changing a product.")
        with st.expander("Edit proposed details after owner review"):
            st.caption("This changes the proposed order. Existing approved reservations are preserved until replacement approval. A new analysis rebuilds proposals from messages.")
            with st.form(f"edit_{order.id}"):
                pids = list(products)
                choice = st.selectbox("Product", ["", *pids], index=pids.index(order.product_id) + 1 if order.product_id in pids else 0, format_func=lambda pid: products[pid].name if pid else "Choose product")
                quantity = st.number_input("Quantity", min_value=1, max_value=10000, value=order.quantity if order.quantity and 1 <= order.quantity <= 10000 else 1)
                pickup_date = st.date_input("Pickup date", value=date.fromisoformat(order.pickup_date) if order.pickup_date else now.date())
                pickup_time = st.time_input("Pickup time", value=time.fromisoformat(order.pickup_time) if order.pickup_time else time(12, 0))
                reviewed = st.checkbox("I reviewed the source messages and any required customer agreement.")
                saved = st.form_submit_button("Save proposed details")
            if saved:
                if not reviewed or not choice:
                    st.error("Select a product and confirm your review first.")
                else:
                    for current in ws.snapshot.orders:
                        if current.id == order.id:
                            current.product_id, current.quantity = choice, quantity
                            current.pickup_date, current.pickup_time = pickup_date.isoformat(), pickup_time.strftime("%H:%M")
                            current.questions = []
                            current.notes.append("Proposed details explicitly edited by the owner.")
                    ws.snapshot.replies.pop(order.id, None)
                    ws.snapshot.resolutions = [r for r in ws.snapshot.resolutions if r.order_id != order.id]
                    add_audit("edit_proposal", order.id)
                    st.session_state.flash = "Proposed details saved. Review the fresh checks before approval."
                    st.rerun()
    with right:
        with st.container(border=True):
            st.write("**Customer response draft**")
            reply = ws.snapshot.replies.get(order.id, routine_reply(order, business))
            if order.status == "confirmed":
                r = ws.reservations[order.id]
                reply = f"Hi {order.customer}! Your order is approved: {r.quantity} {products[r.product_id].name.lower()}, pickup {r.pickup_date} at {r.pickup_time}. Total: {business.currency} {r.quantity * r.unit_price:,}."
            st.text_area("Review and copy", value=reply, height=170, key="reply_" + stable_hash([order.id, reply])[:12])
            st.caption("Review before copying. Messages are never sent automatically.")
        with st.container(border=True):
            st.write("**Source messages**")
            for message in ws.snapshot.messages:
                if message.conversation_id == order.conversation_id:
                    st.caption(f"{message.timestamp[:16]} · {message.sender} · {message.id}")
                    st.write(message.text)
                    st.divider()
        case = next((r for r in ws.snapshot.resolutions if r.order_id == order.id), None)
        if case:
            with st.expander("Why this recommendation?"):
                st.write(case.explanation)
                for policy in policy_sections(business.policy_text):
                    if policy["id"] in case.policy_ids:
                        st.caption(policy["id"])
                        st.write(policy["text"])


def order_desk():
    section("Every request, a clear next step", "Approval always rechecks current capacity")
    metrics()
    ready, attention, closed = st.columns(3, gap="medium")
    groups = [(ready, "Ready for review", [o for o in orders if o.status in {"ready", "change_requested"}]), (attention, "Needs attention", [o for o in orders if o.issues or o.status == "cancel_requested"]), (closed, "Approved / closed", [o for o in orders if o.status in {"confirmed", "cancelled"}])]
    for col, title, group in groups:
        with col:
            section(title, str(len(group)))
            if not group:
                st.markdown('<div class="empty">Nothing here right now.</div>', unsafe_allow_html=True)
            for order in group:
                order_card(order)
    selected = next((o for o in orders if o.id == st.session_state.selected_order), None)
    if selected:
        inspect_order(selected)
    st.divider()
    export_left, export_right = st.columns(2)
    export_left.download_button("Download order sheet", safe_csv(summary_rows(orders, business, ws.reservations)), "chat2order_orders.csv", "text/csv", use_container_width=True)
    export_right.download_button("Save workspace backup", workspace_json(ws), "chat2order_workspace.json", "application/json", use_container_width=True)


def agent_studio():
    section("A coordinated workflow, with a visible budget", "CrewAI Flow + three specialized agents")
    st.caption("Offline demo uses deterministic adapters through the same CrewAI agents. It demonstrates orchestration; it is not live LLM reasoning.")
    specs = [("01 · INTERPRET", "Order Interpreter", "Reads chronological messages and proposes evidence-linked create, change, and cancellation events."), ("02 · RESOLVE", "Fulfillment Resolver", "Uses Python-checked capacity and retrieved policy sections to propose permitted alternatives and questions."), ("03 · COMMUNICATE", "Customer Coordinator", "Prepares short customer drafts from the selected options. The owner reviews every commitment.")]
    for col, (number, title, detail) in zip(st.columns(3), specs):
        col.markdown(f'<div class="step-card"><div class="step-number">{number}</div><h4>{title}</h4><p>{detail}</p></div>', unsafe_allow_html=True)
    st.divider()
    c1, c2, c3 = st.columns(3)
    c1.metric("Last analysis requests", sum(t.requests for t in ws.snapshot.traces))
    c2.metric("Last analysis tokens", sum(t.input_tokens + t.output_tokens for t in ws.snapshot.traces))
    c3.metric("Analysis time", f"{ws.snapshot.elapsed_ms / 1000:.2f} s")
    section("Execution trail", f"Last analysis: {ws.snapshot.processed_at[:19]}")
    for trace in ws.snapshot.traces:
        with st.container(border=True):
            st.write(f"**{trace.agent}**")
            st.caption(trace.detail)
            st.caption(f"{trace.requests} requests · {trace.input_tokens + trace.output_tokens} tokens · {trace.duration_ms} ms" + (" · cached" if trace.cached else ""))
    with st.expander("Inspect structured order events"):
        st.json([e.model_dump() for e in ws.snapshot.events])
    with st.expander("Owner decision history"):
        st.json(ws.audit)
    with st.expander("Resource controls"):
        st.write("Maximum 3 actual Groq requests per run. SDK retries are disabled. No manager agent, tool loops, embeddings, or LLM calls on widget reruns. Results are cached within this session. All capacity checks run again against current reservations.")
        st.caption(f"Session usage: {st.session_state.session_calls} requests, {st.session_state.session_tokens} reported tokens. Failed requests count toward the request limit; tokens are available only when Groq reports them. The session limit resets on browser reload and is not a production-wide quota.")


def settings():
    section("Make it your business", "Catalog values drive every calculation")
    with st.form("business_form"):
        name = st.text_input("Business name", value=business.name, max_chars=80)
        a, b = st.columns(2)
        currency = a.text_input("Currency label", value=business.currency, max_chars=12)
        timezone = b.selectbox("Business timezone", ["Asia/Karachi", "UTC", "Asia/Dubai", "Asia/Kolkata"], index=["Asia/Karachi", "UTC", "Asia/Dubai", "Asia/Kolkata"].index(business.timezone) if business.timezone in ["Asia/Karachi", "UTC", "Asia/Dubai", "Asia/Kolkata"] else 0)
        st.caption("Prices are whole currency units. Capacity is units per product per day. Keep IDs stable.")
        edited = st.data_editor([{"id": p.id, "name": p.name, "price": p.price, "daily_capacity": p.daily_capacity} for p in business.products], disabled=["id"], hide_index=True, use_container_width=True, key="catalog_editor")
        a, b, c = st.columns(3)
        lead = a.number_input("Minimum lead time (hours)", min_value=0, max_value=720, value=business.lead_hours)
        opening = b.time_input("Pickup opens", value=time.fromisoformat(business.pickup_start))
        closing = c.time_input("Pickup closes", value=time.fromisoformat(business.pickup_end))
        policy = st.text_area("Business policy", value=business.policy_text, height=230)
        save = st.form_submit_button("Save business settings", type="primary")
    if save:
        try:
            rows = edited.to_dict("records") if hasattr(edited, "to_dict") else edited
            aliases = {p.id: p.aliases for p in business.products}
            new = Business(name=name.strip() or "My bakery", currency=currency.strip() or "PKR", timezone=timezone, lead_hours=lead, pickup_start=opening.strftime("%H:%M"), pickup_end=closing.strftime("%H:%M"), products=[Product(**row, aliases=aliases.get(row["id"], [])) for row in rows], policy_text=policy)
            validate_business(new, ws.reservations)
            ws.business = new
            ws.snapshot.resolutions = []
            ws.snapshot.replies = {}
            st.session_state.agent_cache = {}
            add_audit("settings", "Business rules changed; agent recommendations cleared.")
            st.session_state.flash = "Settings saved. Python checks update immediately. Reprocess messages to refresh agent recommendations."
            st.rerun()
        except (ValueError, ValidationError) as exc:
            st.error(str(exc))
    st.divider()
    st.download_button("Save workspace backup", workspace_json(ws), "chat2order_workspace.json", "application/json")


{"Overview": overview, "Inbox": inbox, "Order desk": order_desk, "Agent studio": agent_studio, "Business settings": settings}[page]()
st.markdown('<div class="footer-note">Chat2Order · Clear conversations. Thoughtful decisions. · Hackathon MVP</div>', unsafe_allow_html=True)
