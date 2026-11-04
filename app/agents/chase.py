"""The Chase Agent's writer: an ADK agent that checks the invoice with PayPal's own
MCP tools, then words the reminder for its rung of the ladder.

Code has already decided *whether* to chase (see app.services.chase). The agent
only confirms with PayPal and writes the words, and code checks those too: any
money amount in the note must be one PayPal reported, or the plain template is
used instead. If ADK or the MCP server can't run, the template is used: chasing
never stops because an AI component is down.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any, Protocol

from app.deals.guard import amounts_in
from app.i18n import LANGUAGES, t
from app.i18n import base as base_lang
from app.logging_config import get_logger
from app.money import display, to_money
from app.settings import Settings

log = get_logger("chase_agent")

TONES = {
    1: ("friendly", "Warm and light. Assume it simply slipped their mind."),
    2: ("firm", "Polite but clear: it's now well overdue. Ask them to pay this week."),
    3: ("final", "A final reminder: ask them to pay within 7 days or get in touch. Calm, never "
                 "threatening: no legal action, no fees, no debt collectors."),
}  # fmt: skip


@dataclass(frozen=True)
class ReminderFacts:
    business: str
    owner: str
    client_name: str
    number: str
    paypal_invoice_id: str
    currency: str
    total: Decimal
    amount_paid: Decimal
    balance: Decimal
    due_date: date | None
    days_overdue: int
    step: int
    # On a payment plan: the instalment that's late (what's owed by its date), and that date.
    instalment: Decimal | None = None
    instalment_due: date | None = None
    language: str = "en"


@dataclass
class ReminderDraft:
    subject: str
    note: str
    source: str  # agent | template
    trace: dict[str, Any] = field(default_factory=dict)
    skip_reason: str = ""  # set when PayPal says there's nothing to chase


class ReminderWriter(Protocol):
    async def __call__(self, facts: ReminderFacts) -> ReminderDraft: ...


def subject_for(f: ReminderFacts) -> str:
    return t(f.language, f"subject_{f.step}", number=f.number, business=f.business)


def template_reminder(f: ReminderFacts, reason: str = "") -> ReminderDraft:
    """Plain, safe wording for each rung, in the client's language, when the agent can't write."""
    lang = f.language
    who = f.client_name.split()[0] if f.client_name else t(lang, "there")
    if f.instalment is not None and f.instalment_due is not None:
        d = f.instalment_due
        when = f"{d:%-d %b}" if base_lang(lang) == "en" else f"{d:%-d.%m}"
        amount = display(f.instalment, f.currency)
        body = t(lang, f"instalment_{f.step}", name=who, number=f.number, amount=amount, date=when)
    else:
        amount = display(f.balance, f.currency)
        body = t(lang, f"reminder_{f.step}", name=who, number=f.number, amount=amount,
                 days=f.days_overdue)  # fmt: skip
    return ReminderDraft(
        subject=subject_for(f),
        note=f"{body} {f.owner}".strip(),
        source="template",
        trace={"reason": reason} if reason else {},
    )


def allowed_amounts(f: ReminderFacts) -> set[Decimal]:
    known = [f.balance, f.total, f.amount_paid] + ([f.instalment] if f.instalment else [])
    return {to_money(a, f.currency) for a in known}


def check_note(note: str, f: ReminderFacts) -> str:
    """'' if the note is safe to send, else why not."""
    if not note.strip():
        return "empty note"
    if len(note) > 1000:
        return "note too long"
    wrong = [a for a in amounts_in(note) if to_money(a, f.currency) not in allowed_amounts(f)]
    if wrong:
        return f"amounts not on the invoice: {', '.join(map(str, wrong))}"
    return ""


INSTRUCTION = """\
You chase unpaid invoices for {business}, a small trades business, politely.

Invoice {number} (PayPal id {paypal_id}) for {client}: {days} days overdue.{plan}
Tone for this reminder: {tone_name}. {tone_guide}
Write the note in {language}, the client's language, using the polite form of
address where the language has one.

1. Call get_invoice with the PayPal id to check it is still unpaid and read the
   exact amount due. Don't rely on anything else for amounts.
2. Reply with only this JSON object and nothing else:
   {{"still_unpaid": true|false, "amount_due": "<from PayPal>",
     "note": "<the reminder, at most 4 short sentences, signed {owner}>"}}

The note is emailed by PayPal with a Pay button, so say they can pay by card or
PayPal from the email. Quote the amount due exactly as PayPal gave it. Never
threaten legal action, fees or debt collectors. Never invent facts.
"""


def plan_hint(f: ReminderFacts) -> str:
    if f.instalment is None:
        return ""
    return (
        f"\nThey are on a payment plan: the instalment of {display(f.instalment, f.currency)} "
        f"due {f.instalment_due} is what's late. Ask for that instalment, not the whole balance."
    )


def _json_from(text: str) -> dict[str, Any]:
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        raise ValueError("no JSON in the agent's reply")
    return json.loads(match.group(0))


class AdkChaseWriter(AbstractAsyncContextManager["AdkChaseWriter"]):
    """One PayPal MCP server per run, shared by every invoice in that run."""

    def __init__(self, settings: Settings, paypal_token: str) -> None:
        self.settings = settings
        self.token = paypal_token
        self.toolset: Any = None
        self.runner: Any = None
        self.sessions: Any = None

    async def __aenter__(self) -> AdkChaseWriter:
        from google.adk.agents import LlmAgent
        from google.adk.runners import Runner
        from google.adk.sessions import InMemorySessionService
        from google.adk.tools.mcp_tool import McpToolset
        from google.adk.tools.mcp_tool.mcp_session_manager import StdioConnectionParams
        from mcp import StdioServerParameters

        s = self.settings
        s.apply_google_env()
        argv = s.paypal_mcp_argv()
        self.toolset = McpToolset(
            connection_params=StdioConnectionParams(
                server_params=StdioServerParameters(
                    command=argv[0],
                    # Read-only on purpose: code, not the agent, sends the reminder.
                    args=[*argv[1:], "--tools=invoices.get,invoices.list"],
                    env={
                        "PAYPAL_ACCESS_TOKEN": self.token,
                        "PAYPAL_ENVIRONMENT": "PRODUCTION" if s.paypal_env == "live" else "SANDBOX",
                        "PATH": os.environ.get("PATH", ""),
                    },
                ),
                timeout=60,
            )
        )
        self._agent_cls, self._runner_cls = LlmAgent, Runner
        from google.genai import types

        level = s.gemini_thinking_level.strip().upper()
        # Same speed setting as the parser: a reminder doesn't need long deliberation.
        self._config = types.GenerateContentConfig(
            temperature=0.4,
            thinking_config=types.ThinkingConfig(thinking_level=types.ThinkingLevel(level))
            if level
            else None,
        )
        self.sessions = InMemorySessionService()
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self.toolset is not None:
            await self.toolset.close()

    async def __call__(self, f: ReminderFacts) -> ReminderDraft:
        from google.genai import types

        tone_name, tone_guide = TONES[f.step]
        agent = self._agent_cls(
            name="chase_agent",
            model=self.settings.gemini_model,
            instruction=INSTRUCTION.format(
                business=f.business,
                number=f.number,
                paypal_id=f.paypal_invoice_id,
                client=f.client_name or "the client",
                days=f.days_overdue,
                tone_name=tone_name,
                tone_guide=tone_guide,
                owner=f.owner,
                plan=plan_hint(f),
                language=LANGUAGES.get(base_lang(f.language), "English"),
            ),  # fmt: skip
            tools=[self.toolset],
            generate_content_config=self._config,
        )
        runner = self._runner_cls(agent=agent, app_name="sayinvoice", session_service=self.sessions)
        session_id = uuid.uuid4().hex
        await self.sessions.create_session(
            app_name="sayinvoice", user_id="chase", session_id=session_id
        )
        calls: list[dict[str, Any]] = []
        final = ""
        message = types.Content(
            role="user", parts=[types.Part(text="Check the invoice and write the reminder.")]
        )
        async for event in runner.run_async(
            user_id="chase", session_id=session_id, new_message=message
        ):
            for call in event.get_function_calls():
                calls.append({"tool": call.name, "args": dict(call.args or {})})
            if event.is_final_response() and event.content and event.content.parts:
                final = "".join(p.text or "" for p in event.content.parts)
        trace: dict[str, Any] = {"tool_calls": calls}
        try:
            out = _json_from(final)
        except (ValueError, json.JSONDecodeError):
            return template_reminder(f, "the agent's reply couldn't be read")
        trace["paypal_amount_due"] = str(out.get("amount_due", ""))
        if out.get("still_unpaid") is False:
            return ReminderDraft(subject="", note="", source="agent", trace=trace,
                                 skip_reason="PayPal says it's no longer unpaid")  # fmt: skip
        note = str(out.get("note", "")).strip()
        if problem := check_note(note, f):
            draft = template_reminder(f, f"agent note refused: {problem}")
            draft.trace |= trace
            return draft
        if not any("invoice" in c["tool"] for c in calls):
            trace["warning"] = "agent did not call a PayPal tool"
        return ReminderDraft(subject=subject_for(f), note=note, source="agent", trace=trace)
