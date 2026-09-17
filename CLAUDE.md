# KaroSpaceAgent

Agent tooling that navigates the creation of **KaroSpace** spatial-transcriptomics
viewers from raw data. This repo holds the agent config (skills + subagents); it
does not contain the viewer or the pipeline code.

## The two tools this agent drives

| Tool | Lang | Location | Role |
| --- | --- | --- | --- |
| `karospace` | Python | on PATH (`karospace --help`) | Exports an `.h5ad` / SpatialData `.zarr` → standalone HTML viewer |
| `karospace-companion` | Rust | `../KaroSpaceCompanion/target/release/karospace-companion` | Pre-processes an `.h5ad`: spatial graph, normalization, analytics, viewer JSON |

Source repos: `../KaroSpace` and `../KaroSpaceCompanion`. Read their `README.md`
for the full flag surface — both are large and change over time; never assume a
flag exists, check `--help`.

## The job, in one line

Take a researcher's raw dataset + plain-English intent → inspect it → choose
correct flags → (optionally) enrich with the companion → run the export → read
errors → fix → confirm the viewer actually wrote.

## Data-handling rule (non-negotiable)

These are sensitive human spatial datasets (GDPR / Karolinska). The reasoning
step only ever needs **sanitized metadata**: obs column *names*, dtypes, a few
example values, cardinalities, CLI `--help`, and error text — produced by
`karospace <file> --inspect-input`. **Never** read or transmit the raw
expression matrix, cell coordinates, or patient identifiers. The heavy compute
(`karospace`, `karospace-companion`, scanpy, DESeq2) always runs locally, where
the data lives.

## Model note

The brain is Claude (metadata-only, per the rule above). When this graduates to a
standalone product, keep the same split: local hands, Claude for reasoning, only
sanitized metadata crosses the boundary.
