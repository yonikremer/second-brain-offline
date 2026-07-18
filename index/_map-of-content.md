---
title: Map of Content — LLM Finetuning
type: index
tags: [moc, index]
updated: 2026-06-11
---

# 🗺️ Map of Content — LLM Finetuning & Serving

Entry point for the vault. Built from the 8-lesson **Finetuning Sessions** series (Miguel Otero Pedrido, The Neural Maze). Raw clippings live in `raw/`; synthesized notes in `wiki/`. A queryable knowledge graph lives in `graphify-out/` (`graph.html`, `graph.json`, `GRAPH_REPORT.md`).

## The spine

> Pretraining → SFT → Alignment (RLHF) — everything else hangs off this.

- [[the-llm-training-pipeline]] — **start here**
- [[the-transformer-architectures]] — encoder-only / encoder–decoder / decoder-only, attention, scaling laws
- [[pretraining-and-base-models]] — causal LM, self-supervision, CPT

## Stage 2 — Behavior (SFT)

- [[supervised-finetuning]] — behavior not knowledge; reasoning SFT; agentic SFT; evaluation
- [[loss-masking-and-chat-templates]] — `-100` masking, templates, packing, shift-right
- [[lima-hypothesis-data-quality]] — quality beats quantity

## Efficiency — train big models on small GPUs

- [[lora]] — low-rank adapters; rank/alpha/target modules
- [[qlora-and-quantization]] — NF4, double quantization, paged optimizers, quantization formats, GPU hardware arc

## Stage 3 — Alignment (RL)

- [[rlhf-ppo-vs-dpo]] — reward models, KL penalty, PPO vs DPO decision framework, IPO/KTO/RLAIF
- [[grpo-and-variants]] — critic-free RL (DeepSeek), length/difficulty biases, DAPO/GSPO/Dr. GRPO

## Beyond text & into production

- [[multimodal-finetuning-vision-tts]] — VLMs (Qwen3-VL), audio codecs (SNAC), Orpheus-TTS
- [[llm-inference-at-scale]] — prefill/decode, batching evolution, vLLM PagedAttention, kvcached
- [[kv-cache]] — the cross-cutting memory constraint linking training, quantization, and serving

## Frontier model releases

- [[mythos-class-models]] — Anthropic's Mythos tier (above Opus); Fable 5 vs Mythos 5 (same model, safeguards differ)
- [[mythos-class-capabilities]] — claimed SOTA across SWE, knowledge work, vision, memory, life sciences
- [[fable-5-safety-classifiers]] — classifier safeguards that fall back to Opus 4.8
- [[mythos-trusted-access]] — Project Glasswing, trusted-access programs, 30-day data retention

## Analyses

*Write-back notes from cross-source synthesis queries. Added here when created.*

## Other indices

- [[data-sources]] — documented data source bundles and bundle convention
- [[source-registry]] — provenance of all raw clippings (includes per-source summaries in `wiki/sources/`)
- [[key-takeaways]] — distilled insights across the corpus
- [[log]] — append-only ingest journal
