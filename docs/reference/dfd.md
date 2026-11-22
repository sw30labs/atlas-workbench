# Data Flow Diagrams

## 0. Context level (DFD-0)

```mermaid
flowchart LR
    PUB[Public sources<br/>reporting · registers] --> ATLAS((Contingency<br/>Atlas))
    ART[User artifacts<br/>PDF · Office · text · transcripts] --> ATLAS
    ATLAS --> LIB[Scenario library<br/>+ compound register]
    ATLAS --> DASH[Operator dashboard<br/>situation room · costs · compounds]
    OMLX[OMLX local inference] <--> ATLAS
    ATLAS --> TEL[Telemetry log<br/>LLM + run events]
```

## 1. Pipeline decomposition (DFD-1)

```mermaid
flowchart TD
    subgraph Inputs
        EVIDENCE[public evidence]
        ARTIFACT[uploaded artifact]
    end

    subgraph Ingest["1 · Ingest Evidence (daily, LLM)"]
        REVIEW[review-first prefill<br/>markdown + rubric]
        EXTRACT[structured evidence extraction]
    end

    EDB[(event_evidence.db<br/>evidence_events — public)]
    RISKS[(emerging_risks.json<br/>risk catalog)]
    DB[(atlas.db)]

    EVIDENCE --> REVIEW --> EXTRACT --> EDB
    ARTIFACT --> REVIEW

    subgraph Monitor["2 · Risk Monitor (weekly, LLM)"]
        ASSESS[identify + assess signals]
    end
    EDB --> ASSESS --> RISKS

    subgraph Generate["3 · Generate Scenarios (monthly, LLM)"]
        GEN[scenario drafting]
        JUDGE[QA judge ≥ 0.75]
    end
    RISKS --> GEN
    EDB --> GEN
    GEN --> JUDGE --> DB

    subgraph Offline["4–6 · offline phases"]
        PRIOR[4 prioritize → urgency 0–100]
        PATT[5 loss patterns]
        MAINT[6 maintain library]
    end
    DB --> PRIOR --> DB
    DB --> PATT --> DB
    DB --> MAINT --> DB

    subgraph Join["7 · Compound Join (monthly, deterministic)"]
        SERIES[signal-activation series<br/>approved-only evidence_refs linkage]
        FIT[fit marginals + joints]
        SCORE[score channel-blocked pairs]
    end
    RISKS --> SERIES
    EDB --> SERIES
    SERIES --> FIT --> SCORE --> DB

    subgraph Grad["8 · Graduate (monthly, LLM)"]
        ELIG[deterministic eligibility<br/>holdout survival]
        DOC[LLM document draft]
        QAG[QA judge]
    end
    DB --> ELIG --> DOC --> QAG --> DB

    OMLX[OMLX local inference]
    Ingest -.-> OMLX
    Monitor -.-> OMLX
    Generate -.-> OMLX
    Grad -.-> OMLX
    OMLX --> TEL[(telemetry/*.jsonl)]
    Join --> TEL
```

## 2. Compound scoring data flow (DFD-2)

```mermaid
flowchart LR
    RISKS[(risk catalog)] --> TAG[channel tagger<br/>keyword rules]
    TAG --> GRAPH[channel graph<br/>shared-channel edges]
    EVID[(event_evidence.db)] --> ACT[quarterly activations<br/>exact evidence_refs]
    ACT --> FIT[DetScorer.fit<br/>train window only]
    FIT --> MARG[marginals q_i<br/>EB-shrunk to pool]
    FIT --> JOINT[pair joints q_ij<br/>shrunk empirical]
    MARG --> MC[Monte Carlo<br/>all-must-fire, annual]
    JOINT --> MC
    SEV[lognormal severity<br/>pooled shrinkage] --> MC
    MC --> RLZ[ScoreRealization<br/>p̂ + interval + quantiles + components]
    RLZ --> EVT[EVT GPD overlay<br/>n_u ≥ 20 + sensitivity]
    EVT --> COMP[(compound_scenarios<br/>top-20)]
    RLZ --> HASH[state hash<br/>structure only]
    HASH --> COMP
```

## 3. Review boundary (all writes)

```mermaid
flowchart TD
    U[upload / paste] --> C[convert to markdown]
    C --> X[bounded chunk-map extraction]
    X --> RUB[rubric completeness check]
    RUB --> DRAFT[DRAFT fields<br/>source text + lane-labeled sources]
    DRAFT --> HUMAN{human review<br/>edit / drop / confirm}
    HUMAN -->|RUN PHASE| PHASE1[existing Phase 1 flow]
    HUMAN -->|discard| X0[(nothing written)]
    PHASE1 --> EDB[(event_evidence.db)]
```

**Invariant:** no artifact ingestion path writes to a store without a
human-confirmed step. The conversion endpoint (`/api/ingest/convert`)
is read-only by construction.

## 3b. Review queue & deep research (DFD-3b)

```mermaid
flowchart TD
    PEND[(pending records<br/>events + risks)] --> REP[deterministic validation report<br/>traffic lights + per-ref resolution]
    PEND --> PRE[advisory pre-analysis<br/>judge model · cached by content hash]
    REP --> CARD[Review card]
    PRE --> CARD
    RED[red flags<br/>failing checks + concerns] --> DR[deep research loop<br/>plan → DDG search → triage →<br/>capped fetch → assess → coverage judge]
    CARD --> RED
    DR --> CAND[(candidate evidence<br/>pending · origin: deep_research)]
    DR --> MEMO[(review_research memo)]
    CAND --> CARD
    MEMO --> CARD
    CARD --> HUMAN{human decision<br/>two attestations · reason}
    HUMAN -->|approve/reject via review.py| EDB[(event_evidence.db)]
    HUMAN -->|approve/reject via review.py| RISKS[(emerging_risks.json)]
    HUMAN -->|adopt approved refs<br/>register-locked| RISKS
    HUMAN -->|decision logged next to suggestion| AGREE[(review_suggestions<br/>agreement audit)]
```

**Invariants:** the pre-analysis and the research loop write only to
their advisory stores and to `pending` evidence; the four decision
functions in `review.py` are the only path to `approved`/`rejected`;
the research loop can only fetch URLs its own search returned, and
every fetch hop must be a public HTTP(S) URL (ADR-013).

## 4. Telemetry flow

```mermaid
flowchart LR
    subgraph Callers["LLM call sites"]
        P1[phases 1–3, 6]
        P8[phase 8 draft + judge]
        ING[ingest extract + rubric]
        EXT[external-feature null pass]
    end
    CHOKE[OMLXClient<br/>telemetry choke point]
    SINK[(generated/telemetry/<br/>llm.jsonl)]
    DET[deterministic jobs<br/>score_det events]
    JOINLOG[(compound-join.jsonl)]
    COSTS[/api/costs/summary]
    TOK[/api/tokens/usage]
    PAGE[Costs page +<br/>situation-room chart]

    Callers --> CHOKE --> SINK
    DET --> JOINLOG
    SINK --> COSTS
    JOINLOG --> COSTS
    SINK --> TOK
    JOINLOG --> TOK
    COSTS --> PAGE
    TOK --> PAGE
```
