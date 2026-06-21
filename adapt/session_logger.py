"""ADAPT — Session Logger (patent disclosure Section 7; architecture Layer 3).

Phase 2 deliverable. Computes the five per-command, per-session reliability
covariates (Section 7.1) from REAL recorded multi-session sEMG, then assembles
the time-varying table the Cox model consumes (Section 13 schema).

Covariate operationalisation on benchmark datasets (stated honestly, because a
live SIGNAL deployment would measure some of these directly that here must be
derived from fixed-protocol recordings):

  accuracy          Per-session classification accuracy for command class c.
                    A single-channel LDA is trained on the user's BASELINE session
                    and tested on each later session; accuracy therefore reflects
                    real inter-session signal change (electrode shift / non-
                    stationarity) — the documented proxy for decline (Section 5.1).
  mean_rms          Mean RMS amplitude of the detected active burst across class-c
                    trials in the session (motor-unit dropout proxy).
  dur_var           Coefficient of variation of detected burst duration across
                    class-c trials (loss of timing control).
  inter_attempt_gap PROXY: median within-trial onset latency (time from trial start
                    to contraction onset). True wall-clock inter-attempt gaps are
                    not recorded in these datasets; onset latency is a documented
                    signal-derived stand-in for the same fatigue/difficulty signal.
  false_neg_count   Count of class-c trials the classifier rejected as CMD_NONE
                    (max posterior < confidence threshold) — mirrors the firmware's
                    no-match outcome.

Single channel throughout, to mirror SIGNAL's single EMG site.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from typing import Iterable

import numpy as np
import pandas as pd

from .commands import (
    ACCURACY_FLOOR,
    CONSECUTIVE_SESSIONS_FOR_FAILURE,
    COMMANDS,
    COVARIATES,
)
from .datasets import Trial, load_grabmyo, load_db6
from .emg_clean import clean_emg, rms_envelope
from .features import td_features

try:
    from sklearn.discriminant_analysis import LinearDiscriminantAnalysis
    from sklearn.preprocessing import StandardScaler
    from sklearn.pipeline import Pipeline
except Exception:  # pragma: no cover - sklearn is a hard dep, but stay importable
    LinearDiscriminantAnalysis = StandardScaler = Pipeline = None


# --- per-trial signal measures ----------------------------------------------
def burst_metrics(signal: np.ndarray, fs: float, powerline: float = 50.0,
                  hold_frac: float = 0.50, active_frac: float = 0.20) -> tuple[float, float, float]:
    """Return (time_to_peak_s, hold_duration_s, mean_rms) for one trial.

    * time_to_peak  : latency of the RMS-envelope peak (proxy for InterAttemptGap;
                      see module docstring — true wall-clock gaps are unrecorded).
    * hold_duration : time the envelope stays >= hold_frac of its peak (the
                      strong-contraction hold). Non-degenerate even when the trial
                      window is fixed-length, so its CoV (DurVar) carries signal.
    * mean_rms      : mean envelope amplitude over the active (>= active_frac peak)
                      region — the MeanRMS covariate.
    """
    clean = clean_emg(signal, fs, powerline=powerline)
    env = rms_envelope(clean, fs)
    if env.size == 0 or np.all(env == 0):
        return float(len(signal) / fs), 0.0, 0.0
    peak = float(np.max(env))
    time_to_peak = float(np.argmax(env) / fs)
    hold = np.where(env >= hold_frac * peak)[0]
    hold_duration = (hold[-1] - hold[0]) / fs if hold.size else 0.0
    active = np.where(env >= active_frac * peak)[0]
    mean_rms = float(np.mean(env[active])) if active.size else float(np.mean(env))
    return time_to_peak, float(hold_duration), mean_rms


def _select_best_channel(utrials, baseline, win_ms, step_ms, powerline) -> int:
    """Pick the single channel with the best baseline separability (3-fold CV).

    Mirrors choosing the most controllable single electrode site for a user — the
    realistic single-channel SIGNAL deployment decision. Operates on the baseline
    session only so the choice can't peek at future-session decline.
    """
    from sklearn.model_selection import cross_val_score

    sample = next(t for t in utrials if t.signal.ndim == 2)
    n_ch = sample.signal.shape[1]
    best_ch, best_acc = 0, -1.0
    for ch in range(n_ch):
        X, y = [], []
        for t in utrials:
            if t.session != baseline:
                continue
            f = windowed_features(t.signal[:, ch], t.fs, win_ms, step_ms, powerline)
            X.append(f)
            y += [t.command] * len(f)
        if len({*y}) < 2:
            continue
        X = np.vstack(X)
        clf = Pipeline([("scale", StandardScaler()), ("lda", LinearDiscriminantAnalysis())])
        try:
            acc = cross_val_score(clf, X, np.array(y), cv=3).mean()
        except Exception:
            continue
        if acc > best_acc:
            best_ch, best_acc = ch, acc
    return best_ch


def windowed_features(signal: np.ndarray, fs: float, win_ms: float = 200.0,
                      step_ms: float = 100.0, powerline: float = 50.0) -> np.ndarray:
    """Sliding-window time-domain feature matrix [n_windows, 6] for one trial."""
    clean = clean_emg(signal, fs, powerline=powerline)
    win = max(1, int(round(win_ms * 1e-3 * fs)))
    step = max(1, int(round(step_ms * 1e-3 * fs)))
    if len(clean) < win:
        return td_features(clean)[None, :]
    feats = [td_features(clean[i : i + win]) for i in range(0, len(clean) - win + 1, step)]
    return np.vstack(feats)


# --- covariate table ---------------------------------------------------------
def compute_covariate_table(
    trials: Iterable[Trial],
    confidence_threshold: float = 0.5,
    win_ms: float = 200.0,
    step_ms: float = 100.0,
    powerline: float = 50.0,
    select_channel: bool = True,
    calibrate_rejection: bool = True,
    baseline_reject_pct: float = 5.0,
) -> pd.DataFrame:
    """Compute the 5 covariates per (user, command, session) from real Trials.

    Per user: (optionally) pick the best single EMG channel from the baseline
    session, train a single-channel LDA on baseline windows across all classes
    present, calibrate a confidence-rejection threshold from baseline, then score
    every session. Returns a long DataFrame: one row per (user_id, command,
    session_t).

    select_channel: if trials carry multi-channel signals, choose the most
        separable single site per user (realistic single-electrode placement).
    calibrate_rejection: set each user's CMD_NONE threshold to the
        baseline_reject_pct-th percentile of baseline trial confidences (so
        baseline false-negatives are rare and rise as the signal degrades),
        floored at chance.
    """
    if Pipeline is None:
        raise RuntimeError("scikit-learn is required for the covariate classifier.")

    # group trials by user
    by_user: dict[str, list[Trial]] = defaultdict(list)
    dataset_name = None
    for t in trials:
        by_user[t.user_id].append(t)
        dataset_name = t.dataset

    rows: list[dict] = []
    for user, utrials in by_user.items():
        sessions = sorted({t.session for t in utrials})
        baseline = sessions[0]

        # Single-channel selection (mirrors choosing one electrode site).
        if select_channel and any(t.signal.ndim == 2 for t in utrials):
            best_ch = _select_best_channel(utrials, baseline, win_ms, step_ms, powerline)
            for t in utrials:
                if t.signal.ndim == 2:
                    chosen = t.meta.get("channels")
                    t.meta["channel"] = chosen[best_ch] if isinstance(chosen, list) else best_ch
                    t.signal = t.signal[:, best_ch]
        else:
            for t in utrials:
                if t.signal.ndim == 2:
                    t.signal = t.signal[:, 0]

        # Precompute per-trial features + burst metrics once.
        for t in utrials:
            t.meta["_feats"] = windowed_features(t.signal, t.fs, win_ms, step_ms, powerline)
            lat, dur, mrms = burst_metrics(t.signal, t.fs, powerline)
            t.meta["_lat"], t.meta["_dur"], t.meta["_rms"] = lat, dur, mrms

        # Train classifier on baseline-session windows (all classes incl REST).
        Xtr, ytr = [], []
        for t in utrials:
            if t.session == baseline:
                f = t.meta["_feats"]
                Xtr.append(f)
                ytr += [t.command] * len(f)
        if not Xtr:
            continue
        Xtr = np.vstack(Xtr)
        clf = Pipeline([("scale", StandardScaler()), ("lda", LinearDiscriminantAnalysis())])
        if len(set(ytr)) < 2:
            continue  # need >=2 classes to train a discriminant
        clf.fit(Xtr, ytr)
        classes = list(clf.classes_)

        # Calibrate the rejection threshold from baseline trial confidences.
        thr = confidence_threshold
        if calibrate_rejection:
            base_conf = []
            for t in utrials:
                if t.session == baseline and t.command in COMMANDS:
                    proba = clf.predict_proba(t.meta["_feats"])
                    base_conf.append(float(np.mean(np.max(proba, axis=1))))
            if base_conf:
                thr = max(1.0 / len(classes), float(np.percentile(base_conf, baseline_reject_pct)))

        # Score every session, per command class.
        for s in sessions:
            per_cmd = defaultdict(lambda: {"attempts": 0, "correct": 0, "rejected": 0,
                                           "durs": [], "rms": [], "lats": []})
            for t in utrials:
                if t.session != s or t.command not in COMMANDS:
                    continue
                f = t.meta["_feats"]
                proba = clf.predict_proba(f)
                win_pred = [classes[i] for i in np.argmax(proba, axis=1)]
                trial_conf = float(np.mean(np.max(proba, axis=1)))
                pred = Counter(win_pred).most_common(1)[0][0]
                d = per_cmd[t.command]
                d["attempts"] += 1
                if trial_conf < thr:
                    d["rejected"] += 1            # CMD_NONE -> false negative
                elif pred == t.command:
                    d["correct"] += 1
                d["durs"].append(t.meta["_dur"])
                d["rms"].append(t.meta["_rms"])
                d["lats"].append(t.meta["_lat"])

            for cmd, d in per_cmd.items():
                if d["attempts"] == 0:
                    continue
                durs = np.array(d["durs"], dtype=float)
                # NaN (not 0.0) when burst duration is unmeasurable for the whole session:
                # 0.0 would read as "perfect timing control" exactly for the worst trials.
                # NaNs are neutrally imputed (column median) after the table is built.
                dur_cov = float(np.std(durs) / np.mean(durs)) if np.mean(durs) > 0 else float("nan")
                rows.append({
                    "user_id": user,
                    "command": cmd,
                    "session_t": int(s),
                    "accuracy": d["correct"] / d["attempts"],
                    "mean_rms": float(np.mean(d["rms"])) if d["rms"] else 0.0,
                    "dur_var": dur_cov,
                    "inter_attempt_gap": float(np.median(d["lats"])) if d["lats"] else 0.0,
                    "false_neg_count": int(d["rejected"]),
                    "n_attempts": int(d["attempts"]),
                    "dataset": dataset_name,
                })
    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.sort_values(["user_id", "command", "session_t"]).reset_index(drop=True)
        # Neutral imputation of unmeasurable covariate cells (NaN, e.g. dur_var on a
        # fully degraded session): fill with the column median so they bias the hazard
        # model in neither direction, rather than a misleadingly healthy 0.0.
        for c in COVARIATES:
            if c in df.columns and df[c].isna().any():
                df[c] = df[c].fillna(df[c].median())
    return df


# --- time-varying Cox frame (patent Section 13 schema) -----------------------
def to_cox_frame(
    cov_table: pd.DataFrame,
    floor: float = ACCURACY_FLOOR,
    consecutive: int = CONSECUTIVE_SESSIONS_FOR_FAILURE,
) -> pd.DataFrame:
    """Convert the covariate table into CoxTimeVaryingFitter input.

    One 'individual' = one (user, command) trajectory, id = "user|command".
    Sessions become contiguous intervals [k, k+1). event_failure = 1 on the
    interval where Accuracy first stays < floor for ``consecutive`` consecutive
    sessions (patent Section 7.3); rows after the event are dropped. Trajectories
    that never fail are right-censored (event=0 throughout) — the majority case.
    """
    out_rows: list[dict] = []
    for (user, cmd), g in cov_table.groupby(["user_id", "command"]):
        g = g.sort_values("session_t").reset_index(drop=True)
        below = (g["accuracy"] < floor).tolist()
        # first index i where below[i-consecutive+1 .. i] are all True
        fail_idx = None
        for i in range(len(below)):
            if i + 1 >= consecutive and all(below[i - consecutive + 1 : i + 1]):
                fail_idx = i
                break
        last = fail_idx if fail_idx is not None else len(g) - 1
        ident = f"{user}|{cmd}"
        for k in range(last + 1):
            r = g.iloc[k]
            out_rows.append({
                "id": ident,
                "user_id": user,
                "command": cmd,
                "session_t": int(r["session_t"]),
                "start": k,
                "stop": k + 1,
                "event_failure": int(fail_idx is not None and k == fail_idx),
                **{c: float(r[c]) for c in COVARIATES},
                "n_attempts": int(r["n_attempts"]),
            })
    return pd.DataFrame(out_rows)


# --- dataset entry points + CLI ----------------------------------------------
def _parse_int_list(spec: str) -> list[int]:
    """Parse '1-4' or '1,2,5' into a list of ints."""
    out: list[int] = []
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-")
            out += list(range(int(a), int(b) + 1))
        elif part:
            out.append(int(part))
    return out


def build_from_grabmyo(root: str, participants: list[int], sessions=(1, 2, 3),
                       channel: str = "F1", **kw) -> tuple[pd.DataFrame, pd.DataFrame]:
    trials = list(load_grabmyo(root, participants, sessions, channel=channel))
    cov = compute_covariate_table(trials, powerline=50.0, **kw)
    return cov, to_cox_frame(cov)


def build_from_db6(root: str, subjects: list[int], channel: int | str = "all",
                   **kw) -> tuple[pd.DataFrame, pd.DataFrame]:
    trials = list(load_db6(root, subjects, channel=channel))
    cov = compute_covariate_table(trials, powerline=50.0, **kw)
    return cov, to_cox_frame(cov)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Build ADAPT covariate + Cox tables from real sEMG.")
    ap.add_argument("--dataset", choices=["grabmyo", "db6"], required=True)
    ap.add_argument("--root", required=True, help="dataset root directory")
    ap.add_argument("--subjects", default="1-4", help="participant/subject list, e.g. 1-4 or 1,2,5")
    ap.add_argument("--channel", default=None, help="EMG channel (name for GRABMyo, index for DB6)")
    ap.add_argument("--out", default="adapt/derived/covariates.csv")
    ap.add_argument("--cox-out", default=None, help="path for the Cox time-varying frame CSV")
    args = ap.parse_args(argv)

    ids = _parse_int_list(args.subjects)
    if args.dataset == "grabmyo":
        ch = args.channel or "forearm"   # 'forearm' => best single site selected per user
        cov, cox = build_from_grabmyo(args.root, ids, channel=ch)
    else:
        ch = int(args.channel) if args.channel is not None else "all"  # best site per subject
        cov, cox = build_from_db6(args.root, ids, channel=ch)

    import os
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    cov.to_csv(args.out, index=False)
    cox_out = args.cox_out or args.out.replace(".csv", "_cox.csv")
    cox.to_csv(cox_out, index=False)
    n_events = int(cox["event_failure"].sum()) if not cox.empty else 0
    print(f"covariate rows: {len(cov)}  ->  {args.out}")
    print(f"cox rows: {len(cox)}  events(failures): {n_events}  ->  {cox_out}")
    if not cov.empty:
        print(cov.head(12).to_string(index=False))


if __name__ == "__main__":
    main()
