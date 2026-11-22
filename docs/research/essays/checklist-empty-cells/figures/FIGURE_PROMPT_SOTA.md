# SOTA image-generator prompt — article hero figure

**Decision:** Hero figure = **checklist (memory) vs axis matrix (search)** with one highlighted joint cell.  
Not a full Contingency Atlas architecture diagram. Not a literal WWII plane (optional inset only).

Use this with Midjourney v6+, Flux, Ideogram, GPT-Image, Imagen, SD3, etc. Prefer models good at **text in image**. If text is garbled, use the SVG already generated (`figure_checklist_vs_search.svg`) as the publication asset.

---

## Primary prompt (copy-paste)

```text
Editorial infographic for a LinkedIn long-form essay on banking operational risk, 16:9 landscape, dark sophisticated fintech aesthetic (charcoal #0f1419 background, slate panels, warm amber #e8a54b accents, soft sage green for "reviewed" states). Clean Swiss-poster layout, generous margins, no stock-photo clutter, no 3D glassmorphism, no people.

Two equal side-by-side panels with rounded corners and subtle shadow:

LEFT PANEL titled "MEMORY — The checklist" in small amber caps over a serif headline. Vertical list of five operational-risk rows with green checkmarks and the word REVIEWED: Internal/external fraud; Business disruption & systems; Third-party / execution; Model risk (drift/failure); Clients, products & conduct. Below them one dashed empty ghost row labeled "No row for a joint risk" in muted italic gray. Caption under list: "Feels complete when the filled rows are green."

RIGHT PANEL titled "SEARCH — The axis matrix" in small amber caps over a serif headline. A clean 4×4 grid. X-axis label "Transmission channel →" with columns: Cloud/ID stack, Payment chain, Model API, Funding/SPV. Y-axis label "Risk theme →" with rows: Fraud, Third-party, Model risk, Geopolitics. Most cells empty with dashed borders. A few cells softly filled "named" or "drift/fail". ONE cell strongly highlighted with amber border and warm fill at the intersection of Model risk × Funding/SPV, labeled "EMPTY → file this". Below the grid a callout box: "Joint that no singleton list can hold — API distillation × AI buildout financing (private credit / SPV)".

Center top title in elegant serif: "Checklists remember. Axes search." Subtitle: "Operational risk: filled rows vs empty cells — where the next loss often sits."

Bottom full-width takeaway bar: "Takeaway: review the empty cells — and the joints — not only the green ticks." Smaller line: "Survivorship bias in reverse: the records that came home are not the full map of what can kill you."

Crisp vector-like edges, perfect alignment, readable 12–14pt UI typography, publication quality for executive banking audience, no watermarks, no logos, no cartoon mascots.
```

---

## Negative / avoid (if the model supports it)

```text
photorealistic office, people, handshakes, 3D renders, neon cyberpunk, comic style, cluttered icons, tiny unreadable text, misspelled labels, random charts, pie charts, stock photos, airplanes, guns, blood, corporate clipart mascots, rainbow gradients, glassmorphism overload
```

---

## Parameters (suggested)

| Setting | Value |
|---------|--------|
| Aspect | **16:9** (LinkedIn / slides) or **3:2** |
| Style | flat infographic / editorial vector |
| Guidance | medium-high (structure over chaos) |
| Seed | fix after you like a layout; iterate text fixes only |
| Variants | 4; pick sharpest labels |

---

## Model-specific tips

- **Ideogram / GPT-Image / Flux text modes:** best chance at readable labels; use primary prompt as-is.
- **Midjourney:** append ` --ar 16:9 --style raw --v 6` (or current). If text breaks, generate layout only then composite text in Figma/Keynote.
- **Local VLLM (MLX image models):** lower text fidelity — prefer the **SVG** for publish, use generator for moodboards only.

---

## Optional second image (not hero)

Only if you want a companion:

```text
Minimal dark editorial diagram, single abstract aircraft silhouette in charcoal line art, asymmetric small marks (bullet-hit dots) denser on fuselage, engines almost clean with a dashed outline label "missing sample", caption "The holes you don't see are often on the records that never returned — Abraham Wald / SRG, WWII." No gore, no realism of violence, schematic only, amber accent labels, 16:9.
```

Keep this secondary; the matrix is the essay’s structural takeaway.

---

## Caption for the article (paste under figure)

**Figure. Checklists remember; axes search.**  
Left: operational-risk memory as a list of named rows that can all be “reviewed” while a joint still has nowhere to live. Right: risk themes crossed with transmission channels generate empty cells by construction; the amber cell is a joint (e.g. model-capability extraction via API distillation × AI buildout financing through private credit/SPVs) that no singleton checklist can hold. Enumeration forces the look; filing makes it real. The sampling logic is the same as wartime survivorship bias: the records that return are not a full map of what can kill you.

---

## Already generated (no designer needed)

| File | Use |
|------|-----|
| `figure_checklist_vs_search.svg` | **Primary publish asset** (exact text, editable) |
| `figure_checklist_vs_search.png` | Raster for LinkedIn (if converted) |
| This prompt file | Hand to any SOTA image VLLM for a painted/photoreal-infographic variant |
