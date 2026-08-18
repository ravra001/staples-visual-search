# How this app works

This file used to be a detailed step-by-step walkthrough of the visual-search
pipeline. It's been retired in favor of **`frontend/how-it-works.html`**
(open it via the ☰ menu, or `/how-it-works.html` on a running instance) —
that page is kept current as features ship, covers the full system (visual
search, hybrid text search, Shop the Room, Complete the Look, Find Similar,
and the eleven-tool Staples AI chat agent), and renders a live architecture
diagram alongside the explanation.

This markdown file predated pgvector, hybrid text search, Shop the Room, and
Staples AI entirely — none of that made it in before it went stale, so
keeping two competing explanations in sync stopped being worth it. If you
landed here from the Docker image's `COPY README.md GCP_SETUP.md
HOW_IT_WORKS.md RUN_10K.md ./` line: same reasoning applies there, use the
HTML page.

For the embedding-math fundamentals (what a vector is, why cosine similarity,
why L2-normalize) and the offline/startup/query pipeline stages, see
`how-it-works.html`'s "The one idea everything rests on" and "The pipeline at
a glance" sections — same content this file used to carry, kept up to date.

See also: **`README.md`** (setup + architecture overview), **`GCP_SETUP.md`**
(the full Cloud Run/Cloud SQL deployment runbook), **`RUN_10K.md`** (running
the full 10k-catalog demo locally).
