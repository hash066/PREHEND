"""Basic correctness of the sEMG cleaning chain (adapt.emg_clean).

Uses deterministic synthetic *waveforms* (a sinusoid + hum) only to check the
FILTER behaviour — this is signal-processing unit testing, not modelling on
fabricated EMG. Real covariates always come from recorded datasets.
"""
import numpy as np

from adapt.emg_clean import clean_emg, bandpass, notch_filter, rms_envelope, tkeo


def _fs():
    return 2000.0


def test_clean_preserves_length():
    rng = np.random.default_rng(0)
    x = rng.standard_normal(4000)
    y = clean_emg(x, _fs())
    assert y.shape == x.shape
    assert np.all(np.isfinite(y))


def test_bandpass_clamps_high_edge_below_nyquist():
    # asking for 450 Hz at fs=600 (Nyquist 300) must not blow up
    x = np.random.default_rng(1).standard_normal(2000)
    y = bandpass(x, 600.0, lo=20.0, hi=450.0)
    assert np.all(np.isfinite(y)) and y.shape == x.shape


def test_notch_attenuates_powerline_hum():
    fs = _fs()
    t = np.arange(0, 2.0, 1 / fs)
    signal = np.sin(2 * np.pi * 120 * t)          # in-band EMG-ish tone
    hum = 3.0 * np.sin(2 * np.pi * 50 * t)        # strong 50 Hz hum
    x = signal + hum
    y = notch_filter(x, fs, f0=50.0)
    # 50 Hz power should drop substantially after the notch
    def power_at(sig, f):
        fft = np.fft.rfft(sig)
        freqs = np.fft.rfftfreq(len(sig), 1 / fs)
        k = np.argmin(np.abs(freqs - f))
        return np.abs(fft[k])
    assert power_at(y, 50.0) < 0.2 * power_at(x, 50.0)


def test_rms_envelope_nonnegative_and_same_length():
    x = np.random.default_rng(2).standard_normal(3000)
    env = rms_envelope(x, _fs())
    assert env.shape == x.shape
    assert np.all(env >= 0)


def test_tkeo_matches_definition():
    x = np.array([0.0, 1.0, 0.0, -1.0, 0.0])
    psi = tkeo(x)
    # psi[n] = x[n]^2 - x[n-1]*x[n+1], clipped at 0; endpoints 0
    assert psi[0] == 0 and psi[-1] == 0
    assert psi[1] == max(0.0, 1.0 ** 2 - 0.0 * 0.0)  # =1
    assert psi[2] == max(0.0, 0.0 ** 2 - 1.0 * (-1.0))  # =1
