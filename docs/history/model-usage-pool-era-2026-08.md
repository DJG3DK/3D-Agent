# Model usage — pool era (archived 2026-08-20)

Window: 2026-08-06 → 2026-08-20 12:45 UTC. Adaptive 4-tier complexity routing for the coordinator; investigator/summarizer pinned to glm-5.2. Superseded 2026-08-20 12:45 UTC by fixed per-role pins (agent-planner/coder/investigator/summarizer/test-writer).

| Role | Model | Calls | Tokens in | Tokens out | Est. cost |
|---|---|---|---|---|---|
| coordinator | deepseek/deepseek-v4-pro-0813 | 371 | 8,256,433 | 156,780 | $10.37 |
| coordinator | x-ai/grok-4.3 | 134 | 2,655,439 | 29,640 | $3.39 |
| investigator | glm-5.2 | 134 | 1,761,030 | 46,758 | $2.27 |
| coordinator | qwen/qwen3-coder-plus | 129 | 2,725,617 | 27,543 | $1.86 |
| summarizer | glm-5.2 | 74 | 856,307 | 157,460 | $1.61 |
| coordinator | amazon/nova-lite-v1 | 71 | 1,247,592 | 35,339 | $0.08 |
| coordinator | anthropic/claude-haiku-4.5 | 62 | 1,344,136 | 11,049 | $1.40 |
| coordinator | amazon/nova-micro-v1 | 59 | 959,455 | 38,089 | $0.04 |
| coordinator | openai/gpt-4o-mini | 50 | 1,080,722 | 15,639 | $0.17 |
| coordinator | z-ai/glm-5.2 | 38 | 578,235 | 10,588 | $0.59 |
| coordinator | qwen/qwen3.7-max | 37 | 660,346 | 12,768 | $1.03 |
| coordinator | openai/gpt-5.3-codex | 10 | 207,997 | 2,705 | $0.40 |
| coordinator | moonshotai/kimi-k3 | 7 | 136,225 | 976 | $0.42 |
| coordinator | google/gemini-3.1-pro-preview | 6 | 149,842 | 2,606 | $0.33 |

**Total estimated: $23.97** across 1182 calls.
