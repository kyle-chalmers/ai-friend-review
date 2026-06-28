# Multi-AI Review Research Notes

Use this reference when explaining why AI Friend Review asks multiple AI coding agents for independent review. Keep the claim modest: multiple reviewers improve coverage and reduce single-model blind spots, but their findings still need verification.

## Practical Takeaways

- Multiple samples help because LLM answers vary. Self-consistency improved chain-of-thought reasoning by sampling several reasoning paths and choosing the most consistent answer: https://arxiv.org/abs/2203.11171
- Independent debate helps because agents critique each other and expose unsupported reasoning. Multi-agent debate improved reasoning and factuality in the studied settings: https://arxiv.org/abs/2305.14325
- Different models catch different things. Ensemble research such as LLM-Blender shows that combining model outputs can outperform relying on one model, because model strengths vary by example: https://arxiv.org/abs/2306.02561
- LLM code review is useful but imperfect. Recent code-review studies report value for identifying issues, while also showing that generated findings need filtering and evidence checks: https://arxiv.org/html/2404.18496v2 and https://arxiv.org/html/2505.20206v1
- Hallucination remains a real risk. Code-focused hallucination research is a reminder to verify every claimed defect against the code, tests, or runtime behavior: https://arxiv.org/html/2404.00971v3

## How To Say It

AI Friend Review is useful because it creates independent review passes. One model may miss an edge case, overfit to its first explanation, or focus on style. A second or third model often attacks the diff from a different angle. The payoff is not that the majority is automatically right. The payoff is a better shortlist of things to verify before shipping.

## Guardrails

- Do not count votes as proof.
- Do not ship a reviewer finding without checking the file, line, and behavior.
- Prefer findings with concrete evidence over broad concerns.
- Keep disagreement visible. A single-model concern can be valuable, but label it as a lead.
- State verification gaps plainly when tests, services, or credentials are unavailable.
