"""ADAPT — Cox proportional-hazards forecasting (patent disclosure Section 8).

Phase 3 deliverable. Fits a time-varying Cox PH model (lifelines
CoxTimeVaryingFitter ONLY — CLAUDE.md rule 1, no hand-rolled hazard, no swapped
model) on the REAL per-command/per-session covariate table from session_logger,
then derives the survival function S(t) and the migration trigger.

Math (all standard; Section 8):
    h(t | x(t)) = h0(t) * exp(b . x(t))           (8.1)
    H(t)        = integral_0^t h(u) du             (8.4)
    S(t)        = exp(-H(t))                        (8.4)
The trigger is the first session at which S(t) < threshold (default 0.7).

lifelines 0.30.x note: CoxTimeVaryingFitter has no predict_survival_function, so
S(t) is built explicitly from the Breslow baseline cumulative hazard and the
covariate partial hazard — exactly equations 8.1/8.4, not an approximation.

Honesty: every coefficient/threshold is fit on a REAL multi-session sEMG proxy
(Ninapro DB6 / GRABMyo), which is a proxy for disease-driven decline, NOT disease
data (patent Section 16). If there are too few observed failure events to fit,
this module says so (the documented cold-start problem) rather than inventing one.

Few-event caveat (events-per-variable): a Cox model needs roughly >=10 events per
covariate for stable coefficients. On this proxy data events are scarce, so fits
with only a handful of events are reported with an explicit low-confidence flag and
any lifelines convergence/separation warnings are surfaced, NOT hidden. The L2
penalizer is load-bearing under separation, not cosmetic. Coefficients in that
regime are essentially unidentified and must not be read as validated effects.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd

from lifelines import CoxTimeVaryingFitter

from .commands import COVARIATES, SURVIVAL_THRESHOLD


class InsufficientEventsError(RuntimeError):
    """Raised when the real data contains too few failure events to fit a Cox model."""


@dataclass
class TriggerResult:
    crossing_session: Optional[int]   # first ordinal session where S(t) < threshold, else None
    survival: pd.Series               # S(t) indexed by interval stop time (session ordinal)
    flagged: bool


class HazardModel:
    """Time-varying Cox PH model over the five ADAPT covariates."""

    # rule-of-thumb minimum events-per-variable for a "confident" Cox fit
    MIN_EVENTS_PER_VARIABLE = 10

    def __init__(self, penalizer: float = 0.1, covariates: tuple[str, ...] = COVARIATES):
        self.penalizer = penalizer
        self.requested_covariates = list(covariates)
        self.covariates_: list[str] = []      # those actually used (non-degenerate)
        self.dropped_: list[str] = []
        self.ctv: Optional[CoxTimeVaryingFitter] = None
        self.n_events_: int = 0
        self.n_trajectories_: int = 0
        self.epv_: float = 0.0                 # events per variable
        self.low_confidence_: bool = False     # True when EPV is below the rule of thumb
        self.fit_warnings_: list[str] = []     # lifelines convergence/separation warnings, surfaced
        self.mean_: dict[str, float] = {}      # per-covariate training mean (for z-scoring)
        self.std_: dict[str, float] = {}       # per-covariate training std

    # -- fitting --------------------------------------------------------------
    def fit(self, cox_df: pd.DataFrame, min_var: float = 1e-9) -> "HazardModel":
        """Fit on a CoxTimeVaryingFitter frame (id, start, stop, event_failure, covariates).

        Drops covariates with ~zero variance (a Cox model cannot identify them and
        lifelines would raise); the dropped list is recorded and reported, never
        hidden. Raises InsufficientEventsError if no failure events are present.
        """
        required = {"id", "start", "stop", "event_failure"}
        missing = required - set(cox_df.columns)
        if missing:
            raise ValueError(f"cox_df missing columns: {missing}")

        self.n_events_ = int(cox_df["event_failure"].sum())
        self.n_trajectories_ = cox_df["id"].nunique()
        if self.n_events_ < 1:
            raise InsufficientEventsError(
                f"0 failure events across {self.n_trajectories_} trajectories — cannot fit a "
                f"Cox PH model. This is the documented cold-start case (patent Section 16): "
                f"on this real proxy data no command crossed the accuracy floor for 2 consecutive "
                f"sessions. Reporting censored trajectories only; no fabricated events added."
            )

        # Select identifiable covariates and z-score them. Standardisation makes the
        # degeneracy check scale-free (DB6's EMG units are ~1e-5, so an ABSOLUTE
        # variance floor wrongly drops informative columns), improves Cox numerics,
        # and makes coefficients comparable (per-SD hazard ratios).
        self.covariates_, self.dropped_ = [], []
        self.mean_, self.std_ = {}, {}
        for c in self.requested_covariates:
            if c not in cox_df.columns:
                self.dropped_.append(c)
                continue
            vals = cox_df[c].astype(float).values
            sd = float(np.std(vals))
            if sd <= 1e-12 or float(np.ptp(vals)) <= 0:   # truly (near-)constant column
                self.dropped_.append(c)
                continue
            self.covariates_.append(c)
            self.mean_[c] = float(np.mean(vals))
            self.std_[c] = sd
        if not self.covariates_:
            raise ValueError("no usable (non-degenerate) covariates to fit.")

        std_df = cox_df[["id", "start", "stop", "event_failure"]].copy()
        for c in self.covariates_:
            std_df[c] = (cox_df[c].astype(float) - self.mean_[c]) / self.std_[c]
        self.ctv = CoxTimeVaryingFitter(penalizer=self.penalizer)
        # Capture (do not swallow) lifelines convergence/separation warnings so the
        # few-event regime is surfaced rather than hidden (stats-verifier Finding 2).
        import warnings as _w
        with _w.catch_warnings(record=True) as caught:
            _w.simplefilter("always")
            self.ctv.fit(
                std_df,
                id_col="id",
                event_col="event_failure",
                start_col="start",
                stop_col="stop",
            )
        self.fit_warnings_ = [str(x.message) for x in caught]
        self.epv_ = self.n_events_ / max(1, len(self.covariates_))
        self.low_confidence_ = self.epv_ < self.MIN_EVENTS_PER_VARIABLE
        # invariant: the z-score scaler keys must stay in lock-step with the fitted
        # covariates, or survival_function would read a wrong/missing scale.
        assert set(self.covariates_) <= set(self.mean_) and set(self.covariates_) <= set(self.std_)
        return self

    # -- summaries ------------------------------------------------------------
    @property
    def summary(self) -> pd.DataFrame:
        if self.ctv is None:
            raise RuntimeError("model not fit")
        return self.ctv.summary

    def coefficients(self) -> pd.Series:
        return self.summary["coef"]

    # -- survival from baseline cumulative hazard (eq. 8.1 / 8.4) -------------
    def _baseline_cumulative_hazard(self) -> pd.Series:
        bch = self.ctv.baseline_cumulative_hazard_
        if isinstance(bch, pd.DataFrame):
            bch = bch.iloc[:, 0]
        return bch.sort_index()

    def survival_function(self, covariates: dict, horizon: int = 6) -> pd.Series:
        """S(t) = exp( -H0(t) * exp(b.(x - xbar)) ) for a held-constant covariate vector.

        lifelines' baseline_cumulative_hazard_ (Breslow) is defined ONLY at observed
        failure times, so on its own it cannot forecast past the last event. To honour
        the 'project current state forward' intent (covariate values held at the latest
        session, since future values are unknown), H0(t) is linearly EXTRAPOLATED onto a
        future session grid up to last_event + ``horizon``. Extrapolated points are
        clearly low-confidence and flagged on the returned Series' ``.attrs['extrapolated']``.

        Missing covariates default to the TRAINING MEAN (neutral, partial hazard = 1),
        not 0, so a partial covariate dict can't silently imply, e.g., zero accuracy.
        """
        if self.ctv is None:
            raise RuntimeError("model not fit")
        # Standardise the input with the stored training mean/std. A missing covariate
        # falls back to the training mean -> standardised 0 -> neutral (partial hazard 1),
        # never a misleading literal 0.
        row = pd.DataFrame([{c: (covariates.get(c, self.mean_[c]) - self.mean_[c]) / self.std_[c]
                             for c in self.covariates_}])
        partial = float(self.ctv.predict_partial_hazard(row).iloc[0])

        H0 = self._baseline_cumulative_hazard()
        times = [float(t) for t in H0.index]
        vals = [float(v) for v in H0.values]
        extrap = [False] * len(times)
        if horizon and times:
            last_t, last_h = times[-1], vals[-1]
            # average-hazard slope anchored at H(0)=0 (more defensible than anchoring at
            # the first observed event time, and consistent with the single-event case).
            slope = max(last_h / max(last_t, 1.0), 0.0)
            for k in range(1, int(horizon) + 1):
                times.append(last_t + k)
                vals.append(last_h + slope * k)
                extrap.append(True)
        S = np.exp(-np.asarray(vals) * partial)
        out = pd.Series(S, index=times, name="survival")
        out.attrs["extrapolated"] = extrap
        return out

    def forecast_trigger(self, covariates: dict,
                         threshold: float = SURVIVAL_THRESHOLD,
                         horizon: int = 6) -> TriggerResult:
        """Compute S(t) (with forward extrapolation) and the first session S(t) < threshold.

        S(t) is monotonically non-increasing (H0 non-decreasing, partial hazard > 0), so the
        first crossing is well-defined. crossing_session is in re-based per-trajectory ordinal
        session space (see to_cox_frame), not necessarily a user's calendar session number.
        """
        S = self.survival_function(covariates, horizon=horizon)
        below = S[S < threshold]
        crossing = int(below.index[0]) if len(below) else None
        return TriggerResult(crossing_session=crossing, survival=S, flagged=crossing is not None)


# --- convenience: per-command current survival for one user -----------------
def latest_covariates_by_command(cox_df: pd.DataFrame, user_id: str) -> dict[str, dict]:
    """Return {command: latest-session covariate dict} for a user's trajectories."""
    out: dict[str, dict] = {}
    sub = cox_df[cox_df["user_id"] == user_id]
    for cmd, g in sub.groupby("command"):
        last = g.sort_values("stop").iloc[-1]
        out[cmd] = {c: float(last[c]) for c in COVARIATES if c in g.columns}
    return out


def survival_scores_for_user(model: HazardModel, cox_df: pd.DataFrame, user_id: str,
                             horizon: int = 0) -> dict[str, float]:
    """{command: S(t)} for one user — the score the migration policy thresholds against.

    Default horizon=0 uses the *current-risk* survival: S evaluated at the last observed
    baseline-hazard time given the command's latest covariates (no forward extrapolation).
    This is the model's assessment of how survivable each command is by the end of the
    observation window, and it differentiates healthy vs failing commands without the
    artefacts of long-horizon extrapolation. Pass horizon>0 to project further out.
    """
    scores: dict[str, float] = {}
    for cmd, cov in latest_covariates_by_command(cox_df, user_id).items():
        S = model.survival_function(cov, horizon=horizon)
        scores[cmd] = float(S.iloc[-1]) if len(S) else 1.0
    return scores


def main(argv=None):
    import argparse

    ap = argparse.ArgumentParser(description="Fit ADAPT Cox PH model on a real covariate table.")
    ap.add_argument("--cox", required=True, help="Cox time-varying frame CSV (from session_logger)")
    ap.add_argument("--penalizer", type=float, default=0.1)
    ap.add_argument("--threshold", type=float, default=SURVIVAL_THRESHOLD)
    args = ap.parse_args(argv)

    cox_df = pd.read_csv(args.cox)
    print(f"trajectories: {cox_df['id'].nunique()}  rows: {len(cox_df)}  "
          f"events: {int(cox_df['event_failure'].sum())}")
    model = HazardModel(penalizer=args.penalizer)
    try:
        model.fit(cox_df)
    except InsufficientEventsError as e:
        print("\nCANNOT FIT (honest result):\n" + str(e))
        return
    print(f"\nfit on covariates: {model.covariates_}  (dropped: {model.dropped_})")
    print(f"events-per-variable (EPV): {model.epv_:.2f}  "
          f"(rule of thumb >= {model.MIN_EVENTS_PER_VARIABLE})")
    if model.low_confidence_:
        print("** LOW CONFIDENCE: too few events per covariate - coefficients are essentially\n"
              "   unidentified; treat directions/p-values as illustrative, NOT validated (Section 16).")
    for w in model.fit_warnings_:
        print(f"   [lifelines warning] {w.splitlines()[0]}")
    print("\n--- Cox summary ---")
    with pd.option_context("display.width", 160):
        print(model.summary[["coef", "exp(coef)", "se(coef)", "p"]])
    for user in sorted(cox_df["user_id"].unique()):
        scores = survival_scores_for_user(model, cox_df, user)
        print(f"\n{user}: S(t) by command (forecast horizon) -> "
              + ", ".join(f"{k}={v:.2f}" for k, v in scores.items()))


if __name__ == "__main__":
    main()
