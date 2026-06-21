"""Censoring + interval-correctness tests for the Cox time-varying frame.

These exercise adapt.session_logger.to_cox_frame, which converts a per-session
covariate table into CoxTimeVaryingFitter input (patent Section 7.3 event rule).
The fixtures are small hand-built covariate tables (NOT synthetic EMG) used purely
to pin the censoring/interval algorithm — the covariate VALUES on real runs always
come from real recordings.
"""
import pandas as pd

from adapt.commands import COVARIATES
from adapt.session_logger import to_cox_frame


def _table(user, command, accuracies):
    """Build a minimal covariate table for one trajectory with given accuracies."""
    rows = []
    for i, acc in enumerate(accuracies, start=1):
        row = {"user_id": user, "command": command, "session_t": i, "n_attempts": 7}
        for c in COVARIATES:
            row[c] = acc if c == "accuracy" else 0.1  # other covariates immaterial here
        rows.append(row)
    return pd.DataFrame(rows)


def test_intervals_are_contiguous_unit_intervals():
    cox = to_cox_frame(_table("U", "LONG", [0.9, 0.8, 0.7]), floor=0.5)
    assert list(cox["start"]) == [0, 1, 2]
    assert list(cox["stop"]) == [1, 2, 3]
    assert (cox["stop"] - cox["start"] == 1).all()


def test_censored_trajectory_has_no_event_and_keeps_all_rows():
    cox = to_cox_frame(_table("U", "LONG", [0.9, 0.8, 0.7, 0.6, 0.55]), floor=0.5)
    assert len(cox) == 5
    assert cox["event_failure"].sum() == 0


def test_failure_after_two_consecutive_below_floor():
    # below floor at sessions 4 and 5 -> event on the 5th interval (ordinal index 4).
    cox = to_cox_frame(_table("U", "LONG", [0.9, 0.8, 0.6, 0.4, 0.3]), floor=0.5)
    assert cox["event_failure"].sum() == 1
    ev = cox[cox["event_failure"] == 1].iloc[0]
    assert ev["stop"] == 5            # ordinal interval [4,5)
    assert ev["session_t"] == 5
    assert len(cox) == 5              # no rows dropped (event is on the last)


def test_rows_after_event_are_dropped():
    # below floor at sessions 2,3 -> event at session 3; sessions 4,5 dropped.
    cox = to_cox_frame(_table("U", "LONG", [0.9, 0.4, 0.3, 0.4, 0.3]), floor=0.5)
    assert len(cox) == 3
    assert cox["event_failure"].sum() == 1
    assert cox[cox["event_failure"] == 1].iloc[0]["session_t"] == 3


def test_transient_dip_is_not_a_failure():
    # single-session dips never reach 2 consecutive -> censored (Section 7.3 rationale).
    cox = to_cox_frame(_table("U", "LONG", [0.9, 0.4, 0.9, 0.4, 0.9]), floor=0.5)
    assert cox["event_failure"].sum() == 0
    assert len(cox) == 5


def test_immediate_failure_at_second_session():
    cox = to_cox_frame(_table("U", "LONG", [0.4, 0.3, 0.9]), floor=0.5)
    assert cox["event_failure"].sum() == 1
    ev = cox[cox["event_failure"] == 1].iloc[0]
    assert ev["session_t"] == 2
    assert len(cox) == 2


def test_multiple_trajectories_get_distinct_ids():
    df = pd.concat([_table("U1", "LONG", [0.9, 0.4, 0.3]),
                    _table("U2", "SHORT", [0.9, 0.9, 0.9])], ignore_index=True)
    cox = to_cox_frame(df, floor=0.5)
    assert set(cox["id"]) == {"U1|LONG", "U2|SHORT"}
    assert cox.groupby("id")["event_failure"].sum().to_dict() == {"U1|LONG": 1, "U2|SHORT": 0}
