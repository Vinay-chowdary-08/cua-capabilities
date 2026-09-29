"""Agent prompts — written for another agent, not for a brochure."""

from __future__ import annotations

SYSTEM = """You are discovering a reusable teller workflow on a hostile legacy
credit-union UI: framesets, nested tables, almost no stable IDs. Your job is to
reach the savings balance and stop — the recorder will turn your actions into a
capability artifact another process can replay without you.

Rules that matter:
- Exactly one tool call per turn. No parallel tools, no chatter-only replies.
- Click and type by mark number from the annotated screenshot. Do not invent CSS.
- Login already happened offline; you will not see password fields on purpose.
- When Share Savings (or the savings balance) is visible, extract it, then done.
- Irreversible confirm/submit → request_human with a short why.
- Same screen three times with no progress → request_human and say you are stuck.
Keep tool args tight. Prefer marks that match the goal, not decorative chrome.
"""

TOOLS = [
    {
        "name": "click",
        "description": "Click the element with this mark number from the annotated shot.",
        "input_schema": {
            "type": "object",
            "properties": {"mark": {"type": "integer"}},
            "required": ["mark"],
        },
    },
    {
        "name": "type",
        "description": (
            "Type into the marked field. Set is_param to the parameter name "
            "(e.g. member_number) when the text comes from the goal inputs."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "mark": {"type": "integer"},
                "text": {"type": "string"},
                "is_param": {"type": ["string", "null"]},
            },
            "required": ["mark", "text"],
        },
    },
    {
        "name": "select",
        "description": "Choose an option in a marked dropdown.",
        "input_schema": {
            "type": "object",
            "properties": {
                "mark": {"type": "integer"},
                "option": {"type": "string"},
            },
            "required": ["mark", "option"],
        },
    },
    {
        "name": "press",
        "description": "Press a key (usually Enter).",
        "input_schema": {
            "type": "object",
            "properties": {"key": {"type": "string"}},
            "required": ["key"],
        },
    },
    {
        "name": "extract",
        "description": "Read text from a mark into a named output (e.g. savings_balance).",
        "input_schema": {
            "type": "object",
            "properties": {
                "mark": {"type": "integer"},
                "output_name": {"type": "string"},
            },
            "required": ["mark", "output_name"],
        },
    },
    {
        "name": "done",
        "description": "Goal met. One short plain-language summary of what you did.",
        "input_schema": {
            "type": "object",
            "properties": {"summary": {"type": "string"}},
            "required": ["summary"],
        },
    },
    {
        "name": "request_human",
        "description": "Hand the live Chromium session to an operator. Say why.",
        "input_schema": {
            "type": "object",
            "properties": {"reason": {"type": "string"}},
            "required": ["reason"],
        },
    },
]


# Offline fallback when no API key — same path the live agent should take.
MOCK_READ_BALANCE = [
    {"tool": "click", "input": {"mark": "Member Search"}},
    {
        "tool": "type",
        "input": {"mark": "qry", "text": "{member_number}", "is_param": "member_number"},
    },
    {"tool": "click", "input": {"mark": "Find Member"}},
    {"tool": "click", "input": {"mark": "Open"}},
    {"tool": "extract", "input": {"mark": "Share Savings", "output_name": "savings_balance"}},
    {"tool": "done", "input": {"summary": "Opened the member and grabbed Share Savings."}},
]
