# Risks and Objections — The Uncomfortable Case Against Contingency Atlas

This document is intentionally hostile. If you cannot answer these, do not pitch.

---

## 1. Foundational statistical objections

### 1.1 You do not have enough independent compound events

Observed compound operational failures are rare. The effective \(n\) for “A and B and C in the same quarter” may be closer to single digits than to the thousands the MC UI implies. **Wide posteriors are correct; confident compound probabilities are not.**

**Mitigation:** Lead with ranking and workshop utility, not calibrated compound \(p\). Report `judgment_dominant` ruthlessly. Use pair-level evaluation, not fantasy 5-way joints.

**Residual risk:** Sponsors will still hear “probability” and forget the asterisks.

### 1.2 Base rates from observational signals are biased

Sources change coverage, terminology, and publication cadence. Public reporting is selective, synthetic data is simplified, and historical frequency is not a clean Poisson process.

**Mitigation:** Document known biases; sensitivity analysis; prefer ranks within comparable regimes.

**Residual risk:** Garbage in, precision theater out.

### 1.3 Dependence is mostly unidentified

Copulas and BNs look scientific. With sparse co-occurrence, many dependence structures fit equally well and disagree in the tail — exactly where you claim value.

**Mitigation:** Common-factor channels with tight residual correlation caps; stress multiple dependence assumptions; never single-point tail.

**Residual risk:** You will still pick one default and people will treat it as truth.

### 1.4 Multiple comparisons will invent geopolitics

Thousands of (external feature × scoring cell) tests will produce “significant” stories. LLMs will narrate them beautifully.

**Mitigation:** Pre-registration, FDR, temporal holdout, adversarial null, ban free-text causal claims as evidence.

**Residual risk:** Humans love the narrative more than the q-value. Culture beats methodology unless governance is painful.

### 1.5 Post-hoc fitting is the default mode of LLMs

Ask a frontier model why an outcome happened during a volatile week and you will get a TED talk. That is not evidence.

**Mitigation:** Architectural separation: frontier models never see local-detail rows; judgment tagged; dual critique.

**Residual risk:** Local models hallucinate too. Tags do not stop a slide deck.

---

## 2. Methodological product objections

### 2.1 Cartesian combinatorial framing is a seductive mistake

Most subsets are nonsense. Searching nonsense wastes money and creates false discovery surface. “We cover the combinatorial space” is marketing, not measurement.

**Mitigation:** CBGE; order caps; weekly pair batch as honest baseline; kill multiverse if it loses.

**Residual risk:** You already shipped Ψ-CHRONOS vibes; sponsors may want the demo magic back.

### 2.2 Recursive baseline promotion locks noise

Early winners redefine the search neighborhood. That is exploitation. Without forced exploration and multi-baseline archives, you get a tech-risk monoculture.

**Mitigation:** Archive K, ε-wildcards, bandit with exploration, diversity crowding.

**Residual risk:** Metrics still reward what is easy to score (tech, data-rich), not what kills you (legal, conduct, low-data).

### 2.3 Branch merge will either destroy hypotheses or explode storage

Strict hash merge: never merges. Soft merge: collapses distinct transmission mechanisms. There is no free lunch.

**Mitigation:** Dual equivalence; conflict records; transmission_id in hash; conservative auto-merge.

**Residual risk:** Engineers will “just merge” under deadline.

### 2.4 “Novel algorithm” is a model-risk liability

In a governed setting, novelty is a validation cost. A standard frequency–severity pipeline with a better UI may get further than a bespoke scoring formalism.

**Mitigation:** Boredom-first scoring (this redesign). Novelty only in search orchestration.

**Residual risk:** Your own taste for elegance fights boredom-first.

### 2.5 Existing methods may dominate

Structured expert elicitation (SHELF), well-facilitated scenario workshops, and a curated Bayesian network maintained by SMEs might beat an agent loop for decision quality per dollar — especially at n=3 engineers.

**Mitigation:** Evaluate against workshop baselines (Task D). Be willing to reposition Atlas as **workshop multiplier**, not replacement.

**Residual risk:** If that is the truth, the “AI-native engine” pitch shrinks.

---

## 3. Model risk and governance

### 3.1 This will be treated as a model whether you like it or not

If numbers influence decisions, model governance applies: inventory, validation, challengers, and ongoing monitoring.

**Mitigation:** Explicit “research / workshop input only” scope; no automated decisions; model inventory entry before operational use.

**Residual risk:** Scope creep from a managing director who wants “the number” for a committee tomorrow.

### 3.2 Auditability dies in the agent graph

LangGraph + multi-model + branches makes reconstruction hard. Validators will ask: “Reproduce this score.” If you cannot, you fail.

**Mitigation:** Deterministic core; seeds; component blobs; state hashes; no score without decomposition.

**Residual risk:** Judgment components remain non-reproducible across model versions.

### 3.3 Disagreement washing

Pooling can hide that two models wildly disagree.

**Mitigation:** First-class disagreement field; widen intervals; no silent averages on conflicts.

### 3.4 Version churn of frontier models

`claude-x` next month changes priors. Time series of elicited numbers become non-stationary.

**Mitigation:** Freeze model ids for an evaluation; re-elicit on version change; prefer local for stable priors.

---

## 4. Confidentiality and security

### 4.1 The boundary will be bypassed under pressure

Someone will paste a local-detail row into a frontier chat “just once.” Or a prompt will include planted prose through a bug.

**Mitigation:** Egress gateway, no direct internet from local-detail processing, red team, logging, culture.

**Residual risk:** Humans with browsers exist. Technical control is necessary, not sufficient.

### 4.2 Sparse aggregates can reveal individual rows

Sparse cells + timing + public news can re-identify.

**Mitigation:** Group-size thresholds, allow-listed aggregate fields, and explicit review of any new egress schema.

**Residual risk:** Residual re-id risk never zero.

### 4.3 Prompt/telemetry leakage

Logging prompts for debug is an exfil path.

**Mitigation:** Default no prompt storage for aggregate or local-detail calls; hash-only optional.

---

## 5. Organizational and incentive objections

### 5.1 “How is this different from what risk already does?”

Scenario analysis already exists. You are proposing software. The burden is proof of better coverage, audit trail, and cost — not AI mystique.

**Mitigation:** Pitch as continuous combinatorial **support** for scenario analysis; measure review time saved and scenario diversity.

### 5.2 Wrong customer

If op-risk capital team wants SMA simplicity, they may not fund scenario engines. If operational resilience / scenario teams want narratives, they may not want MC.

**Mitigation:** Pick one buyer (**(REC)** scenario analysis / op-risk framework team). Do not sell to everyone.

### 5.3 Three engineers cannot maintain a multiverse OS

Agent graphs, eval harnesses, gateways, UIs, and model A/B are a platform. Platform entropy will eat the pod.

**Mitigation:** Phase 1 kill complexity early; batch scorer fallback; ruthless scope.

### 5.4 Demo-driven development

Ψ-CHRONOS-style outputs impress in a meeting and fail in validation. The organization rewards the meeting.

**Mitigation:** Pre-commit to abandon criteria publicly with your sponsor.

---

## 6. Engineering objections

### 6.1 Cost unpredictability of agent loops

Loops can spiral: retries, huge contexts, branch explosion.

**Mitigation:** Hard caps per cycle/branch/run; telemetry; pause switches.

### 6.2 Context fidelity loss

Summaries drop the assumption that mattered.

**Mitigation:** Structured assumptions as source of truth; summaries display-only.

### 6.3 Local model quality gap

Cheap high-volume scoring may be wrong in correlated ways (systematic bias).

**Mitigation:** A/B harness; calibration by model role; do not let local model set severity alone without data.

---

## 7. Ethical and misuse

### 7.1 False assurance

A dashboard of scenarios can create the feeling of control over the tail without improving resilience spend.

**Mitigation:** Pair every high scenario with control/resilience options and owners; track whether actions follow.

### 7.2 Blame automation

Scenarios linked to BUs can become political weapons.

**Mitigation:** Careful access control; research framing; no auto-publish to wide audiences.

---

## 8. What is actually strong (so this is not pure nihilism)

These parts of the idea **are** defensible:

1. **Compound scenarios are the right *object*** for scenario analysis gaps in list-based registers.
2. **Hybrid scoring with explicit uncertainty classes** is how you stay honest.
3. **Confidentiality split with an enforcement point** is mandatory and designable.
4. **Telemetry-first** is the right funding strategy (access → measured cost → ask).
5. **Evaluation harness with abandon criteria** is mature leadership, not pessimism.
6. **Graph-guided search** is a reasonable engineering response to combinatorics — *if* it beats batch pairs.

---

## 9. The one-sentence kill case

> If Atlas cannot beat independent marginals at ranking historical pair co-occurrence on holdout data, and cannot produce scenarios experts prefer to random combinations, then it is an expensive story generator — and you should stop.

That sentence should appear (in plain language) in the evaluation charter.
