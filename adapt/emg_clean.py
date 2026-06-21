"""ADAPT — surface-EMG signal cleaning.

The build goal asks for "proper cleaning of brainwaves." A precise note: this
project is *myoelectric* (sEMG, muscle), not EEG. The implemented chain is the
standard, citable surface-EMG preprocessing pipeline (Konrad 2005; SENIAM
recommendations), which is the correct cleaning for this signal. No EEG-specific
steps are claimed.

Why this module exists: the single-channel SIGNAL hardware already does a
DC-block -> RMS -> TKEO chain on-device (firmware Section 6.5). The research
datasets used to validate ADAPT (Ninapro DB6, GRABMyo) store *raw* sEMG, so we
reproduce an equivalent, well-documented chain off-device to derive the same
covariates (patent Section 7) honestly from real recordings.

Chain:
  1. mean removal (DC offset)
  2. power-line notch (50 or 60 Hz + harmonics), IIR notch, zero-phase
  3. band-pass 20-450 Hz (sEMG energy band), high edge clamped below Nyquist
  4. (optional) full-wave rectification
  5. moving-RMS envelope  /  discrete TKEO (matches firmware onset detector)
"""
from __future__ import annotations

import numpy as np
from scipy.signal import butter, iirnotch, sosfiltfilt, filtfilt


def remove_dc(x: np.ndarray) -> np.ndarray:
    """Remove the DC offset (mean) from a 1-D signal."""
    x = np.asarray(x, dtype=float)
    return x - np.mean(x)


def notch_filter(
    x: np.ndarray,
    fs: float,
    f0: float = 50.0,
    q: float = 30.0,
    harmonics: tuple[int, ...] = (1, 2, 3),
) -> np.ndarray:
    """Zero-phase IIR notch at the power-line frequency and its harmonics.

    Args:
        x: 1-D signal.
        fs: sampling rate (Hz).
        f0: power-line fundamental (50 Hz in EU/India, 60 Hz in NA).
        q: notch quality factor.
        harmonics: which multiples of f0 to notch (skipped if >= Nyquist).
    """
    x = np.asarray(x, dtype=float)
    nyq = fs / 2.0
    y = x
    for h in harmonics:
        freq = f0 * h
        if freq >= nyq:
            continue
        b, a = iirnotch(freq / nyq, q)
        y = filtfilt(b, a, y)
    return y


def bandpass(
    x: np.ndarray,
    fs: float,
    lo: float = 20.0,
    hi: float = 450.0,
    order: int = 4,
) -> np.ndarray:
    """Zero-phase Butterworth band-pass over the sEMG energy band.

    The high edge is clamped to just under Nyquist so the same call is valid for
    DB6 (fs=2000) and GRABMyo (fs=2048) without manual tuning.
    """
    x = np.asarray(x, dtype=float)
    nyq = fs / 2.0
    hi = min(hi, 0.99 * nyq)
    lo = max(lo, 1e-3)
    if lo >= hi:
        raise ValueError(f"invalid band: lo={lo} hi={hi} (fs={fs})")
    sos = butter(order, [lo / nyq, hi / nyq], btype="band", output="sos")
    return sosfiltfilt(sos, x)


def clean_emg(
    x: np.ndarray,
    fs: float,
    powerline: float = 50.0,
    lo: float = 20.0,
    hi: float = 450.0,
) -> np.ndarray:
    """Full clean: DC removal -> power-line notch -> band-pass. Not rectified.

    Returns the cleaned (still bipolar) sEMG. Use ``rms_envelope`` or ``tkeo``
    on the result to derive amplitude / onset features.
    """
    y = remove_dc(x)
    y = notch_filter(y, fs, f0=powerline)
    y = bandpass(y, fs, lo=lo, hi=hi)
    return y


def rectify(x: np.ndarray) -> np.ndarray:
    """Full-wave rectification."""
    return np.abs(np.asarray(x, dtype=float))


def rms_envelope(x: np.ndarray, fs: float, win_ms: float = 150.0) -> np.ndarray:
    """Moving-RMS envelope with a centred rectangular window.

    win_ms ~150 ms is a standard sEMG envelope window. Output length == len(x).
    """
    x = np.asarray(x, dtype=float)
    win = max(1, int(round(win_ms * 1e-3 * fs)))
    # RMS = sqrt(moving-average of x^2); reflect-pad to keep length & edges sane.
    sq = x * x
    kernel = np.ones(win) / win
    pad = win // 2
    padded = np.pad(sq, pad, mode="reflect")
    ma = np.convolve(padded, kernel, mode="same")[pad : pad + len(x)]
    return np.sqrt(np.maximum(ma, 0.0))


def tkeo(x: np.ndarray) -> np.ndarray:
    """Discrete Teager-Kaiser energy operator: psi[n] = x[n]^2 - x[n-1]*x[n+1].

    Mirrors the firmware onset detector (Section 6.5). Endpoints set to 0.
    Negative values are clipped to 0 (as the firmware does).
    """
    x = np.asarray(x, dtype=float)
    psi = np.zeros_like(x)
    psi[1:-1] = x[1:-1] ** 2 - x[:-2] * x[2:]
    return np.maximum(psi, 0.0)
