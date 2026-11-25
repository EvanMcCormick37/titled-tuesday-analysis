# LLM model for research: `zai/glm-5.3-flash` via minirouter.sh

2026-11-20 · decided with the user

**Decision.** Research LLM calls use `zai/glm-5.3-flash` through minirouter.sh, for both the per-event loops and the final summarising call. Backup: `openai/gpt-6-luna`.

**Why.** The work needs reliable forced function calls, server-side search mixed with our own functions, a long context and a low price. Results from `scripts/research_smoke_test.py` ($0.12 in total):

| | GLM-5.3-Flash | Haiku 5.5 | GPT-6 Luna |
|---|---|---|---|
| Forced function call + round trip | ok | ok | ok |
| Planted clash (round during the window) classified as | `hard_round_conflict` ✓ | `rumoured` ✗ | `hard_round_conflict` ✓ |
| Google search mixed with our function | ok (once `max_tool_calls` removed) | not retested | not retested |
| News search | ok, $0.013 | ok, $0.013 | ok, but $0.028 (exceeded `max_uses`) |

Prices at the time (input / output, $ per million tokens): GLM-5.3-Flash 0.158 / 0.525; Haiku 5.5 and GPT-6 Luna 0.105 / 0.525. GLM is on a Vercel route, so every server tool works on it. Haiku's under-classification is safe in itself, but it would weaken any gate that acts only on hard categories.

The estimated cost of a full research run was about $0.15, and searches cost more than the tokens.

**What would change it.** Re-run the smoke test before switching. Pick from its results, not from this table.
