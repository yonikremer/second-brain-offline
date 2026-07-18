---
title: "Summary — Surpassing Frontier Performance with Fusion"
type: source-summary
tags: [clippings, inference, routing, ensembles, agents]
sources:
  - "[[Surpassing Frontier Performance with Fusion]]"
published: 2026-06-12
---

# Summary — Surpassing Frontier Performance with Fusion

**OpenRouter's Fusion synthesizes a panel of models through a judge to outperform individual frontier models on deep-research tasks, with diverse budget panels approaching frontier performance at roughly half the cost.**

Fusion dispatches a prompt to a chosen panel of models in parallel, each with web search, web fetch, and bash tools, then has a judge produce structured analysis (consensus, contradictions, gaps, unique insights) before the calling model writes the final answer. On OpenRouter's DRACO benchmark of 100 complex research tasks, `Fable 5 + GPT-5.5` fused by Opus 4.8 scored 69.0%, beating Fable 5 solo (65.3%) and every individual model. A budget panel (`Gemini 3 Flash + Kimi K2.6 + DeepSeek V4 Pro`) reached 64.7%, beating GPT-5.5 and Opus 4.8 solo while costing about 50% of Fable. Self-fusion (`Opus 4.8 + Opus 4.8`) also lifted Opus 6.7 points, showing synthesis itself adds value beyond diversity. OpenRouter excluded benchmark-rubric domains from search/fetch to prevent contamination. The post notes DRACO is text-only, English-only, and judge-dependent, so absolute scores shift with judges while rankings stay stable.

## Key claims
- Model panels consistently beat individual models on deep research, and budget panels can surpass solo frontier models → [[openrouter-fusion]]
- A significant part of Fusion's lift comes from the synthesis step itself, not only panel diversity → [[openrouter-fusion]]
- Contamination controls (excluded domains) are necessary when panel models have web access during eval → [[openrouter-fusion]]

## Derived concept notes
[[openrouter-fusion]]
