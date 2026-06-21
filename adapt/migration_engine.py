"""ADAPT — migration engine (patent disclosure Section 10).

Turns per-command survival estimates S(t) into concrete remap actions:

  * flag any command whose S(t) has dropped below the action threshold
    (Section 8.4, default 0.7),
  * substitute a flagged LOWER-priority command with a compound (double-tap)
    pattern of the healthiest currently-healthy HIGHER-priority command
    (Section 10.2, e.g. LONG -> SHORT_SHORT),
  * escalate to caregiver instead of compressing further if SHORT — the
    highest-priority, structurally simplest command — is itself flagged
    (Section 10.3). Silently degrading the only load-bearing command is unsafe.

Priority order is fixed by the spec: SHORT > DOUBLE > LONG (commands.PRIORITY).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .commands import PRIORITY, DEFAULT_LOGICAL, ESCALATE, SURVIVAL_THRESHOLD
from .serial_bridge import build_remap


@dataclass
class MigrationDecision:
    """One migration/escalation action for one flagged command."""

    flagged_command: str            # SHORT / LONG / DOUBLE
    action: str                     # "SUBSTITUTE" or "ESCALATE"
    substitute_pattern: Optional[str]   # e.g. "SHORT_SHORT" (None for escalation)
    target_logical: Optional[str]       # logical to retarget, e.g. "HOME"
    survival: float                 # S(t) that triggered the action
    reason: str

    def remap_line(self) -> Optional[str]:
        """The exact REMAP wire line to send, or None for an escalation."""
        if self.action != "SUBSTITUTE" or self.substitute_pattern is None:
            return None
        return build_remap(self.target_logical, self.substitute_pattern)


def select_substitute(
    flagged_command: str,
    healthy_commands: list[str],
    survival_scores: dict[str, float],
) -> str:
    """Return the substitute pattern for a flagged command, or ESCALATE.

    Implements patent Section 10.2/10.3. Candidates are strictly HIGHER priority
    (more protected) than the flagged command and currently healthy; the
    healthiest (highest S(t)) wins, and the flagged command is remapped to its
    double-tap (e.g. SHORT_SHORT). If no higher-priority healthy command exists
    (i.e. SHORT itself flagged) -> ESCALATE.
    """
    if flagged_command not in PRIORITY:
        raise ValueError(f"unknown command {flagged_command!r}")
    flagged_rank = PRIORITY.index(flagged_command)
    candidates = [c for c in PRIORITY[:flagged_rank] if c in healthy_commands]
    if not candidates:
        return ESCALATE  # Section 10.3
    best = max(candidates, key=lambda c: survival_scores.get(c, 0.0))
    return f"{best}_{best}"  # double-tap compound pattern


def decide_migrations(
    survival_scores: dict[str, float],
    threshold: float = SURVIVAL_THRESHOLD,
    logical_map: dict[str, str] | None = None,
) -> list[MigrationDecision]:
    """Produce migration/escalation decisions from current survival scores.

    Args:
        survival_scores: {command: S(t)} for each command class in play.
        threshold: action threshold; a command with S(t) < threshold is flagged.
        logical_map: command -> logical mapping (defaults to firmware default).

    Returns a list of MigrationDecision, one per flagged command, evaluated in
    priority order (lowest priority migrated first).
    """
    logical_map = logical_map or DEFAULT_LOGICAL
    flagged = {c for c, s in survival_scores.items() if s < threshold}
    healthy = [c for c in survival_scores if c not in flagged]

    decisions: list[MigrationDecision] = []
    # Evaluate lowest-priority-first so the most-readily-migrated commands go first.
    for command in sorted(flagged, key=lambda c: -PRIORITY.index(c)):
        s = survival_scores[command]
        substitute = select_substitute(command, healthy, survival_scores)
        if substitute == ESCALATE:
            decisions.append(
                MigrationDecision(
                    flagged_command=command,
                    action="ESCALATE",
                    substitute_pattern=None,
                    target_logical=logical_map.get(command),
                    survival=s,
                    reason=(
                        "Highest-priority command flagged: the single EMG channel is "
                        "approaching its limit. Escalating per Section 10.3 instead of "
                        "compressing the only load-bearing command."
                    ),
                )
            )
        else:
            decisions.append(
                MigrationDecision(
                    flagged_command=command,
                    action="SUBSTITUTE",
                    substitute_pattern=substitute,
                    target_logical=logical_map.get(command),
                    survival=s,
                    reason=(
                        f"S(t)={s:.2f} < {threshold:.2f}: migrating {command} to "
                        f"{substitute} (double-tap of a healthier higher-priority command) "
                        f"before functional failure."
                    ),
                )
            )
    return decisions
