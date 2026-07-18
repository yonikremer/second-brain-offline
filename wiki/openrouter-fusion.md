---
title: "OpenRouter Fusion"
type: concept
tags: [inference, routing, ensembles, agents]
sources:
  - "[[Surpassing Frontier Performance with Fusion]]"
---

# OpenRouter Fusion

**OpenRouter Fusion is a server-side model-synthesis service that dispatches a prompt to a panel of models, has a judge analyze their outputs, and returns a fused answer, exceeding solo frontier performance on the DRACO deep-research benchmark.**

Fusion is invoked like a single model (`model: openrouter/fusion`) or as a server tool that the caller decides to use. The panel runs in parallel with identical tools: web search, web fetch, and bash. A judge then produces structured analysis covering consensus, contradictions, partial coverage, unique insights, blind spots, and source quality. The final answer is written by the calling model grounded in that analysis.

On OpenRouter's evaluation using the DRACO benchmark, a frontier panel (`Fable 5 + GPT-5.5` synthesized by Opus 4.8) scored 69.0%, surpassing every individual model. A budget panel (`Gemini 3 Flash + Kimi K2.6 + DeepSeek V4 Pro`) scored 64.7%, beating solo Opus 4.8 (58.8%) and GPT-5.5 (60.0%) at roughly half the cost of Fable. Even self-fusion (`Opus 4.8 + Opus 4.8`) raised Opus's score 6.7 points, showing that synthesis itself captures value from multiple reasoning paths and source selections.

Caveats: DRACO is text-only and English-only, absolute scores depend on judge choice, and Fusion adds 2–3× latency when invoked. It is not positioned as a drop-in replacement for coding or long-horizon agent tasks.

## Related
[[surpassing-frontier-performance-with-fusion]] · [[source-registry]]
