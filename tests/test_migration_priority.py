"""Migration priority + escalation tests (patent Section 10.1-10.3, CLAUDE.md rule 5).

Priority SHORT > DOUBLE > LONG. A flagged lower-priority command migrates to a
double-tap of the healthiest higher-priority command. If SHORT itself is flagged,
escalate instead of compressing further.
"""
from adapt.commands import ESCALATE
from adapt.migration_engine import select_substitute, decide_migrations


def test_long_substitutes_to_short_when_short_healthy():
    sub = select_substitute("LONG", ["SHORT", "DOUBLE"], {"SHORT": 0.9, "DOUBLE": 0.8})
    assert sub == "SHORT_SHORT"


def test_long_falls_back_to_double_when_short_unhealthy():
    # Only DOUBLE healthy among higher-priority candidates -> DOUBLE_DOUBLE.
    sub = select_substitute("LONG", ["DOUBLE"], {"DOUBLE": 0.8})
    assert sub == "DOUBLE_DOUBLE"


def test_double_substitutes_to_short():
    assert select_substitute("DOUBLE", ["SHORT"], {"SHORT": 0.95}) == "SHORT_SHORT"


def test_short_flagged_escalates():
    # SHORT has no higher-priority command to migrate to.
    assert select_substitute("SHORT", [], {}) == ESCALATE


def test_decide_migrations_flags_below_threshold_only():
    decisions = decide_migrations({"SHORT": 0.9, "DOUBLE": 0.6, "LONG": 0.5}, threshold=0.7)
    flagged = {d.flagged_command: d for d in decisions}
    assert set(flagged) == {"DOUBLE", "LONG"}          # SHORT (0.9) not flagged
    assert flagged["LONG"].action == "SUBSTITUTE"
    assert flagged["LONG"].substitute_pattern == "SHORT_SHORT"
    assert flagged["LONG"].target_logical == "HOME"
    assert flagged["DOUBLE"].target_logical == "BACK"


def test_decide_migrations_lowest_priority_first():
    decisions = decide_migrations({"SHORT": 0.9, "DOUBLE": 0.6, "LONG": 0.5}, threshold=0.7)
    order = [d.flagged_command for d in decisions]
    assert order.index("LONG") < order.index("DOUBLE")  # LONG migrated first


def test_decide_migrations_short_escalates():
    decisions = decide_migrations({"SHORT": 0.5, "DOUBLE": 0.9, "LONG": 0.9}, threshold=0.7)
    assert len(decisions) == 1
    d = decisions[0]
    assert d.flagged_command == "SHORT" and d.action == "ESCALATE"
    assert d.remap_line() is None  # escalation never produces a wire remap


def test_substitute_decision_emits_correct_remap_line():
    d = decide_migrations({"SHORT": 0.95, "DOUBLE": 0.9, "LONG": 0.4}, threshold=0.7)[0]
    assert d.remap_line() == "REMAP,HOME,SHORT_SHORT\n"
