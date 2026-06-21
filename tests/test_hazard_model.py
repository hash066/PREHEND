"""Cox hazard-model tests (patent Section 8).

Uses a small hand-built covariate table (NOT synthetic EMG) to drive the model
wrapper deterministically: it checks fitting, the cold-start guard, monotonic
survival, the migration trigger, and the low-confidence (events-per-variable) flag.
"""
import numpy as np
import pandas as pd
import pytest

from adapt.commands import COVARIATES
from adapt.session_logger import to_cox_frame
from adapt.hazard_model import HazardModel, InsufficientEventsError


def _cov_row(user, cmd, t, acc, rms, dvar, gap, fneg):
    return {"user_id": user, "command": cmd, "session_t": t, "n_attempts": 12,
            "accuracy": acc, "mean_rms": rms, "dur_var": dvar,
            "inter_attempt_gap": gap, "false_neg_count": fneg}


def _failing(user, cmd):
    # accuracy declines below floor for the last 2 sessions -> one event;
    # other covariates given real variance and event-correlated drift.
    accs = [0.95, 0.85, 0.7, 0.45, 0.3]
    return [_cov_row(user, cmd, i + 1, a, 0.2 - 0.03 * i, 0.1 + 0.05 * i, 1.0 + 0.4 * i, i)
            for i, a in enumerate(accs)]


def _censored(user, cmd):
    accs = [0.95, 0.93, 0.9, 0.88, 0.86]
    return [_cov_row(user, cmd, i + 1, a, 0.2 + 0.01 * i, 0.1 + 0.01 * i, 1.0 + 0.05 * i, 0)
            for i, a in enumerate(accs)]


def _frame_with_events():
    rows = []
    for u in ("U1", "U2", "U3"):
        rows += _failing(u, "LONG")
    for u in ("U4", "U5", "U6"):
        rows += _censored(u, "SHORT")
    return to_cox_frame(pd.DataFrame(rows), floor=0.5)


def test_fit_succeeds_and_counts_events():
    cox = _frame_with_events()
    assert cox["event_failure"].sum() == 3
    model = HazardModel(penalizer=0.5).fit(cox)
    assert model.n_events_ == 3
    assert model.covariates_  # at least one usable covariate kept


def test_low_confidence_flag_when_few_events():
    model = HazardModel(penalizer=0.5).fit(_frame_with_events())
    assert model.low_confidence_ is True            # EPV well below 10
    assert model.epv_ == pytest.approx(3 / len(model.covariates_))


def test_survival_is_monotonically_non_increasing():
    model = HazardModel(penalizer=0.5).fit(_frame_with_events())
    cov = {c: 0.0 for c in COVARIATES}
    cov["accuracy"] = 0.3  # degraded -> should drop survival
    S = model.survival_function(cov, horizon=6)
    vals = S.values
    assert np.all(np.diff(vals) <= 1e-9)            # non-increasing
    assert "extrapolated" in S.attrs and any(S.attrs["extrapolated"])


def test_forecast_trigger_fires_for_degraded_covariates():
    model = HazardModel(penalizer=0.5).fit(_frame_with_events())
    degraded = {"accuracy": 0.2, "mean_rms": 0.05, "dur_var": 0.4,
                "inter_attempt_gap": 3.0, "false_neg_count": 5}
    res = model.forecast_trigger(degraded, threshold=0.7, horizon=6)
    assert res.flagged is True
    assert res.crossing_session is not None


def test_zero_events_raises_cold_start():
    rows = []
    for u in ("A", "B", "C"):
        rows += _censored(u, "SHORT")
    cox = to_cox_frame(pd.DataFrame(rows), floor=0.5)
    assert cox["event_failure"].sum() == 0
    with pytest.raises(InsufficientEventsError):
        HazardModel().fit(cox)
