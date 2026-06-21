"""Phase 4 — optional host-side EMG classifier upgrade (behind a flag).

CLAUDE.md rule 6: GitHub/HF models are allowed ONLY for the burst/gesture classifier,
as an OPTIONAL upgrade to SIGNAL's classifier, never as a substitute for the Cox hazard
model — and any external model must be verified to actually run. The verified search is
documented in docs/phase4_classifier_search.md. Its honest conclusion: no turnkey DB6
classifier with shippable weights exists. WaveFormer (MIT) is the recommended external
model to plug in (train-from-scratch); see WAVEFORMER_NOTES.

What this module ships is a RUNNABLE, flag-gated upgrade benchmarked on REAL DB6:
  * mode='baseline' : single best EMG channel + LDA  (mirrors SIGNAL's single-site reality)
  * mode='upgrade'  : all 14 channels, Hudgins TD features + RandomForest (host-side only)

The firmware real-time FSM is NEVER replaced — this is host-side, behind a flag. Benchmark
numbers produced here come from actually running on downloaded DB6 data (standard rep
split: train reps 1-8, test reps 9-12), never copied from a paper/README.
"""
from __future__ import annotations

import argparse
from collections import Counter

import numpy as np
import pandas as pd

from sklearn.discriminant_analysis import LinearDiscriminantAnalysis
from sklearn.ensemble import RandomForestClassifier
from sklearn.model_selection import cross_val_score
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from .datasets import load_db6
from .emg_clean import clean_emg
from .features import td_features

WAVEFORMER_NOTES = """To plug in WaveFormer (https://github.com/ForeverBlue816/WaveFormer, MIT) instead of
the built-in upgrade classifier:
  1. git clone the repo; pip install -r requirements.txt (PyTorch, scipy, h5py).
  2. Arrange DB6 .mat as data/DB6/<subject>/*.mat and run its DB6 preprocessor
     (data/DB6/db6_data_processor): 16->14 ch (drop ch 8-9), fs=2000, 20-90 Hz bandpass,
     50 Hz notch, z-score, 1024/512 windows, rep split 1-8/9-10/11-12.
  3. Edit train.sh for DB6: NB_CLASSES=7, INPUT_VARIATES=14, TIME_STEPS=1024, PATCH_WIDTH
     dividing 1024 (e.g. 32/64), point *_DATA_PATH at the processed dir, set --nproc_per_node.
  4. bash train.sh (torchrun; --test evaluates). No weights ship, so one training run is
     required; its README's 81.93% inter-session is the authors' claim, not reproduced here.
"""


def _window_bounds(n: int, win: int, step: int) -> list[tuple[int, int]]:
    if n < win:
        return [(0, n)]
    return [(i, i + win) for i in range(0, n - win + 1, step)]


def trial_feature_matrix(signal: np.ndarray, fs: float, win_ms: float = 200.0,
                         step_ms: float = 100.0, powerline: float = 50.0) -> np.ndarray:
    """Window a trial into a [n_windows, n_feat] feature matrix.

    1-D signal -> 6 Hudgins features per window. 2-D (samples x channels) -> the 6
    features concatenated across channels (n_ch * 6).
    """
    win = max(1, int(round(win_ms * 1e-3 * fs)))
    step = max(1, int(round(step_ms * 1e-3 * fs)))
    if signal.ndim == 1:
        clean = clean_emg(signal, fs, powerline=powerline)
        return np.vstack([td_features(clean[a:b]) for a, b in _window_bounds(len(clean), win, step)])
    cleaned = np.column_stack([clean_emg(signal[:, c], fs, powerline=powerline)
                               for c in range(signal.shape[1])])
    out = []
    for a, b in _window_bounds(len(cleaned), win, step):
        out.append(np.concatenate([td_features(cleaned[a:b, c]) for c in range(cleaned.shape[1])]))
    return np.vstack(out)


def make_classifier(mode: str):
    """baseline -> StandardScaler+LDA; upgrade -> RandomForest (host-side only)."""
    if mode == "baseline":
        return Pipeline([("scale", StandardScaler()), ("lda", LinearDiscriminantAnalysis())])
    if mode == "upgrade":
        return RandomForestClassifier(n_estimators=200, random_state=0, n_jobs=-1)
    raise ValueError(f"unknown mode {mode!r}")


def _select_best_channel(train_trials, fs, win_ms, step_ms) -> int:
    """Pick the single channel with best 3-fold CV separability on the train set."""
    n_ch = train_trials[0][0].shape[1]
    best_ch, best_acc = 0, -1.0
    for ch in range(n_ch):
        X, y = [], []
        for sig, lab, _rep in train_trials:
            f = trial_feature_matrix(sig[:, ch], fs, win_ms, step_ms)
            X.append(f)
            y += [lab] * len(f)
        if len(set(y)) < 2:
            continue
        try:
            acc = cross_val_score(make_classifier("baseline"), np.vstack(X), np.array(y), cv=3).mean()
        except Exception:
            continue
        if acc > best_acc:
            best_ch, best_acc = ch, acc
    return best_ch


def _trial_level_accuracy(clf, test_trials, fs, win_ms, step_ms, channel) -> float | None:
    """Majority-vote prediction per trial; return fraction of trials classified correctly."""
    if not test_trials:
        return None
    correct = 0
    for sig, lab, _rep in test_trials:
        s = sig if channel is None else sig[:, channel]
        feats = trial_feature_matrix(s, fs, win_ms, step_ms)
        preds = clf.predict(feats)
        pred = Counter(preds).most_common(1)[0][0]
        correct += int(pred == lab)
    return correct / len(test_trials)


def benchmark_db6(root: str, subjects: list[int], win_ms: float = 200.0, step_ms: float = 100.0,
                  train_reps=range(1, 9), test_reps=range(9, 13)) -> pd.DataFrame:
    """Benchmark baseline vs upgrade on REAL DB6 (standard repetition split).

    For each subject: train on baseline-session reps 1-8; evaluate trial-level accuracy
    intra-session (baseline session, reps 9-12) and inter-session (later sessions, reps
    9-12). Returns a DataFrame of real accuracy numbers.
    """
    train_reps, test_reps = set(train_reps), set(test_reps)
    trials = list(load_db6(root, subjects, channel="all"))
    by_user: dict[str, list] = {}
    for t in trials:
        by_user.setdefault(t.user_id, []).append(t)

    rows = []
    for user, ut in by_user.items():
        fs = float(ut[0].fs)
        baseline = min(t.session for t in ut)
        train = [(t.signal, t.command, t.meta["repetition"]) for t in ut
                 if t.session == baseline and t.meta["repetition"] in train_reps]
        test_intra = [(t.signal, t.command, t.meta["repetition"]) for t in ut
                      if t.session == baseline and t.meta["repetition"] in test_reps]
        test_inter = [(t.signal, t.command, t.meta["repetition"]) for t in ut
                      if t.session > baseline and t.meta["repetition"] in test_reps]
        if len({lab for _s, lab, _r in train}) < 2 or not train:
            continue

        for mode in ("baseline", "upgrade"):
            channel = _select_best_channel(train, fs, win_ms, step_ms) if mode == "baseline" else None
            X, y = [], []
            for sig, lab, _rep in train:
                s = sig if channel is None else sig[:, channel]
                f = trial_feature_matrix(s, fs, win_ms, step_ms)
                X.append(f)
                y += [lab] * len(f)
            clf = make_classifier(mode)
            clf.fit(np.vstack(X), np.array(y))
            intra = _trial_level_accuracy(clf, test_intra, fs, win_ms, step_ms, channel)
            inter = _trial_level_accuracy(clf, test_inter, fs, win_ms, step_ms, channel)
            rows.append({
                "user_id": user,
                "mode": mode,
                "channels": "1 (best)" if mode == "baseline" else "14 (all)",
                "n_train_windows": int(sum(len(x) for x in X)),
                "intra_session_acc": None if intra is None else round(intra, 4),
                "inter_session_acc": None if inter is None else round(inter, 4),
                "n_test_intra": len(test_intra),
                "n_test_inter": len(test_inter),
            })
    return pd.DataFrame(rows)




def main(argv=None):
    ap = argparse.ArgumentParser(description="Benchmark baseline vs upgrade classifier on real DB6.")
    ap.add_argument("--root", required=True, help="extracted DB6 .mat root")
    ap.add_argument("--subjects", default="1")
    ap.add_argument("--out", default=None)
    args = ap.parse_args(argv)

    subs = []
    for part in args.subjects.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-"); subs += list(range(int(a), int(b) + 1))
        elif part:
            subs.append(int(part))

    df = benchmark_db6(args.root, subs)
    with pd.option_context("display.width", 160):
        print(df.to_string(index=False))
    if args.out:
        df.to_csv(args.out, index=False)
        print(f"\nwrote {args.out}")
    print("\nNOTE: numbers above are from running on REAL DB6 data, standard rep split.")


if __name__ == "__main__":
    main()
