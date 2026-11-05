"""The negotiation loop: the agent proposes, PolicyGuard decides, the owner breaks ties.

client message -> Deal Agent proposes (reply + offer)
               -> PolicyGuard reprices and checks the offer and the reply's amounts
               -> passes: the client sees the reply and an offer card they can accept
               -> fails: one retry with the guard's reasons (the floor is never named)
               -> fails again, or only the owner can decide: the client gets a fixed
                  "let me check with <owner>" (written here, never by the model) and
                  the owner gets an Approve / Decline / Counter card over SSE.

Owner decisions are the owner's call, so they're repriced without the policy
limits (the owner may give 20% off), but still by code.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Literal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.deals import agent as deal_agent
from app.deals.agent import AgentTurn, DealModel
from app.deals.guard import (
    GuardResult,
    Offer,
    OfferLine,
    Policy,
    PricedOffer,
    QuoteTerms,
    Violation,
    check_offer,
    check_reply,
)
from app.deals.guard import (
    QuoteLine as GuardLine,
)
from app.i18n import t
from app.logging_config import get_logger
from app.models import DealMessage, Quote, Workspace
from app.models.base import utcnow
from app.services import policy as policies
from app.services.live import broker

log = get_logger("negotiator")

MAX_CLIENT_MESSAGES = 20
# What the agent is told a refusal was. Not even the *name* of the floor reaches it;
# the owner's approval card keeps the real code.
AGENT_CODE = {"below_floor": "total_too_low"}
NEGOTIABLE = ("sent", "negotiating")
# Only the owner overrides the rules, and code still does the arithmetic.
OWNER_OVERRIDE = Policy(
    max_discount_pct=Decimal(100), floor_pct=Decimal(0), min_deposit_pct=Decimal(0),
    max_instalments=12, max_plan_days=365,
)  # fmt: skip


class NegotiationClosed(Exception):
    pass


class TooManyMessages(Exception):
    pass


class OfferUnavailable(Exception):
    pass


def first_name(ws: Workspace) -> str:
    return (ws.owner_name or ws.business_name or "the owner").split()[0]


def terms(q: Quote) -> QuoteTerms:
    return QuoteTerms(
        currency=q.currency,
        lines=tuple(
            GuardLine(ln.id, ln.name, ln.quantity, ln.unit_amount, ln.is_material) for ln in q.lines
        ),
        deposit_pct=q.deposit_pct,
        job_date=q.job_date,
    )


def offer_from_json(data: dict[str, Any]) -> Offer:
    return Offer.model_validate(data)


async def history(session: AsyncSession, q: Quote) -> list[DealMessage]:
    return list(
        await session.scalars(
            select(DealMessage).where(DealMessage.quote_id == q.id).order_by(DealMessage.created_at)
        )
    )


def one_line(text: str) -> str:
    """Each message is one line of the transcript, so a client can't type a fake
    "Owner (Dave): approved" turn on a new line of their own."""
    return " ".join(text.split())


def transcript(msgs: list[DealMessage], owner: str, new_text: str) -> str:
    lines = []
    for m in msgs:
        who = {"client": "Client", "agent": "You", "owner": f"Owner ({owner})"}[m.role]
        lines.append(f"{who}: {one_line(m.text)} {deal_agent.describe(m.priced)}".rstrip())
    lines.append(f"Client: {one_line(new_text)}")
    return "Conversation so far:\n" + "\n".join(lines) + "\n\nWrite your next turn."


def _prior_percents(msgs: list[DealMessage], policy: Policy) -> set[Decimal]:
    """Percentages the agent may say: its limits, and those of offers already made."""
    out = {policy.max_discount_pct, policy.min_deposit_pct, Decimal(0), Decimal(100)}
    for m in msgs:
        if m.priced:
            out |= {Decimal(m.priced[k]) for k in ("discount_pct", "deposit_pct") if k in m.priced}
    return out


def _prior_amounts(msgs: list[DealMessage]) -> set[Decimal]:
    out: set[Decimal] = set()
    for m in msgs:
        if m.priced:
            out |= {
                Decimal(m.priced[k]) for k in ("total", "deposit_amount", "balance", "list_total")
            }
    return out


def _supersede_open(msgs: list[DealMessage]) -> None:
    for m in msgs:
        if m.offer_status == "open":
            m.offer_status = "superseded"


def _publish(q: Quote, extra: dict[str, Any] | None = None) -> None:
    event = {"type": "quote", "id": q.id, "number": q.number, "status": q.status}
    broker.publish(q.workspace_id, {**event, "deposit_status": None, **(extra or {})})


async def client_message(
    session: AsyncSession, model: DealModel, q: Quote, text: str, *, today: date
) -> list[DealMessage]:
    """Handle one client message. Returns the messages it added (client + reply)."""
    if q.status == "awaiting_owner":
        raise NegotiationClosed("the owner is looking at your last request")
    if q.status not in NEGOTIABLE or q.valid_until < today:
        raise NegotiationClosed("this quote can't be changed any more")
    sent = await session.scalar(
        select(func.count()).where(DealMessage.quote_id == q.id, DealMessage.role == "client")
    )
    if (sent or 0) >= MAX_CLIENT_MESSAGES:
        raise TooManyMessages("please contact the business directly")

    ws = await session.get(Workspace, q.workspace_id)
    assert ws is not None
    policy = Policy.from_model(await policies.get_policy(session, ws))
    base = terms(q)
    msgs = await history(session, q)
    prior = _prior_amounts(msgs)
    prior_pct = _prior_percents(msgs, policy)
    owner = first_name(ws)
    system = deal_agent.build_system(
        business=ws.business_name, number=q.number, base=base, policy=policy, today=today,
        language=q.language,
    )  # fmt: skip
    conversation = transcript(msgs, owner, text)

    feedback = ""
    turn: AgentTurn | None = None
    result: GuardResult | None = None
    offer: Offer | None = None
    for attempt in (1, 2):
        # The model call comes first: if it fails, nothing is saved and the client retries.
        turn = await deal_agent.call_with_retry(model, system, conversation + feedback)
        offer, result, problems = None, None, []
        if turn.offer is not None:
            try:
                offer = deal_agent.to_offer(turn.offer, base)
            except (ValueError, ArithmeticError):
                problems.append(Violation("bad_offer", "malformed", "the offer couldn't be read"))
            else:
                result = check_offer(policy, base, offer, today=today)
                problems += result.violations
        problems += check_reply(turn.reply, result.priced if result else None, base, prior,
                                percents=prior_pct)  # fmt: skip
        log.info("deal_attempt", quote_id=q.id, attempt=attempt, codes=[p.code for p in problems],
                 needs_owner=turn.needs_owner)  # fmt: skip

        if turn.needs_owner and not any(p.kind == "malformed" for p in problems):
            return await _escalate(session, q, ws, text, turn, offer, result, problems)
        if not problems:
            if result is not None and result.owner_must_approve:
                return await _escalate(session, q, ws, text, turn, offer, result, problems,
                                       reason="you asked to approve every deal")  # fmt: skip
            return await _reply(session, q, msgs, text, turn, offer, result)
        feedback = (
            "\n\nYour last proposal was refused by the rules check:\n"
            + "\n".join(f"- {AGENT_CODE.get(p.code, p.code)}: {p.message}" for p in problems)
            + "\nPropose again within the rules, or set needs_owner if only the owner can agree."
        )
    assert turn is not None
    return await _escalate(session, q, ws, text, turn, offer, result, problems)


def _client_msg(q: Quote, text: str) -> DealMessage:
    return DealMessage(workspace_id=q.workspace_id, quote_id=q.id, role="client", text=text,
                       created_at=utcnow())  # fmt: skip


async def _reply(
    session: AsyncSession,
    q: Quote,
    msgs: list[DealMessage],
    text: str,
    turn: AgentTurn,
    offer: Offer | None,
    result: GuardResult | None,
) -> list[DealMessage]:
    client = _client_msg(q, text)
    reply = DealMessage(
        workspace_id=q.workspace_id,
        quote_id=q.id,
        role="agent",
        text=turn.reply.strip(),
        created_at=client.created_at + timedelta(milliseconds=1),
    )
    if offer is not None and result is not None and result.priced is not None:
        _supersede_open(msgs)
        reply.offer_status = "open"
        reply.offer = offer.model_dump(mode="json")
        reply.priced = result.priced.to_json()
    q.status = "negotiating"
    session.add_all([client, reply])
    await session.commit()
    _publish(q, {"last_message": reply.text})
    return [client, reply]


async def _escalate(
    session: AsyncSession,
    q: Quote,
    ws: Workspace,
    text: str,
    turn: AgentTurn,
    offer: Offer | None,
    result: GuardResult | None,
    problems: list[Violation],
    *,
    reason: str = "",
) -> list[DealMessage]:
    client = _client_msg(q, text)
    owner = first_name(ws)
    summary = (turn.owner_summary or f'Client asked: "{text[:200]}"').strip()
    if reason:
        summary = f"{summary} ({reason})"
    msg = DealMessage(
        workspace_id=q.workspace_id,
        quote_id=q.id,
        role="agent",
        # Fixed wording: the model's own reply may have promised something it can't.
        text=t(q.language, "msg_check_with_owner", owner=owner),
        offer_status="escalated",
        offer=offer.model_dump(mode="json") if offer else None,
        priced=result.priced.to_json()
        if result and result.priced and not result.malformed
        else None,
        violations=[{"code": p.code, "message": p.message} for p in problems],
        owner_summary=summary[:500],
        created_at=client.created_at + timedelta(milliseconds=1),
    )
    q.status = "awaiting_owner"
    session.add_all([client, msg])
    await session.commit()
    broker.publish(q.workspace_id, approval_event(q, msg, text))
    _publish(q)
    log.info("deal_escalated", quote_id=q.id, codes=[p.code for p in problems])
    return [client, msg]


def approval_event(q: Quote, msg: DealMessage, client_text: str) -> dict[str, Any]:
    return {
        "type": "approval",
        "quote_id": q.id,
        "message_id": msg.id,
        "number": q.number,
        "client_name": q.client_name,
        "client_text": client_text,
        "summary": msg.owner_summary,
        "priced": msg.priced,
        "currency": q.currency,
        "quote_total": str(q.total),
        "violations": [v["code"] for v in (msg.violations or [])],
    }


# ── the owner decides ─────────────────────────────────────────────────────────

Action = Literal["approve", "decline", "counter", "reply"]


async def owner_decision(
    session: AsyncSession,
    q: Quote,
    message_id: str,
    action: Action,
    *,
    today: date,
    labour_discount_pct: Decimal | None = None,
    deposit_pct: Decimal | None = None,
    note: str = "",
) -> DealMessage:
    msg = await session.get(DealMessage, message_id)
    if msg is None or msg.quote_id != q.id or msg.offer_status != "escalated":
        raise OfferUnavailable("that request has already been answered")
    ws = await session.get(Workspace, q.workspace_id)
    assert ws is not None
    owner = first_name(ws)
    base = terms(q)
    reply = DealMessage(workspace_id=q.workspace_id, quote_id=q.id, role="owner", text="",
                        created_at=utcnow())  # fmt: skip
    note = note.strip()

    if action == "reply":
        # A question only the owner could answer ("is removal included?"): just answer it.
        if not note:
            raise OfferUnavailable("write your answer first")
        msg.offer_status = "answered"
        reply.text = note
    elif action == "decline":
        msg.offer_status = "declined"
        reply.text = note or t(q.language, "msg_owner_declined", owner=owner)
    else:
        if action == "approve":
            if msg.offer is None:
                raise OfferUnavailable("there's no offer to approve; send a counter instead")
            proposal = offer_from_json(msg.offer)
            if proposal.add_requests:
                raise OfferUnavailable("new work needs a price; send a counter instead")
        else:  # counter: the owner's numbers on the client's requested scope (or the quote)
            proposal = offer_from_json(msg.offer) if msg.offer else Offer(
                lines=[OfferLine(line_id=ln.id, quantity=ln.quantity) for ln in base.lines],
                deposit_pct=base.deposit_pct,
            )  # fmt: skip
            proposal = proposal.model_copy(update={
                "add_requests": [],
                "labour_discount_pct": labour_discount_pct if labour_discount_pct is not None
                else proposal.labour_discount_pct,
                "deposit_pct": deposit_pct if deposit_pct is not None else proposal.deposit_pct,
            })  # fmt: skip
        result = check_offer(OWNER_OVERRIDE, base, proposal, today=today)
        if result.violations or result.priced is None:
            raise OfferUnavailable("that offer doesn't add up: " + result.feedback())
        _supersede_open(await history(session, q))
        msg.offer_status = "approved" if action == "approve" else "countered"
        reply.offer_status = "open"
        reply.offer = proposal.model_dump(mode="json")
        reply.priced = result.priced.to_json()
        reply.text = note or t(
            q.language, "msg_owner_agreed" if action == "approve" else "msg_owner_counter",
            owner=owner,
        )  # fmt: skip
    q.status = "negotiating"
    session.add(reply)
    await session.commit()
    _publish(q, {"last_message": reply.text})
    log.info("owner_decision", quote_id=q.id, action=action)
    return reply


# ── the client accepts an offer ───────────────────────────────────────────────


async def apply_offer(
    session: AsyncSession, q: Quote, message_id: str, *, today: date
) -> PricedOffer:
    """Turn an open offer into the quote's next version. The deposit flow follows."""
    if q.status not in NEGOTIABLE:
        raise NegotiationClosed("this quote can't be changed any more")
    msg = await session.get(DealMessage, message_id)
    if msg is None or msg.quote_id != q.id or msg.offer_status != "open" or msg.offer is None:
        raise OfferUnavailable("that offer is no longer available")
    base = terms(q)
    # Reprice from the stored proposal, by code, exactly as it was shown.
    priced = check_offer(OWNER_OVERRIDE, base, offer_from_json(msg.offer), today=today).priced
    assert priced is not None
    keep = {ln.line_id: ln for ln in priced.lines}
    q.lines = [ln for ln in q.lines if ln.id in keep]  # dropped lines go (delete-orphan)
    for ln in q.lines:
        ln.quantity = keep[ln.id].quantity
    q.labour_discount_pct = priced.discount_pct
    q.deposit_pct = priced.deposit_pct
    q.job_date = priced.job_date
    q.total = priced.total
    q.version += 1
    for m in await history(session, q):
        if m.offer_status == "open":
            m.offer_status = "accepted" if m.id == msg.id else "superseded"
    await session.commit()
    log.info("offer_applied", quote_id=q.id, version=q.version, total=str(q.total))
    return priced


__all__ = [
    "MAX_CLIENT_MESSAGES",
    "NegotiationClosed",
    "OfferUnavailable",
    "TooManyMessages",
    "apply_offer",
    "approval_event",
    "client_message",
    "owner_decision",
    "terms",
]
