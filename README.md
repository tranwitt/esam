# ESAM — learning embeddings, vector stores and graph queries

A slow, hands-on walk through the three ideas behind the ESAM knowledge layer:

1. **Embeddings**: turning text into vectors whose *direction* encodes meaning.
2. **Vector stores**: storing those vectors and finding the nearest ones fast.
3. **Graph queries**: following *relationships* (what connects to what), which similarity
   search cannot do.

Everything is built on a **synthetic** military light tactical vehicle (SYNTH-LTV): a
**Brake Control ECU on an RTOS** and a **Driver Digital Cluster on Linux**, linked by a SAE J1939
message (EBC1). The structure uses real **MIL-STD-881F Appendix G** WBS codes. All content below
that level is invented.

> **Coming back after a break or a reboot?** See [`HOW_TO_START.md`](HOW_TO_START.md).

## Release 0.1.0: what ESAM does

The lessons grew into a working system an SE can use (learning build, synthetic data):

| Part | What it gives an SE | Read |
|---|---|---|
| **SE baseline + ontology** | The vehicle structure in YAML, checked against the programme vocabulary (MIL-STD-498 document names, MIL-STD-882E criticality, rules for which documents each subsystem needs) | SE manual §1, §8 |
| **Hybrid answers** | Questions answered from the documents *and* the graph, with **MISSING** when a needed document or test doesn't exist | §7, `docs/how_esam_finds_answers.html` |
| **Traceability + health report** | Requirements and test cases read from the documents; six checks on the baseline, only integrity blocks | §5, §10 |
| **SE Cockpit** | Dashboard, Graph, Ontology and Tasks tabs (`uvicorn cockpit.app:app --port 8080`) | `cockpit/README.md` |
| **The agents (CrewAI)** | Review a document → **SE approves** → ingest + verify; ask questions; draft an SRS from an SE brief with an evidence pack; a task list and worker; full run logs | §11, §12, `crew/README.md` |
| **Standards library** | MIL-STD-882E (extract) and the SRS DID (DI-IPSC-81433A), cited only by real clause | §11.4 |

**For SEs:** [`docs/ESAM_SE_User_Manual.md`](docs/ESAM_SE_User_Manual.md) (§12: working with the agents).
**For whoever runs it:** [`HOW_TO_START.md`](HOW_TO_START.md) and [`crew/README.md`](crew/README.md).

## Built for the ESAM pilot's constraints

| Constraint (from `ESAM_Pilot_Implementation_v1.0.md`) | How this code handles it |
|---|---|
| No internet on the pilot network | Model loaded from a pre-staged folder; HF offline mode forced; no runtime downloads |
| LLM gateway has chat models but **no embeddings endpoint** | Local CPU embedder (`BAAI/bge-base-en-v1.5`, 768-dim) behind a swappable interface |
| PostgreSQL 16 + pgvector on VM-1 | Same Postgres 16 + pgvector locally, in Docker (`docker-compose.yml`); only the connection string differs |
| No GPU | Everything runs on CPU |
| Neo4j not available; recursive CTEs now, JanusGraph later | Graph lessons use SQL recursive CTEs, then the Gremlin equivalent |

## Layout

```
esam/
├── SESSION_HANDOFF.md        status, local credentials/DBs, resume steps, pending tasks
├── docker-compose.yml        local PostgreSQL 16 + pgvector 0.8 (127.0.0.1:5433, volume esam-pgdata)
├── esamkit/                  the reusable, swappable parts
│   ├── config.py             all settings from env vars / .env
│   ├── factory.py            the ONLY place that picks concrete backends
│   ├── baseline.py           SE YAML baseline -> vertices + edges (no DB code)
│   ├── chunking.py           documents -> heading-labelled chunks
│   ├── chat.py               the chat model for lessons 05/06 (a thin wrapper over llm.py)
│   ├── llm.py                model endpoints: Chat Completions + Responses API, fallback endpoints
│   ├── readers.py            any supported file -> markdown (.md .txt .docx .doc .rtf, pictures)
│   ├── word_docx.py, word_doc.py, docmodel.py   built-in Word readers (.docx and legacy .doc), no dependencies
│   ├── office.py             optional LibreOffice conversion for .doc / .rtf
│   ├── diagrams.py           picture diagrams -> searchable text (vision model, SVG labels, OCR)
│   ├── embedders/
│   │   ├── base.py           Embedder interface (embed_documents / embed_query)
│   │   ├── local_st.py       sentence-transformers, from local folder, offline
│   │   ├── openai_compat.py  any OpenAI-compatible /v1/embeddings endpoint (ESAM_EMBED_BASE_URL)
│   │   └── prefixes.py       query/passage prefixes by model family (BGE, E5)
│   ├── vectorstores/
│   │   ├── base.py           VectorStore interface + model-mismatch guard
│   │   └── pgvector_store.py PostgreSQL + pgvector, HNSW + iterative filtered scans
│   └── graphstores/
│       ├── base.py           GraphStore interface
│       └── postgres_cte.py   pilot graph schema + recursive-CTE queries (commented SQL)
│   ├── ontology.py, health.py, traceability.py, coverage.py, hybrid.py   ontology, checks, answers
│   ├── intake.py, srs.py, standards.py      document intake, SRS checks + evidence pack, standards library
│   ├── environment.py, usage.py, tasks.py, runs.py   environments, usage ledger, task list, run folders
├── cockpit/                  SE Cockpit (FastAPI + one offline HTML page; snapshot export)
├── crew/                     the agents: main.py, crew.py, llm.py (fallback endpoints), custom_tools.py, config/*.yaml;
│                             graph_admin.py (`python -m crew.main graph status | reload | reset`)
├── config/environments/      environment profiles (local, pilot-il5, full-janusgraph); secrets by name only
├── data/
│   ├── baseline/vehicle_decomposition.yaml   SE-owned structure (ESAM YAML format)
│   ├── documents/*.md                        synthetic ICD / SDD / SRS documents (MIL-STD-498 names)
│   ├── documents/originals/                  Word / picture files as delivered (kept by the intake pipeline)
│   ├── ontology/esam_ontology.yaml           the SE vocabulary and rules
│   ├── standards/                            standards library (clause text + catalogue with source hashes)
│   └── briefs/                               SE authoring briefs for SRS drafting
├── docs/                     SE manual, SE/PM briefing page, source PDFs of the standards, MIL-STD-881F excerpt
├── work/                     (not in git) inbox, runs with their logs, drafts, task logs
├── lessons/
│   ├── 00_setup/             install, stage the model, air-gap transfer bundle
│   ├── 01_corpus/            what we're embedding and why it's shaped this way
│   ├── 02_embeddings/        vectors and cosine similarity, by hand
│   ├── 03_vector_store/      chunk -> embed -> store -> search, and its blind spot
│   ├── 04_graph/             recursive CTEs (+ Gremlin / Cypher equivalents)
│   ├── 05_hybrid/            vector entry point -> graph reach -> chat model answer
│   ├── 06_missing_document/  "that document doesn't exist" -> ingest -> right answer
│   └── 07-09/                ontology, traceability, health report
└── tests/                    pytest (DB tests use database esam_test)
```

## Quick start

```bash
cd esam
python3 -m venv .venv && source .venv/bin/activate
pip install torch --index-url https://download.pytorch.org/whl/cpu
pip install -r requirements-dev.txt                 # requirements.txt + pytest
python lessons/00_setup/stage_model.py              # once, needs internet (~440 MB)
docker compose up -d --wait                         # local Postgres + pgvector

python lessons/02_embeddings/embeddings_by_hand.py
python lessons/03_vector_store/ingest.py --reset
python lessons/03_vector_store/search.py --demo
python lessons/04_graph/load_graph.py --reset
python lessons/04_graph/queries.py
python lessons/05_hybrid/hybrid.py "If we change the EBC1 message period, what is affected?"

pytest -q                                           # ~200 tests (those needing the database or CrewAI skip without them)

pip install -r requirements-crew.txt                # the agents (CrewAI) and run logs
uvicorn cockpit.app:app --port 8080                 # SE Cockpit at http://127.0.0.1:8080
python -m crew.main env                             # the agents' environment check
```

## Moving to the pilot / swapping backends

Nothing in `lessons/` imports a backend. Change environment variables only:

```bash
# Local Docker database (default, nothing to set)
ESAM_PG_DSN=postgresql://esam_user:esam_local_only@127.0.0.1:5433/esam_pilot

# ESAM pilot VM-1
ESAM_PG_DSN=postgresql://esam_user:***@<vm-1>:5432/esam_pilot

```

## Choosing the embeddings model and where it runs

Two settings decide it (`.env`, or the environment):

| Setting | Meaning | Default |
|---|---|---|
| `ESAM_EMBED_MODEL` | **which model**: a Hugging Face id when it runs locally, or the name the endpoint serves | `BAAI/bge-base-en-v1.5` |
| `ESAM_EMBED_BASE_URL` | **where it runs**: the URL of an OpenAI-compatible embeddings endpoint (ESAM posts to `<URL>/embeddings`). Unset = on this machine, from `models/` | unset |
| `ESAM_EMBED_API_KEY` | the endpoint's key, if it wants one | unset |

```bash
# Local model (default). Another local model: stage it, then name it.
ESAM_EMBED_MODEL=BAAI/bge-small-en-v1.5        # folder: models/bge-small-en-v1.5 (or ESAM_EMBED_MODEL_DIR)

# An endpoint: setting the URL is the switch. Ollama, TEI, vLLM, OpenAI, a gateway route:
ESAM_EMBED_BASE_URL=http://127.0.0.1:11434/v1
ESAM_EMBED_MODEL=nomic-embed-text
```

`python -m esamkit.environment` shows the model and URL in effect and, for an endpoint, makes one real
embedding call to confirm the model answers and report its dimension. The crew's profiles carry the same two
settings by name (`embeddings.model_env`, `embeddings.base_url_env` in `config/environments/*.yaml`).
Optional: `ESAM_EMBED_DIMENSIONS`, `ESAM_EMBED_BATCH_SIZE`, `ESAM_EMBED_QUERY_PREFIX` / `_DOC_PREFIX`
(all in `.env.example`). The older names (`ESAM_EMBED_MODEL_ID`, `ESAM_OPENAI_BASE_URL`,
`ESAM_OPENAI_EMBED_MODEL`, `ESAM_EMBED_BACKEND=openai`) still work.

**Changing the embedding model means re-embedding everything.** Each collection records the
model id and dimension it was built with and refuses to open with a different one
(`ModelMismatchError`). Use a new `ESAM_COLLECTION` or `ingest.py --reset`.

## Word documents and picture diagrams

Everything that reads a document goes through `esamkit/readers.py`, which turns the file into the same
markdown the rest of ESAM already understands:

| File | Read by |
|---|---|
| `.md`, `.txt` | as is |
| `.docx` | built-in reader (headings, lists, tables, bold requirement ids, captions, text boxes, embedded pictures) |
| `.doc` (Word 97-2003) | LibreOffice when installed (`ESAM_DOC_CONVERTER=auto`), otherwise the built-in binary reader; both give the same requirements and test cases on the test corpus |
| `.rtf` | LibreOffice, otherwise text only |
| `.png .jpg .gif .webp .bmp .tif .svg` | a picture diagram, see below |

```bash
python -m esamkit.readers path/to/SRS-PWR-001.doc     # print the markdown ESAM would index
```

A **picture diagram** (a picture file, or one embedded in a Word document) becomes a quoted block in the
markdown, so it is chunked, embedded and searchable with its section. `ESAM_DIAGRAMS` sets how it is read:
`auto` (a vision model describes it **if** `ESAM_VISION_MODEL` is set; results are cached by picture
content), `local` (never sent to a model: SVG labels, text typed in Word shapes, OCR if `tesseract` is
installed), `off` (name and caption only). With no vision model configured, no picture leaves the machine.

What a Word document must contain to be recognised is the same as for markdown: a title
`<ID> — <name>` (Title or Heading 1 style) and the `Document | Type | Revision | Subsystem` table.

## Model endpoints: Responses API and fallbacks

The chat model (`esamkit/llm.py`) speaks Chat Completions (`ESAM_CHAT_API=chat`, default), the Responses
API (`responses`), or tries chat and then responses on the same endpoint (`auto`). Fallback endpoints are
tried in order when a call fails in a way another endpoint could fix (unreachable, 404/405, 408/429, 5xx,
rejected key); each inherits what it leaves unset:

```bash
ESAM_CHAT_FALLBACK_1_API=responses                          # same endpoint, the Responses API
ESAM_CHAT_FALLBACK_2_BASE_URL=https://second-gateway/v1     # another endpoint
```

The crew has the same per role in its profile (`crew/README.md`, "Model endpoints").

To add a backend, implement the interface and add one branch to `factory.py`. For a different
vector database, that's the five methods of `VectorStore`; for JanusGraph, the methods of
`GraphStore`, using the Gremlin in `lessons/04_graph/gremlin_equivalents.md`.
