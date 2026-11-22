# Pitch — Atlas Workbench (Project Review, ~5 minutes)

*No slides. Spoken structure. Plain language.*

---

## 1. Context / objective

Atlas Workbench is an AI-assisted **operational-risk scenario analysis engine**: it systematically builds **compound** scenarios — combinations of emerging risks that could hit together — and scores them in a way we can audit, using public signals and planted synthetic data under explicit controls.

---

## 2. Problem

Our risk registers and many of our tools still treat risks largely **one at a time**. The losses that actually hurt are often **joint**: a control weakness, a third-party dependency, and an external shock landing in the same window.

Traditional scenario analysis handles that with expert workshops — high quality, low coverage, a few times a year. We get a small number of deep scenarios. We do **not** systematically search the space of plausible combinations or evaluate the ranking machinery against a known generative process.

This work makes scenario analysis more continuous, combinatorial, and traceable. It is not a capital model, and synthetic exercises do not establish real-world calibration.

---

## 3. Approach

Three pieces, deliberately unglamorous where it counts:

**Generation.** We do not brute-force every subset of the risk list — that does not scale and most combinations are nonsense. We expand along **causal channels** and known dependencies (shared identity stack, cloud region, payment chain, and so on), limited to pairs and triples unless a stronger case is made, and we keep an explicit exploration budget for surprises.

**Scoring.** Hybrid and auditable: frequency and severity estimated from planted or public signals; dependence via shared-channel structure; Monte Carlo for a **distribution**, not a single magic number; LLM or expert input only as **documented judgment** — never as a silent substitute for data. Every score stores its components so reviewers can reconstruct the result.

**Search discipline.** Candidates are promoted into a small archive only if they pass gates (coherence, novelty, evidence floor, uncertainty honesty). We checkpoint assumptions, allow branches when we were wrong, and measure calibration on held-out periods so we can tell if the method works or if we should stop.

`local_detail` demo rows stay local. External models only see `public` information or allow-listed `aggregate` structures. That split is enforced in code, not left as a guideline.

---

## 4. Value

- **Compound and tail-oriented scenarios** that a flat register structurally under-represents.  
- **Continuous** candidate generation between formal workshops, so the workshop starts from a sharper list.  
- **Auditable** numbers: components, uncertainty class, lineage — suitable for challenge, not a black box score.  
- **Honest scope:** research and scenario support first; capital use only if validation later supports it.

What it is not: a promise to predict next quarter’s losses, or a replacement for SME judgment.

---

## 5. The ask

**A bounded evaluation, not a platform commitment.**

1. **One full synthetic/public evaluation cycle** with telemetry enabled.
2. **An approved model path** for public-context work, with local inference for aggregate or local-detail processing.
3. **A review panel** to measure cost per promoted scenario, calibration diagnostics, and whether reviewers prefer promoted scenarios to random combinations.

We return with **measured** unit economics and a go/no-go — including the option that the multiverse search layer is not worth it and a simpler batch scorer is enough. That keeps the decision reversible for you.

I am not asking you to fund a multi-year platform on a slide. I am asking for a bounded test that can prove or kill the method.

---

## Anticipated objections (annex)

**Model risk / validation.**  
Any number that influences risk decisions should be treated as a model. We design for that: deterministic core, stored components, versioned configs, and research-only framing until validation says otherwise. Success is a report and review exercise, not automated risk acceptance.

**Data confidentiality.**  
Local-detail event rows do not go to public APIs. Frontier models receive public context and allow-listed abstract structures only, behind a tested egress control.

**“How is this different from what we already do?”**  
We already do scenario analysis well at small N. This increases **coverage of combinations**, attaches **auditable quantification**, and runs **between** workshop cycles. It feeds the existing process; it does not invent a parallel risk universe.

**Spurious correlation / AI storytelling.**  
That is the main scientific risk. We pre-register external features, use holdout periods and false-discovery control, separate judgment from observation, and require the method to beat simple baselines on historical pair co-occurrence — or we stop.

**Cost predictability.**  
Agent-style loops can run away. We hard-cap spend per run, instrument every call, and base any future work on measured cost per validated scenario from the evaluation — not on a guess.

---

## Closing line (if needed)

If the evaluation cannot beat independent risk-by-risk scoring on its registered tasks, and cannot produce scenarios reviewers prefer to random combinations, we should not scale it.
