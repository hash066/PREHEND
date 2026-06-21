"""ADAPT — caregiver report generator (Nebius Token Factory).

The project's single LLM use (CLAUDE.md rule 7): turn a migration/escalation
event into a short, plain-language notification for a caregiver/user. Nebius
Token Factory is an OpenAI-compatible inference host; the API key comes from the
NEBIUS_API_KEY environment variable and is NEVER hardcoded.

The Cox hazard model and the EMG classifier are NOT hosted here — this module
only phrases an already-decided event.

Safety (patent Section 16): the notification must describe command-CLASSIFICATION
reliability, not a clinical/diagnostic assessment of disease progression. The
prompt enforces this and the deterministic fallback respects it too.
"""
from __future__ import annotations

import os
from typing import Optional

from .commands import DEFAULT_LOGICAL, ESCALATE

NEBIUS_BASE_URL = "https://api.tokenfactory.nebius.com/v1/"
# Configurable; default is a small fast instruct model confirmed in the Nebius
# catalog. Override with NEBIUS_MODEL. Check docs.tokenfactory.nebius.com for the
# current catalog before relying on a specific id.
DEFAULT_MODEL = os.environ.get("NEBIUS_MODEL", "meta-llama/Meta-Llama-3.1-8B-Instruct-fast")

_SYSTEM_PROMPT = (
    "You are an assistive-technology aide writing a brief, calm notification to a "
    "caregiver about a single-channel EMG switch-access device. Write 2-4 short "
    "sentences in plain language. Explain what the system is doing and what (if "
    "anything) the caregiver should do. IMPORTANT CONSTRAINTS: this is a report "
    "about how reliably a control GESTURE is being recognised by the device, NOT a "
    "medical or diagnostic assessment of the person's health or disease. Do not give "
    "medical advice, do not diagnose, do not speculate about prognosis. Be reassuring "
    "and concrete."
)


def render_event_text(event: dict) -> str:
    """Deterministic, factual rendering of an event — no LLM involved.

    Serves two purposes: (1) the structured content handed to the LLM, and
    (2) an honest offline fallback when no API key / network is available. It is
    explicitly NOT presented as LLM-generated.
    """
    kind = event.get("kind")
    command = event.get("command", "?")
    logical = event.get("logical") or DEFAULT_LOGICAL.get(command, "?")
    user = event.get("user_id", "the user")
    s = event.get("survival")
    s_txt = f"{s:.2f}" if isinstance(s, (int, float)) else "n/a"
    horizon = event.get("forecast_session")
    dataset = event.get("dataset")
    proxy_note = f" (model fitted on {dataset})" if dataset else ""

    if kind == "escalation" or event.get("substitute_pattern") == ESCALATE:
        return (
            f"ADAPT escalation for {user}: the most basic control gesture "
            f"('{command}' -> {logical}) is itself becoming less reliably recognised "
            f"(survival estimate S={s_txt}){proxy_note}. The device will not compress "
            f"the command set further. Recommend reviewing electrode placement and the "
            f"input site/modality with the user. This describes gesture-recognition "
            f"reliability only, not a medical assessment."
        )
    # migration
    sub = event.get("substitute_pattern", "a compound pattern")
    when = f" around session {horizon}" if horizon is not None else ""
    return (
        f"ADAPT notice for {user}: the '{command}' gesture (controls {logical}) is "
        f"trending toward unreliable recognition{when} (survival estimate S={s_txt})"
        f"{proxy_note}. To stay ahead of it, the device is switching {logical} to be "
        f"triggered by '{sub}' instead, with a short guided-practice period. No action "
        f"needed unless the user finds the new pattern difficult. This describes "
        f"gesture-recognition reliability only, not a medical assessment."
    )


def _build_messages(event: dict) -> list[dict]:
    return [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                "Write the caregiver notification for this event. Here is the factual "
                "summary to base it on (do not invent details beyond it):\n\n"
                + render_event_text(event)
            ),
        },
    ]


def generate_caregiver_report(
    event: dict,
    *,
    use_llm: bool = True,
    model: str = DEFAULT_MODEL,
    allow_offline_fallback: bool = True,
    client=None,
    temperature: float = 0.4,
) -> str:
    """Generate a plain-language caregiver notification for a migration/escalation.

    Args:
        event: dict describing the event. Recognised keys: kind ("migration" |
            "escalation"), user_id, command, logical, substitute_pattern,
            survival (S(t)), forecast_session, dataset.
        use_llm: if False, returns the deterministic offline rendering.
        model: Nebius model id.
        allow_offline_fallback: if the API call fails and this is True, return the
            deterministic rendering (clearly marked) instead of raising.
        client: optional pre-built OpenAI-compatible client (for testing/injection).

    Returns the notification text. Never returns a hardcoded-secret-bearing string.
    """
    if not use_llm:
        return render_event_text(event)

    api_key = os.environ.get("NEBIUS_API_KEY")
    if client is None and not api_key:
        if allow_offline_fallback:
            return render_event_text(event) + (
                "\n\n[offline template — set NEBIUS_API_KEY for LLM-phrased output]"
            )
        raise RuntimeError("NEBIUS_API_KEY is not set and no client was provided.")

    try:
        if client is None:
            from openai import OpenAI

            client = OpenAI(base_url=NEBIUS_BASE_URL, api_key=api_key)
        resp = client.chat.completions.create(
            model=model,
            messages=_build_messages(event),
            temperature=temperature,
            max_tokens=220,
        )
        return resp.choices[0].message.content.strip()
    except Exception as exc:  # network / auth / model errors
        if allow_offline_fallback:
            return render_event_text(event) + f"\n\n[LLM unavailable ({exc.__class__.__name__}); offline template used]"
        raise
