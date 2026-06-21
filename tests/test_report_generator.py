"""Caregiver report generator tests (offline / no network).

Exercises the deterministic rendering and the no-LLM path. Does not call Nebius.
"""
from adapt.report_generator import render_event_text, generate_caregiver_report


MIGRATION = {
    "kind": "migration",
    "user_id": "GRABMyo_p2",
    "command": "LONG",
    "logical": "HOME",
    "substitute_pattern": "SHORT_SHORT",
    "survival": 0.62,
    "forecast_session": 5,
    "dataset": "Ninapro DB6 (proxy)",
}

ESCALATION = {
    "kind": "escalation",
    "user_id": "GRABMyo_p2",
    "command": "SHORT",
    "logical": "SELECT",
    "survival": 0.55,
    "dataset": "Ninapro DB6 (proxy)",
}


def test_migration_text_is_factual_and_safe():
    txt = render_event_text(MIGRATION)
    assert "LONG" in txt and "SHORT_SHORT" in txt and "HOME" in txt
    assert "0.62" in txt
    assert "not a medical assessment" in txt.lower()


def test_escalation_text_mentions_no_further_compression():
    txt = render_event_text(ESCALATION)
    assert "escalation" in txt.lower()
    assert "not compress" in txt.lower() or "won't compress" in txt.lower()
    assert "not a medical assessment" in txt.lower()


def test_generate_without_llm_matches_offline_rendering():
    assert generate_caregiver_report(MIGRATION, use_llm=False) == render_event_text(MIGRATION)


def test_generate_no_key_falls_back_with_marker(monkeypatch):
    monkeypatch.delenv("NEBIUS_API_KEY", raising=False)
    out = generate_caregiver_report(MIGRATION, use_llm=True, allow_offline_fallback=True)
    assert "offline template" in out.lower()
    assert "LONG" in out
