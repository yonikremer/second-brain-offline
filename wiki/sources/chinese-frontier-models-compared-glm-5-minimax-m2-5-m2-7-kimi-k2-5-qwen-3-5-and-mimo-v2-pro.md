---
title: "Summary — Chinese frontier models compared: GLM-5, MiniMax M2.5 & M2.7, Kimi K2.5, Qwen 3.5, and MiMo-V2-Pro"
type: source-summary
tags: [clippings, models, china, benchmarking]
sources:
  - "[[Chinese frontier models compared GLM-5, MiniMax M2.5 & M2.7, Kimi K2.5, Qwen 3.5, and MiMo-V2-Pro]]"
published: 2026-02-28
---

# Summary — Chinese frontier models compared: GLM-5, MiniMax M2.5 & M2.7, Kimi K2.5, Qwen 3.5, and MiMo-V2-Pro

**Leading Chinese MoE models now compete with Western frontier systems on standardized benchmarks at 5–17× lower API cost, with Claude Opus 4.6 still leading SWE-bench, Terminal-Bench, and most enterprise tasks.**

The article benchmarks GLM-5, MiniMax M2.5, Kimi K2.5, Qwen 3.5, and (in a March update) MiniMax M2.7 and Xiaomi MiMo-V2-Pro against Claude Opus 4.6 using Vals AI's standardized harness. Every Chinese model uses MoE: Kimi K2.5 activates 32B of 1T parameters; MiniMax M2.5 activates only 10B. Trade-offs are sharp: Qwen 3.5 edges Opus on LiveCodeBench and offers a 991K context; Kimi K2.5 ties Opus on AIME and leads MMMU; MiniMax M2.5/M2.7 are cheapest at $0.16/test on Vals. The widest gaps remain in real-world software engineering (SWE-bench) and terminal tasks. Pricing at scale is dramatic: MiniMax M2.5 costs ~$9K/month versus $150K/month for Opus at 1M 1K-token calls/day. The piece closes with a task-based decision guide and cautions that MiMo-V2-Pro lacks a Vals row, so its numbers are not directly comparable.

## Key claims
- Standardized Vals evaluation narrows or reverses the Western-frontier advantage on math, coding, and multimodal tasks → [[chinese-frontier-models]]
- MoE activation sparsity is the driver of the 5–17× cost advantage over dense Western APIs → [[chinese-frontier-models]]
- Claude Opus 4.6 retains clear leads on SWE-bench, Terminal-Bench, and most enterprise benchmarks → [[chinese-frontier-models]]

## Derived concept notes
[[chinese-frontier-models]]
