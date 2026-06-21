# ADAPT — Results on Real Data (honest record)

All numbers below were produced by running this repo's code on the **real** downloaded
datasets (see `docs/real_data_manifest.md`). Nothing here is synthetic or hand-tuned, and
nothing is claimed as clinically validated. The datasets are a **proxy** for disease-driven
decline (healthy-subject inter-session sEMG drift), not disease data.

Reproduce: `python -m adapt.run_analysis --db6-root data/ninapro_db6/extracted --db6-subjects 1-3 --grabmyo-root data/grabmyo/1.1.0 --grabmyo-subjects 1-4`

## 1. Covariate pipeline (Phase 2)

Real per-command/per-session covariates, single best forearm channel per user.

- **GRABMyo** (participants 1–4, 3 sessions = days 1/8/29): 36 covariate rows, **4 failure events**.
- **Ninapro DB6** (subjects 1–3, 10 sessions over 5 days each): 88 covariate rows, **5 failure events**.

Real inter-session decay is visible, e.g. GRABMyo_p1 LONG accuracy `1.00 → 0.43 → 0.00`
(days 1→8→29) — two consecutive sessions below the 0.50 floor → a real `AccuracyFailure`
event (patent §7.3).

## 2. Cox proportional-hazards fit (Phase 3)

Fitted **per dataset** (DB6's 10-session and GRABMyo's 3-session axes are not pooled into
one baseline hazard — that would mix incompatible time grids).

> **Low-confidence caveat (stated everywhere it matters):** events are scarce on this proxy
> (events-per-variable ≈ 0.8–1.0, vs. the conventional ≥10). The Cox coefficients below are
> therefore **essentially unidentified** — directions are sensible but magnitudes/p-values are
> illustrative, NOT validated. lifelines' separation warnings are surfaced, not hidden. This is
> exactly the cold-start regime the patent's §16 anticipates. The pipeline is what's
> demonstrated as correct; the coefficients are not a clinical finding.

In both fits `accuracy` is the strongest (negative) standardized coefficient — higher accuracy
→ lower failure hazard, the expected direction:

- **GRABMyo** (12 trajectories, 4 events, EPV 0.80): `accuracy` coef −0.53 (exp −0.59), p≈0.16.
- **DB6** (9 trajectories, 5 events, EPV 1.00): `accuracy` coef −0.57 (exp −0.57), **p≈0.064** —
  directionally strong even at this tiny N, though still flagged low-confidence.

## 3. Forecast + migration policy (Phase 3, §10)

Current-risk survival S(t) per command, with the migration policy applied:

| User | S(SHORT) | S(DOUBLE) | S(LONG) | Action |
|---|---|---|---|---|
| GRABMyo_p1 | 0.77 | 0.92 | 0.48 | MIGRATE LONG → `DOUBLE_DOUBLE` (`REMAP,HOME,DOUBLE_DOUBLE`) |
| GRABMyo_p2 | 0.92 | 0.77 | 0.25 | MIGRATE LONG → `SHORT_SHORT` |
| GRABMyo_p3 | 0.75 | 0.78 | 0.43 | MIGRATE LONG → `DOUBLE_DOUBLE` |
| GRABMyo_p4 | 0.47 | 0.35 | 0.89 | ESCALATE DOUBLE & SHORT (no healthier higher-priority command; §10.3) |
| DB6_s1 | 0.84 | 0.62 | 0.47 | MIGRATE LONG → `SHORT_SHORT`; MIGRATE DOUBLE → `SHORT_SHORT` (`REMAP,BACK,SHORT_SHORT`) |
| DB6_s2 | 0.15 | 0.79 | 0.25 | MIGRATE LONG → `DOUBLE_DOUBLE`; ESCALATE SHORT (§10.3) |
| DB6_s3 | 0.17 | 0.75 | 0.37 | MIGRATE LONG → `DOUBLE_DOUBLE`; ESCALATE SHORT (§10.3) |

This demonstrates both behaviours on real data: proactive substitution of a failing
lower-priority command to a double-tap of a healthier higher-priority command, and the safety
escalation (DB6_s2/s3 SHORT, S≈0.15–0.17) when no healthy higher-priority command exists.

## 4. Classifier upgrade benchmark (Phase 4)

Real DB6 benchmark (subjects 1–3, standard repetition split: train reps 1–8, test reps 9–12),
`python -m adapt.classifier_upgrade --root data/ninapro_db6/extracted --subjects 1-3`:

| subject | mode | channels | intra-session acc | inter-session acc |
|---|---|---|---|---|
| DB6_s1 | baseline | 1 best + LDA | 0.833 | 0.731 |
| DB6_s1 | upgrade | 14 all + RF | 1.000 | 0.722 |
| DB6_s2 | baseline | 1 best + LDA | 0.917 | 0.340 |
| DB6_s2 | upgrade | 14 all + RF | 1.000 | **0.450** |
| DB6_s3 | baseline | 1 best + LDA | 0.917 | 0.407 |
| DB6_s3 | upgrade | 14 all + RF | 1.000 | **0.463** |

The upgrade reaches perfect intra-session accuracy and improves inter-session for s2/s3; the
large intra→inter drops (e.g. s2: 1.00 → 0.45) are exactly the real cross-session reliability
decay ADAPT models. No external pretrained weights were used (none exist for DB6 — see
`docs/phase4_classifier_search.md`); these numbers come from running the shipped code on real
DB6 data, not from any paper/README.

## 5. What is NOT claimed

- No clinical validation; healthy-subject proxy only.
- Cox coefficients are not a validated effect size (too few events).
- `inter_attempt_gap` is a documented signal-derived proxy on these fixed-protocol datasets.
- WaveFormer's README accuracy (81.93%) is the authors' claim and is **not** reproduced here.
