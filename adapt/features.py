"""ADAPT — time-domain sEMG features for the per-session reliability classifier.

The Accuracy covariate (patent Section 7.1) is the fraction of attempted command
instances classified correctly. To compute it honestly from recorded data we train
a per-user classifier on a baseline session and measure its accuracy on each later
session — so "accuracy" reflects real inter-session signal change (electrode shift,
non-stationarity), the documented proxy for decline (patent Section 5.1).

Feature set: the classic Hudgins time-domain set (MAV, WL, ZC, SSC) plus RMS and
IEMG — the standard, cheap, well-cited sEMG feature vector for myoelectric control.
These are computed on a single channel to mirror SIGNAL's single-EMG-site hardware.
"""
from __future__ import annotations

import numpy as np

_EPS = 1e-12


def mav(w: np.ndarray) -> float:
    """Mean absolute value."""
    return float(np.mean(np.abs(w)))


def rms(w: np.ndarray) -> float:
    """Root mean square (amplitude proxy)."""
    return float(np.sqrt(np.mean(w * w)))


def iemg(w: np.ndarray) -> float:
    """Integrated EMG (sum of absolute values)."""
    return float(np.sum(np.abs(w)))


def waveform_length(w: np.ndarray) -> float:
    """Cumulative waveform length: sum |x[n]-x[n-1]|."""
    return float(np.sum(np.abs(np.diff(w))))


def zero_crossings(w: np.ndarray, thresh: float = 0.0) -> float:
    """Zero crossings with an amplitude deadzone to reject baseline noise."""
    w = np.asarray(w, dtype=float)
    s = np.signbit(w)
    crossings = np.diff(s).astype(bool)
    amp_ok = np.abs(np.diff(w)) >= thresh
    return float(np.sum(crossings & amp_ok))


def slope_sign_changes(w: np.ndarray, thresh: float = 0.0) -> float:
    """Slope sign changes (count of local extrema above a deadzone)."""
    w = np.asarray(w, dtype=float)
    d = np.diff(w)
    ssc = (d[:-1] * d[1:]) < 0
    amp_ok = (np.abs(d[:-1]) >= thresh) | (np.abs(d[1:]) >= thresh)
    return float(np.sum(ssc & amp_ok))


def td_features(w: np.ndarray, zc_ssc_thresh: float | None = None) -> np.ndarray:
    """Return the 6-D feature vector [MAV, RMS, IEMG, WL, ZC, SSC] for a window.

    zc_ssc_thresh: deadzone for ZC/SSC; defaults to 1% of the window RMS so it
    scales with signal amplitude across datasets/sessions.
    """
    w = np.asarray(w, dtype=float)
    if zc_ssc_thresh is None:
        zc_ssc_thresh = 0.01 * (rms(w) + _EPS)
    return np.array(
        [
            mav(w),
            rms(w),
            iemg(w),
            waveform_length(w),
            zero_crossings(w, zc_ssc_thresh),
            slope_sign_changes(w, zc_ssc_thresh),
        ],
        dtype=float,
    )


FEATURE_NAMES = ("MAV", "RMS", "IEMG", "WL", "ZC", "SSC")
