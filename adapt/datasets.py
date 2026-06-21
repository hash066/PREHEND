"""ADAPT — real multi-session sEMG dataset loaders.

Honest-proxy datasets (CLAUDE.md rule 2): no public EMG-reliability-decline
dataset in a progressive-disease population exists, so ADAPT is validated against
the closest REAL recorded multi-session sEMG datasets and every artefact says so.

  * Ninapro DB6 — 10 intact subjects, 10 sessions over 5 days, built to study
    inter-session sEMG reliability decay from electrode shift (Palermo et al.,
    IEEE ICORR 2017). 14 channels @ 2 kHz. Stored as MATLAB .mat.
  * GRABMyo — 43 participants, 3 days (1/8/29), 16 gestures + rest, 7 trials.
    28 active channels (16 forearm + 12 wrist) @ 2048 Hz. Stored as WFDB .dat/.hea
    (Pradhan et al., Sci Data 2022).

Command framing (Phase 2 instruction): SIGNAL's vocabulary is SHORT / LONG /
DOUBLE. These benchmark datasets contain hand GESTURES/GRASPS, not burst
patterns, so we relabel a fixed subset of gesture classes as the three command
classes. The ADAPT claim tracks per-command-CLASS reliability and is agnostic to
which physical gesture realises each class — the relabeling is a documented
framing device, not a claim that gesture k *is* a SHORT burst. A single EMG
channel is used throughout to mirror SIGNAL's single-site hardware.
"""
from __future__ import annotations

import glob
import os
import re
from dataclasses import dataclass, field
from typing import Iterator, Optional

import numpy as np


@dataclass
class Trial:
    """One recorded attempt of one command class in one session for one user."""

    user_id: str
    session: int               # 1-based session/day index (time axis for ADAPT)
    command: str               # 'SHORT' | 'LONG' | 'DOUBLE' | 'REST'
    signal: np.ndarray         # 1-D single-channel raw sEMG
    fs: float
    dataset: str
    gesture_id: int            # original dataset label (traceability)
    meta: dict = field(default_factory=dict)


# --- GRABMyo -----------------------------------------------------------------
GRABMYO_FS = 2048.0
# Default command framing: three distinct gestures relabeled as the command set,
# plus rest. Indices are the gestures the download script fetches. Documented and
# overridable. (gesture 17 is the rest/no-motion class.)
GRABMYO_COMMAND_MAP = {1: "SHORT", 6: "LONG", 11: "DOUBLE", 17: "REST"}
GRABMYO_FOREARM = tuple(f"F{i}" for i in range(1, 17))  # 16 forearm bipolar channels
GRABMYO_DEFAULT_CHANNEL = "forearm"  # 'forearm' => return all 16, let the logger pick the best site


def _resolve_channels(channel, sig_name: list[str]):
    """Return (column_indices, is_multi). 'forearm' -> all F* channels; a name/int
    -> single; a list -> those channels (2-D)."""
    if channel == "forearm":
        cols = [sig_name.index(c) for c in GRABMYO_FOREARM if c in sig_name]
        return cols, True
    if isinstance(channel, (list, tuple)):
        cols = [c if isinstance(c, int) else sig_name.index(c) for c in channel]
        return cols, True
    col = channel if isinstance(channel, int) else sig_name.index(channel)
    return [col], False


def load_grabmyo(
    root: str,
    participants: list[int],
    sessions: list[int] = (1, 2, 3),
    command_map: dict[int, str] | None = None,
    channel: str | int | list = GRABMYO_DEFAULT_CHANNEL,
) -> Iterator[Trial]:
    """Yield Trials from a local GRABMyo WFDB tree.

    Args:
        root: path to .../grabmyo/1.1.0
        participants: participant numbers to include.
        sessions: session indices (1,2,3 -> days 1,8,29).
        command_map: {gesture_id: command}. Defaults to GRABMYO_COMMAND_MAP.
        channel: 'forearm' (all 16 F* channels, 2-D signal, logger picks the best
            single site), or a single signal name/index (1-D), or a list.
    """
    import wfdb

    command_map = command_map or GRABMYO_COMMAND_MAP
    for s in sessions:
        for p in participants:
            pdir = os.path.join(root, f"Session{s}", f"session{s}_participant{p}")
            if not os.path.isdir(pdir):
                continue
            for hea in sorted(glob.glob(os.path.join(pdir, "*.hea"))):
                base = hea[:-4]
                m = re.search(r"gesture(\d+)_trial(\d+)$", base)
                if not m:
                    continue
                gid = int(m.group(1))
                if gid not in command_map:
                    continue
                try:
                    rec = wfdb.rdrecord(base)
                except Exception as exc:  # truncated/partial download or corrupt record
                    import warnings
                    warnings.warn(f"skipping unreadable GRABMyo record {os.path.basename(base)}: {exc}")
                    continue
                cols, multi = _resolve_channels(channel, list(rec.sig_name))
                sig = np.asarray(rec.p_signal[:, cols], dtype=float)
                if not multi:
                    sig = sig[:, 0]
                yield Trial(
                    user_id=f"GRABMyo_p{p}",
                    session=s,
                    command=command_map[gid],
                    signal=sig,
                    fs=float(rec.fs),
                    dataset="GRABMyo v1.1.0",
                    gesture_id=gid,
                    meta={"trial": int(m.group(2)),
                          "channels": [rec.sig_name[c] for c in cols]},
                )


# --- Ninapro DB6 -------------------------------------------------------------
DB6_FS = 2000.0
# DB6 has 7 grasp classes, labelled with Ninapro movement IDs {1,3,4,6,9,10,11}
# (verified from restimulus), plus rest (0). Map three distinct grasps to the
# command set; documented framing, overridable. (Label 5 does NOT exist in DB6.)
DB6_COMMAND_MAP = {1: "SHORT", 3: "LONG", 6: "DOUBLE", 0: "REST"}
DB6_N_CHANNELS = 14  # 'emg' field has 16 cols (14 Trigno EMG + 2 aux); use first 14
DB6_DEFAULT_CHANNEL = "all"  # 'all' => return all 14 Trigno channels, logger picks best site


def _db6_session_index(filename: str, fallback: int) -> int:
    """Infer a 1-based session index from a DB6 .mat filename.

    DB6 preproc files encode subject/day/trial; we map distinct (day, am/pm) to a
    monotonically increasing session index. Falls back to discovery order.
    """
    name = os.path.basename(filename).lower()
    # common patterns: s1_d1_t1 (day, trial=am/pm).  day in 1..5, t in 1..2
    md = re.search(r"d(\d+)", name)
    mt = re.search(r"[_]t(\d+)", name)
    if md and mt:
        day, t = int(md.group(1)), int(mt.group(1))
        return (day - 1) * 2 + t  # 1..10
    return fallback


def load_db6(
    root: str,
    subjects: Optional[list[int]] = None,
    command_map: dict[int, str] | None = None,
    channel: int | str = DB6_DEFAULT_CHANNEL,
    rest_label: int = 0,
) -> Iterator[Trial]:
    """Yield single-channel Trials from extracted Ninapro DB6 .mat files.

    Each .mat is a continuous recording with per-sample movement labels
    (``restimulus`` preferred, else ``stimulus``) and repetition labels
    (``rerepetition`` preferred, else ``repetition``). One (movement, repetition)
    segment == one Trial. Expects .mat files already extracted under ``root``
    (see scripts/extract_db6.py). ``subjects`` filters by the s<N> in the path.
    """
    from scipy.io import loadmat

    command_map = command_map or DB6_COMMAND_MAP
    mats = sorted(glob.glob(os.path.join(root, "**", "*.mat"), recursive=True))
    discovery = 0
    for fp in mats:
        name = os.path.basename(fp).lower()
        ms = re.search(r"s(\d+)", name)
        subj = int(ms.group(1)) if ms else None
        if subjects is not None and subj not in subjects:
            continue
        discovery += 1
        md = loadmat(fp)
        if "emg" not in md:
            continue
        emg = np.asarray(md["emg"], dtype=float)
        label = md.get("restimulus", md.get("stimulus"))
        rep = md.get("rerepetition", md.get("repetition"))
        if label is None or rep is None:
            continue
        label = np.asarray(label).ravel().astype(int)
        rep = np.asarray(rep).ravel().astype(int)
        session = _db6_session_index(fp, discovery)
        if channel == "all":
            sig_full = emg[:, : min(DB6_N_CHANNELS, emg.shape[1])]
            multi = True
        else:
            sig_full = emg[:, int(channel)]
            multi = False
        # Each contiguous (movement>0, repetition r) run is one attempt.
        for mov in command_map:
            if mov == rest_label:
                continue
            for r in np.unique(rep[(label == mov) & (rep > 0)]):
                seg_mask = (label == mov) & (rep == r)
                if seg_mask.sum() < int(0.2 * DB6_FS):  # ignore <200 ms slivers
                    continue
                seg = sig_full[seg_mask]  # 1-D or 2-D depending on `multi`
                yield Trial(
                    user_id=f"DB6_s{subj}",
                    session=session,
                    command=command_map[mov],
                    signal=np.asarray(seg, dtype=float),
                    fs=DB6_FS,
                    dataset="Ninapro DB6",
                    gesture_id=int(mov),
                    meta={"repetition": int(r), "file": os.path.basename(fp),
                          "channels": ("all" if multi else int(channel))},
                )
