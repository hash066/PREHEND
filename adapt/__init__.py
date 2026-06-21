"""ADAPT — Survival-Analytic Command-Channel Prognostics (host-side layer).

ADAPT sits above the single-channel EMG SIGNAL interface (firmware, frozen) and
forecasts, with a Cox proportional-hazards survival model, when an individual
burst-command class (SHORT / LONG / DOUBLE) will become unreliable for a user,
then proactively migrates it to a compound pattern of healthier commands before
functional failure.

This package is the non-real-time host layer only (patent disclosure Section 11).
No model fitting or inference ever runs in the Arduino 1 kHz loop.

Ground truth: ADAPT_command_channel_prognostics_patent_disclosure.pdf and
SIGNAL_ADAPT_hardware_map_and_firmware.pdf (repo root). If code conflicts with
those specs, the specs win.
"""

__version__ = "0.1.0"
