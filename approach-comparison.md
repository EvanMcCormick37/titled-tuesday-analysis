# Titled Tuesday Prediction: Approach Comparison

## Overview

Both notebooks predict win/top-N probabilities for the July 8, 2026 Titled Tuesday event using the same two datasets. They share the same structural decomposition — `P(result) = P(enters) × P(result | enters)` — but differ fundamentally in how they estimate the conditional skill component.

---

## `evans-analysis.ipynb` — Weighted Frequency Model

**How it works:** For each player, compute a recency-weighted fraction of past appearances where they finished in the top N. Decay is applied in calendar time (weeks since event). Multiply by a similarly decayed attendance rate.

```
top_N_when_play = Σ(skill_coeff × [rank ≤ N]) / Σ(skill_coeff)
P(top N)        = attendance × top_N_when_play
```

### Strengths
- Simple and transparent — each number is directly interpretable as a weighted historical rate.
- Fast to compute; no iterative fitting.
- No hyperparameters beyond the decay constants.

### Weaknesses
- **Ignores field composition.** A top-3 finish against 150 players counts the same as a top-3 against 500. This makes results from thinner fields (early-morning sessions, holiday events) worth the same as results from stacked fields.
- **No regularization for sparse data.** A player with one appearance who finished 2nd gets `top_1_when_play = 0`, `top_3_when_play = 1.0`. The model treats that single result as perfectly reliable.
- **Probabilities don't sum to 1.** The model independently estimates each player's chance; there's no constraint that ensures the numbers are jointly consistent. In principle all 1,600 active players could each show a 5% win probability.
- **Decay uses calendar time, not event count.** The `skill_coeff` is computed as `0.98^(weeks since event)`. Because Titled Tuesday runs twice a week, this means the effective per-event decay varies depending on how the events happen to land on the calendar — a minor but real inconsistency.

---

## `analysis.ipynb` — Truncated Plackett-Luce Model

**How it works:** Fits a latent strength `w_i > 0` for each player using the Hunter MM algorithm on the full rank orderings of ~417 historical events, with recency weighting per event. Only the top-10 finishing positions are treated as "informative choices" (truncated PL); players below rank 10 still appear in the denominators but contribute no numerator signal. Win probability is then estimated via 100,000 Monte Carlo simulations using the Gumbel race representation of Plackett-Luce.

### Strengths
- **Principled probabilistic model.** PL directly models the generative process: player `i` wins a tournament with probability proportional to their strength relative to the field that showed up. Probabilities are consistent by construction.
- **Field-composition-aware.** Because strengths are estimated jointly across all tournaments, finishing ahead of Magnus Carlsen is worth more than finishing ahead of an unrated player.
- **Truncated likelihood avoids mid-field noise.** Limiting to the top-10 positions means a bad round in a Swiss tournament doesn't drag a star's strength estimate down; the model correctly treats the tail order as uninformative.
- **Handles sparse data.** The `MIN_APPEARANCES = 5` filter removes players with too little data from the MLE. A Gamma prior `(A=0.1, B=0.1)` prevents degenerate zero estimates for players who appeared in the top-10 group but never cracked the top-10 themselves.
- **Monte Carlo gives full distributions.** The simulation produces `P(top 1)`, `P(top 3)`, `P(top 10)`, `P(top 25)`, `P(top 50)` jointly and correctly — useful for hedging predictions across different threshold bets.
- **Event-count decay is more natural.** Decaying by event index (not calendar time) treats each tournament uniformly regardless of when it fell on the calendar.

### Weaknesses
- **Considerably more complex.** The MM algorithm, truncated likelihood, and Monte Carlo layer introduce more failure modes and are harder to audit or explain to a non-technical audience.
- **Still assumes independence.** Swiss pairings create within-event correlation (strong players are paired against each other after round 4). PL ignores this; all players are assumed to draw their performance independently.
- **Participation probability is naively decayed.** The model uses a simple exponential decay for `P(enters)` without any player-specific context (tournaments, travel schedule, streaming conflicts).
- **Strength estimates are rank-only.** Score within the tournament is ignored. A player who wins every game 1.0–0 is treated the same as one who grinds out 8.5–1.5. This discards real signal.

---

## Head-to-Head on Key Design Choices

| | `evans-analysis` | `analysis` (PL) |
|---|---|---|
| Skill metric | Weighted top-N hit rate | Latent PL strength from rank orderings |
| Field-size sensitivity | None | Yes (joint estimation) |
| Sparse-data handling | None | Prior + appearance filter |
| Probability consistency | Not guaranteed | Guaranteed (Gumbel race) |
| Uncertainty quantification | Point estimate only | Full top-N distribution via MC |
| Interpretability | High | Moderate |
| Computational cost | Negligible | ~30–60s (400 MM iterations) |

---

## Verdict

**I favor `analysis.ipynb` (Plackett-Luce).**

The core problem — predicting who wins a large Swiss tournament — is fundamentally a rank-order problem, and Plackett-Luce is the canonical model for exactly that. The frequency approach in `evans-analysis` treats all top-N finishes as interchangeable regardless of field strength, and produces probabilities that aren't jointly consistent. In a 400-player event where finishing top-3 requires beating some of the best online blitz players in the world, ignoring field composition is a meaningful error.

The one area where `evans-analysis` has a genuine edge is interpretability: every number is a direct historical rate that any chess fan can understand. That's worth preserving for communication — but not at the cost of using a mis-specified model for the actual prediction.

The most impactful improvement to `analysis.ipynb` would be incorporating **score** (not just rank) as a secondary signal, since it carries real information about dominance within a tournament that pure rank discards.
