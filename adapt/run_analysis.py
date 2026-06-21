"""ADAPT — end-to-end analysis driver (real data only).

Builds the per-command/per-session covariate tables from the REAL downloaded
datasets (Ninapro DB6 and/or GRABMyo), pools them into one CoxTimeVaryingFitter
frame, fits the hazard model, and prints the per-user survival forecasts and the
migration/escalation decisions the policy would take. Writes derived tables to
adapt/derived/.

Pooling note: each (user, command) trajectory is an independent 'individual'
(id = "user|command") on the shared session-ordinal time axis, so DB6 and GRABMyo
trajectories pool cleanly even though they have different session counts.

No synthetic data. If the pooled data has too few failure events to fit, the model
says so (cold-start) rather than inventing events.
"""
from __future__ import annotations

import argparse
import os

import pandas as pd

from .session_logger import build_from_db6, build_from_grabmyo
from .hazard_model import HazardModel, InsufficientEventsError, survival_scores_for_user
from .migration_engine import decide_migrations


def _parse_int_list(spec: str) -> list[int]:
    out: list[int] = []
    for part in (spec or "").split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-"); out += list(range(int(a), int(b) + 1))
        elif part:
            out.append(int(part))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="ADAPT end-to-end analysis on real data.")
    ap.add_argument("--grabmyo-root", default=None)
    ap.add_argument("--grabmyo-subjects", default="1-4")
    ap.add_argument("--db6-root", default=None)
    ap.add_argument("--db6-subjects", default="1")
    ap.add_argument("--penalizer", type=float, default=0.1)
    ap.add_argument("--out-dir", default="adapt/derived")
    args = ap.parse_args(argv)

    os.makedirs(args.out_dir, exist_ok=True)
    datasets: list[tuple[str, pd.DataFrame]] = []

    if args.grabmyo_root:
        cov, cox = build_from_grabmyo(args.grabmyo_root, _parse_int_list(args.grabmyo_subjects))
        cov.to_csv(os.path.join(args.out_dir, "grabmyo_covariates.csv"), index=False)
        cox.to_csv(os.path.join(args.out_dir, "grabmyo_covariates_cox.csv"), index=False)
        n_ev = int(cox["event_failure"].sum()) if not cox.empty else 0
        print(f"GRABMyo: {len(cov)} covariate rows, {n_ev} events")
        if not cox.empty:
            datasets.append(("GRABMyo", cox))

    if args.db6_root:
        cov, cox = build_from_db6(args.db6_root, _parse_int_list(args.db6_subjects))
        cov.to_csv(os.path.join(args.out_dir, "db6_covariates.csv"), index=False)
        cox.to_csv(os.path.join(args.out_dir, "db6_covariates_cox.csv"), index=False)
        n_ev = int(cox["event_failure"].sum()) if not cox.empty else 0
        print(f"DB6: {len(cov)} covariate rows, {n_ev} events")
        if not cox.empty:
            datasets.append(("DB6", cox))

    if not datasets:
        print("No usable covariate data (empty/cold-start) for any dataset provided.")
        return

    # Save a pooled frame for reference, but FIT PER DATASET: DB6 (10 sessions) and
    # GRABMyo (3 sessions) live on different session-count axes, so a single pooled
    # baseline hazard mixes incompatible time grids (stats-verifier Finding 3). Per-
    # dataset fits keep the time axis coherent.
    pd.concat([c for _n, c in datasets], ignore_index=True).to_csv(
        os.path.join(args.out_dir, "pooled_cox.csv"), index=False)

    for name, cox in datasets:
        print(f"\n================ {name} hazard fit ================")
        model = HazardModel(penalizer=args.penalizer)
        try:
            model.fit(cox)
        except InsufficientEventsError as e:
            print("CANNOT FIT (honest result):\n" + str(e))
            continue
        print(f"trajectories={cox['id'].nunique()} events={model.n_events_} "
              f"EPV={model.epv_:.2f} low_confidence={model.low_confidence_} dropped={model.dropped_}")
        if model.low_confidence_:
            print("** Coefficients below are illustrative, NOT validated (few events; Section 16).")
        with pd.option_context("display.width", 160):
            print(model.summary[["coef", "exp(coef)", "se(coef)", "p"]])

        print(f"\n--- {name}: per-user current-risk S(t) + migration policy ---")
        for user in sorted(cox["user_id"].unique()):
            scores = survival_scores_for_user(model, cox, user)  # horizon=0: current risk
            decisions = decide_migrations(scores)
            line = ", ".join(f"{k}={v:.2f}" for k, v in sorted(scores.items()))
            print(f"\n{user}: S(t) -> {line}")
            for d in decisions:
                if d.action == "ESCALATE":
                    print(f"   ESCALATE {d.flagged_command} (S={d.survival:.2f}): no healthier "
                          f"higher-priority command available (Section 10.3)")
                else:
                    print(f"   MIGRATE  {d.flagged_command} (S={d.survival:.2f}) -> "
                          f"{d.substitute_pattern}  [{d.remap_line().strip()}]")


if __name__ == "__main__":
    main()
