---
title: "Chinese Frontier Models"
type: concept
tags: [models, china, moe, benchmarking]
sources:
  - "[[Chinese frontier models compared GLM-5, MiniMax M2.5 & M2.7, Kimi K2.5, Qwen 3.5, and MiMo-V2-Pro]]"
---

# Chinese Frontier Models

**Leading Chinese MoE models now compete with Western frontier systems on standardized benchmarks at a fraction of the API cost, with Claude Opus 4.6 still leading SWE-bench and terminal tasks.**

In early 2026, Zhipu AI's `GLM-5`, MiniMax's `MiniMax M2.5` and `MiniMax M2.7`, Moonshot AI's `Kimi K2.5`, Alibaba's `Qwen 3.5`, and Xiaomi's `MiMo-V2-Pro` reached or surpassed Opus 4.6 on selected Vals AI benchmarks while remaining 5–17× cheaper. All use sparse Mixture-of-Experts architectures: Kimi K2.5 activates 32B of 1T parameters; MiniMax M2.5 activates just 10B. That sparsity is the mechanical reason for the cost gap.

Task-specific leaders: Qwen 3.5 wins LiveCodeBench (85.33%) and offers a 991K context; Kimi K2.5 ties Opus on AIME (95.63%) and leads MMMU; GLM-5 has the highest Chinese Vals Index (60.69%) and is fully MIT-licensed; MiniMax M2.7 narrows the SWE-bench gap to ~5 points versus Opus while keeping the $0.16/test price. Persistent weaknesses include low IOI scores, lower Terminal-Bench performance, and (for MiMo) the absence of a Vals row, making direct comparison unsafe.

Production implications: for high-volume, well-defined tasks where a Chinese model is within a few points of frontier, the cost savings are meaningful; for real-world software engineering and agent reliability, Opus 4.6 remains the safer default.

## Related
[[chinese-frontier-models-compared-glm-5-minimax-m2-5-m2-7-kimi-k2-5-qwen-3-5-and-mimo-v2-pro]] · [[source-registry]]
