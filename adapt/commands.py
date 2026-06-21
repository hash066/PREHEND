"""ADAPT — shared command vocabulary, priorities, and thresholds.

Single source of truth for constants that MUST stay consistent across the
covariate pipeline, hazard model, migration engine, serial bridge, and the
firmware wire protocol. Every value cites the spec section it implements so a
reviewer can trace it (CLAUDE.md style rule: no orphan magic numbers).
"""
from __future__ import annotations

# --- Burst-command vocabulary ------------------------------------------------
# SIGNAL classifies one EMG site into three burst patterns (firmware Section 6.3).
COMMANDS: tuple[str, ...] = ("SHORT", "LONG", "DOUBLE")

# Default burst-pattern -> logical-command mapping (firmware activeMap default,
# hardware/firmware ref Section 6.4 / Section 14).
DEFAULT_LOGICAL: dict[str, str] = {"SHORT": "SELECT", "LONG": "HOME", "DOUBLE": "BACK"}
LOGICALS: tuple[str, ...] = ("SELECT", "HOME", "BACK")

# --- Migration policy --------------------------------------------------------
# Substitution priority, highest-PROTECTED first (patent Section 10.1):
# SHORT (Select) > DOUBLE (Back) > LONG (Home).  A lower-priority command is
# migrated before a higher-priority one; SHORT is protected longest.
PRIORITY: tuple[str, ...] = ("SHORT", "DOUBLE", "LONG")

# If SHORT itself is flagged, do NOT compress further — escalate (Section 10.3).
ESCALATE: str = "ESCALATE_TO_CAREGIVER"

# --- Event of interest / censoring (patent Section 7.3) ----------------------
# AccuracyFailure(c): first session where Accuracy(c, t) < floor, sustained across
# CONSECUTIVE_SESSIONS_FOR_FAILURE consecutive sessions (avoids single-noisy-session
# triggers). Otherwise the command is right-censored at the last observed session.
ACCURACY_FLOOR: float = 0.50
CONSECUTIVE_SESSIONS_FOR_FAILURE: int = 2

# --- Forecast action threshold (patent Section 8.4) --------------------------
# Migrate when the survival estimate S(t) first drops below this confidence
# threshold (default 0.7 => 30% cumulative failure probability).
SURVIVAL_THRESHOLD: float = 0.70

# --- The five per-command, per-session covariates (patent Section 7.1) -------
# Kept SEPARATE and entered into the hazard model individually (Section 7.2) — no
# ad-hoc composite "capability score". Order matters: it is the model column order.
COVARIATES: tuple[str, ...] = (
    "accuracy",            # x1  Accuracy(c, t)         — direct functional measure
    "mean_rms",            # x2  MeanRMS(c, t)          — raw signal strength / motor-unit dropout
    "dur_var",             # x3  DurVar(c, t)           — CoV of burst duration (timing control)
    "inter_attempt_gap",   # x4  InterAttemptGap(c, t)  — median gap between attempts (fatigue proxy)
    "false_neg_count",     # x5  FalseNeg(c, t)         — count of CMD_NONE (no-match) per session
)
