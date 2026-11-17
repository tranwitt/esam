# ESAM crew (CrewAI)

Runs ESAM work without a Claude session: an environment check, then pipelines of three agents
(Triage → Researcher/Author → SE Reviewer) that use **only** tested `esamkit` functions as tools.
Same 5-file structure as the RTOS crew (`aidev/crew/`): `main.py`, `crew.py`, `custom_tools.py`,
`config/agents.yaml`, `config/tasks.yaml`.

```bash
pip install -r requirements-crew.txt          # once; CrewAI 1.15.x (see the file for why not 1.10.1)
python -m esamkit.environment                 # step 0 probes only (no agent, no tokens)
python -m crew.main env                       # step 0 with the Environment Engineer agent
python -m esamkit.usage                       # tokens per model, calls per tool, last 7 days

python -m crew.main review path/to/STD-BRK-001.md   # Pipeline A: A1 + A2, then STOP for the SE
python -m crew.main review path/to/SRS-PWR-001.doc  # Word (.docx, .doc) works the same way
python -m crew.main review topology.png --set id=ICD-BRK-003 --set name="Brake bus topology" \
                           --set revision=A --set attached_to=SUB-BRK-ECU     # a picture diagram
python -m crew.main inbox                           # the same for every document in work/inbox/
python -m crew.main runs                            # runs and their state
python -m crew.main approve <run>                   # the SE's decision: A3 ingest + A4 verify
python -m crew.main reject <run> --note "why"
python -m crew.main undo <run>                      # reverse an ingest exactly
python -m crew.main ask "Which brake requirements have no test?"

python -m esamkit.standards                         # the standards library
python -m esamkit.standards show "MIL-STD-882E 4.4.2"
python -m crew.main author data/briefs/SRS-ENG-001.yaml   # Pipeline B → work/drafts/

python -m crew.main graph status                    # is the graph in step with the baseline?
python -m crew.main graph reload                    # rebuild the graph from the baseline and its documents
python -m crew.main graph reset --yes               # empty the graph

python -m crew.main tasks add review work/inbox/STD-BRK-001.md     # queue work in PostgreSQL
python -m crew.main tasks list
python -m crew.main worker [--once | --drain]                     # process the list
```

| Step | Status |
|---|---|
| 0. Environment: profiles, probes, capabilities, usage ledger, Environment Engineer | ✅ |
| 1. Pipeline A (ontology → document review → **stop for SE approval** → ingest → verify), interactive + inbox modes | ✅ |
| 2. Standards library (MIL-STD-882E) + Pipeline B (SRS authoring, verified, certification evidence pack) | ✅ |
| 3. Task list in the database + worker | ✅ |
| 4. Cockpit Tasks tab | ✅ |

## Step 0: environments

A profile in `config/environments/<name>.yaml` declares what a place offers; `ESAM_ENV=<name>` or `--env`
selects it. Shipped: `local` (this laptop), `pilot-il5` (gateway GPT-5.1 + Nemotron, VM-1 PostgreSQL,
air-gapped; template), `full-janusgraph` (template).

| Probe | Checks | Never does |
|---|---|---|
| host | CPU (warns without AVX2: the local-embedding SIGILL), cores, RAM, Python | |
| files | baseline, ontology, documents; baseline vs ontology | |
| database | connect, PostgreSQL + pgvector versions, latency; DSN shown masked | print the password |
| vector | chunks per collection; the collection's embedding model matches the profile's | load the model |
| graph | PostgreSQL CTE tables and counts; JanusGraph reachability (reported *not implemented in this build*) | |
| embeddings | local: model staged on disk, size, dimension. Endpoint: one real embedding call, dimension | load the local model; contact a public host from an air-gapped profile |
| llm.\<role\> | key variable set; endpoint lists the model; the same for each fallback (warns when only a fallback is usable) | spend tokens; contact a public host from an air-gapped profile |
| crewai | installed version; telemetry and memory off | |

**Capabilities**: each crew tool lists the services it needs (`TOOL_NEEDS` in `esamkit/environment.py`);
a tool is handed to agents only if all of them are OK. **Secrets**: profiles name environment variables,
never values. **Telemetry**: CrewAI/OpenTelemetry telemetry is switched off; crew memory is off (it needs
an embeddings API the pilot doesn't have).

**Usage ledger** (`esamkit/usage.py`): one row per LLM call (model, agent, tokens), tool call (duration,
success) and run, in PostgreSQL `esam_usage` or `work/usage/usage.jsonl` when there is no database. Cost is
shown only if a profile has `llm.pricing` with your own rates.

Run folders: `work/runs/<timestamp>_<name>/` (gitignored).

### Model endpoints: fallbacks and the Responses API

A role in the profile has one primary endpoint and, optionally, fallbacks:

```yaml
llm:
  roles:
    reasoning:
      model: gpt-5.1
      model_env: ESAM_REASONING_MODEL          # when this variable is set in .env it replaces the line above
      base_url_env: ESAM_REASONING_BASE_URL
      api_key_env: ESAM_REASONING_API_KEY
      api: chat                                # chat | responses | auto (chat, then responses on the same endpoint)
      api_env: ESAM_REASONING_API
      fallbacks_env: ESAM_REASONING_FALLBACK   # numbered variables in .env, see below
    coding:                                    # its own endpoint: nothing is shared with reasoning
      model: nemotron-4-15b
      model_env: ESAM_CODING_MODEL
      base_url_env: ESAM_CODING_BASE_URL
      api_key_env: ESAM_CODING_API_KEY
      api_env: ESAM_CODING_API
      fallbacks_env: ESAM_CODING_FALLBACK
```

**The environment file and the profiles.** Any profile key may have a sibling `<key>_env` naming a variable;
when that variable is set (in `esam/.env` or the environment) it is used, otherwise the value written in the
profile. The shipped profiles name exactly the variables in `.env.example`, so `.env` is the one place to set
URLs, models and keys, and `python -m crew.main env` shows what is in effect:

| Role | Variables | Notes |
|---|---|---|
| `reasoning` (all the crew's agents, lessons 05/06) | `ESAM_REASONING_BASE_URL`, `_API_KEY`, `_MODEL`, `_API`, `ESAM_REASONING_FALLBACK_<n>_BASE_URL` / `_API_KEY` / `_MODEL` / `_API` | `ESAM_CHAT_*` is the older name and is still read |
| `coding` (the model approved for writing code) | `ESAM_CODING_BASE_URL`, `_API_KEY`, `_MODEL`, `_API`, `ESAM_CODING_FALLBACK_<n>_…` | Separate from reasoning: no URL or key is shared. Off when it has no model. `crew.crew.coding_llm(profile)` returns its model; no agent of this crew writes code yet |
| `vision` (describes picture diagrams) | `ESAM_VISION_MODEL`, `_BASE_URL`, `_API_KEY`, `_API`, `ESAM_VISION_FALLBACK_<n>_…` | Off until `ESAM_VISION_MODEL` is set. What it leaves unset comes from reasoning |

A fallback states only what differs from the role's primary endpoint (`ESAM_REASONING_FALLBACK_1_API=responses`
is the same endpoint over the Responses API). `ESAM_GATEWAY_URL` / `ESAM_GATEWAY_KEY`, which used to serve both
roles, are still read for whichever of the two has no URL or key of its own. Also set from `.env`:
`ESAM_COLLECTION`, `ESAM_DIAGRAMS`, `ESAM_EMBED_MODEL`, `ESAM_EMBED_BASE_URL`, `ESAM_EMBED_API_KEY`,
`ESAM_EMBED_MODEL_DIR`, `ESAM_PG_DSN`, `ESAM_GREMLIN_URL`. A profile may still list `fallbacks:` itself.

With one route the crew uses a plain CrewAI `LLM` (`api: responses` selects CrewAI's Responses API support).
With several it uses `FailoverLLM` (`crew/llm.py`): a call that fails because the endpoint is unreachable, does
not offer that API or model (404/405), is overloaded (408/429/5xx) or rejects the key is made again on the next
route, and the failed route is skipped for `ESAM_LLM_FAILOVER_COOLDOWN` seconds (300). Errors that would repeat
anywhere (context too long, content filter) are raised unchanged. Which routes served a run is printed at the
end of it. A fallback whose URL or key variable is unset is left out and reported by `python -m crew.main env`.
With no fallback variable set a role has one route and nothing changes.

## The graph: status, reload, reset

The graph is derived: its structure comes from the baseline, its requirements and test cases from the SRS and
STD documents the baseline lists. `crew/graph_admin.py` rebuilds or empties it without an agent, a model or
the lessons folder (it replaces `lessons/04_graph/load_graph.py --reset` for day-to-day use).

| Command | Does |
|---|---|
| `graph status` | Counts by kind, and whether the graph holds exactly what the baseline and documents say; lists what is missing or extra. Exit code 1 when out of step |
| `graph reload` | Reads and checks everything first (the baseline loads, passes the ontology check, documents are readable), then empties the graph and loads it. A baseline with errors is refused and the graph is left as it was; `--force` loads one that only fails the ontology check |
| `graph reset --yes` | Empties the graph and leaves it empty until the next `reload` |

Use `reload` after editing the baseline by hand, after a `git checkout`, after restoring a database, or when
health check 5 says "reload the graph". None of the three touches the baseline, the documents or the search
store. All take `--env <profile>`; `python -m crew.graph_admin …` is the same without the crew's other
dependencies.

## Step 1: Pipeline A (a document → verified ingestion)

Every pipeline command first runs the step 0 probes (no tokens) and refuses to start if a tool it needs is
disabled in this environment.

| Step | Agent | Tools | Decides |
|---|---|---|---|
| A1 ontology check | Triage | `ontology_check`, `ontology_lookup`, `check_coverage` | the tool: the findings adding the document would introduce; ONTOLOGY OK/FAIL |
| A2 document review | Researcher | `review_document`, `verified_by`, `search_documents`, `walk_graph` | the agent compares each test case's pass criteria with the requirement it verifies (MATCHES / STRICTER / WEAKER / UNCLEAR) |
| A2 approval request | SE Reviewer | `ontology_check`, `review_document` | RECOMMENDATION: INGEST / DO NOT INGEST |
| **STOP** | **the SE** | `approve` / `reject` | **a person; nothing is ingested before this** |
| A3 ingest | Researcher | `ingest_document` | the tool refuses without a recorded approval, and re-runs the ontology check first |
| A4 verify | SE Reviewer | `verify_ingestion`, `health_report` | the tool: chunks stored, attached in the graph, test cases + VERIFIES links, search finds it, integrity passes, health before vs after; VERIFIED / NOT VERIFIED |

**Word documents and pictures.** `review` and `inbox` take `.md .txt .docx .doc .rtf` and picture files.
The file is converted to markdown once, when it arrives (`esamkit/readers.py`); the agents review that
markdown, the approval request says it was converted and by what, and lists every picture with how it was
read (described by a model: compare with the picture; not described: its content is not searchable). On
approval the markdown becomes `data/documents/<ID>.md` and the delivered file is kept as
`data/documents/originals/<ID>.<ext>`; `undo` removes both. A picture states nothing about itself, so it
needs `--set id=…` (and normally `name`, `revision`, `attached_to`). `documents.diagrams` in the profile
(`auto | local | off`) decides whether pictures may go to the vision role's model; the pilot templates say
`local`.

What the document must state (SE manual §2.5): a title line `# <ID> — <name>` and the table
`| Document | Type | Revision | Subsystem |`. Missing fields are ontology findings; the SE can supply them
with `--set revision=A --set attached_to=SUB-…`.

**Several documents of one type; documents in parts.** A subsystem (or the vehicle, for an SSDD) may have more
than one document of a type. Each is its own baseline entry with its own id (`SSDD-LTV-001`, `SSDD-LTV-002`, …;
ids run 001–009 per type and owner code) and is reviewed, approved, ingested and undone on its own: a document
replaces another only when it has the **same id** at a later revision. When they are parts of one document,
each says which part it is with a `Part` column in its header table (`2 of 3`), or `--set part="2 of 3"`:

```
| Document | Type | Revision | System | Part |
|---|---|---|---|---|
| SSDD-LTV-002 | SSDD | A | V-001 | 2 of 3 |
```

The ontology check then knows the set: a missing part is a warning (parts can arrive in any order, and the
approval request says which are still missing), two documents claiming the same part is an error. Requirements
split over several SRS parts all land on the subsystem. The required-documents rules are met by any document of
the type; an incomplete set shows as a warning in health check 5 and beside the document in the Cockpit.
Documents without `part` (two ICDs for one ECU) are simply two documents.

**System requirements and software requirements.** Both are written in the SRS, and a marker after `SRS-` in the
**id** says which a requirement is:

| Id | It is | Meaning |
|---|---|---|
| `SRS-SYS-BRK-050` | a **system** requirement | what the subsystem as a whole must do, whatever mix of hardware and software does it |
| `SRS-SW-BRK-060` | a **software** requirement | what the software itself must do |
| `SRS-BRK-011` | a **software** requirement | the unmarked form used before the two were told apart; still read |

```
- **SRS-SYS-BRK-050** The Brake Control ECU shall prevent sustained lock-up of any braked wheel above 8 km/h.
  Safety: SwCI 1 | Verification: T, A

- **SRS-SW-BRK-060** The software shall command pressure release within 20 ms of detecting incipient lock-up.
  Timing: 20 ms max | Verification: T | Traces to: SRS-SYS-BRK-050
```

Each requirement in the graph carries `level` (and `level_source: id`). `Traces to:` from a software requirement
to a system requirement becomes a `TRACES_TO` link. The review of an SRS lists the two levels apart with counts,
shows each parent's text, and says which system requirements no software requirement traces to. The health
report, `verified_by`, `graph reload` and the Cockpit's requirement coverage show the two apart as well. Test
cases cite either kind on their `Verifies:` line.

So that a requirement numbered as the wrong kind is noticed, the review cross-checks every id against the
requirement itself and gives the id it should have: an optional `Level: system` / `Level: software` on its
attribute line, and the subject of its "shall" ("… the software shall" reads as software, "The Brake Control ECU
shall" as system; a software word wins). An id that is not an `SRS-` id is reported and classified by that same
check. The markers and the word lists are in the ontology (`requirement_classification` in
`data/ontology/esam_ontology.yaml`).

**System-level documents (SSDD).** Every document type is attached to a subsystem except the SSDD, which the
ontology lets sit on the vehicle system, one system or a subsystem (`document_types.ssdd.attaches_to:
[Vehicle, System, Subsystem]` in `data/ontology/esam_ontology.yaml`). The last header column then holds
`V-001` or `SYS-…` and may be headed System, Vehicle or Attached to. A sample to try:
`python -m crew.main review lessons/06_missing_document/staged/SSDD-LTV-001.md`. Its chunks carry the
owner's own WBS code (`1.0` for the vehicle) and the graph gets `V-001 ─HAS_ARTIFACT→ SSDD-LTV-001`.
To let another type sit higher up, add `attaches_to` to that type; the class must also be in
`relationships.HAS_ARTIFACT.from`.

**What "ingest" changes**: copies the file into `data/documents/`, appends one line to the baseline's
`artifacts:` (as text, so the YAML's comments survive), stores the chunks, and reloads the graph with its
traceability. A later revision replaces the earlier file and line (kept in the run's `replaced/`). Each ingest
writes `manifest.json`; `undo` reverses exactly that. These are real edits to the SE baseline: review them
with `git diff` and commit them, or `undo`.

**Approving against the recommendation** needs `--against-recommendation` and a `--note`; it is recorded.
Ontology errors can't be approved past: the document is fixed and reviewed again.

Run folder of an intake: `state.json` (state, entry, ontology result, review, approval, health before/after,
verification), `incoming/<ID>.md` (and `incoming/original/<file>` when it was converted), one `.md` per task, `approval_request.md`, `manifest.json`,
`verification_report.md`. Inbox originals move to `work/inbox/done/`.

**Interactive** (`ask`): Triage pins down the entities and the documents that would answer (present or
MISSING), the Researcher answers from tools only with citations, the SE Reviewer re-runs the coverage and
traceability checks and keeps the MISSING section. Read-only: `ask` never changes anything.

## Step 2: standards library and Pipeline B (an SRS from an SE's brief)

**Standards library** (`data/standards/`, `esamkit/standards.py`): only clauses ESAM holds can be cited.

| Standard | Held | From |
|---|---|---|
| MIL-STD-882E (2012) | 74 clauses: 3.2 definitions, section 4 with Tables I–VI, Appendix B | `pdftotext` of the public PDF; `python -m esamkit.standards extract-882e <pdf>` rebuilds it and refuses a PDF with a different SHA-256 |
| DI-IPSC-81433A (1999) | Full text, 39 clauses (the SRS outline + the DID's instructions as I.0–I.4) | Transcribed from the scanned PDF (no text layer) |
| ISO/IEC/IEEE 29148 (2018) | Name and its requirement characteristics only | Copyrighted: no text |

Tables I, IV, V and VI are also held as data (`library.yaml`), and `tests/test_standards.py` checks every Table V
cell against the extracted text. `resolve("<standard> <clause>")` never returns a near miss. Change 1 of 882E (2023)
is not held.

**Pipeline B** (`python -m crew.main author <brief.yaml>`): the SE's brief names the document, its sources and the
safety assessment (severity and control category per safety-significant function). ESAM computes the SwCI and never
assesses safety itself.

| Step | Agent | Tools | Decides |
|---|---|---|---|
| brief check | (none) | `esamkit.srs.check_brief` | SwCI per SSF (Table V); brief vs baseline (e.g. 882E 3.2.31) |
| B1 gather basis | Triage | `read_brief`, `read_source`, `check_coverage`, `walk_graph`, `search_standards`, `standard_clause`, `ontology_lookup` | |
| B2 draft | Researcher | `read_brief`, `read_source`, `standard_clause`, `search_standards`, `save_draft`, `check_draft` | every save is linted at once (`esamkit.srs.check_srs`); errors must reach 0 |
| B3 verify | SE Reviewer | `check_draft`, `read_source`, `standard_clause`, `save_draft` | the lint; the agent confirms values against sources and that each cited clause *supports* the requirement; VERDICT |
| B4 evidence pack | SE Reviewer | `build_evidence_pack` | the pack; the runner rebuilds it from the final draft regardless |

Output in `work/drafts/<doc>_<timestamp>/`: the candidate SRS, `compliance_matrix.md`, `traceability_matrix.csv`,
`lint.md`/`lint.json`, `evidence_pack.md` (provenance and open items) and `se_reviewer_memo.md`. The draft enters
the baseline only through Pipeline A (`review` → SE `approve`). The pack is supporting evidence; certification is
the certifying authority's decision.

## Step 3: the task list (PostgreSQL) and the worker

`esam_tasks` holds queued crew work; `python -m crew.main worker` claims the highest-priority, oldest task, runs the
same command an SE would, and records the outcome. Several workers can run at once: a claim is
`UPDATE … WHERE id = (SELECT … FOR UPDATE SKIP LOCKED)`, so no two take the same task (`tests/test_tasks.py`).

| Kind | Runs | Ends as |
|---|---|---|
| `review <file>` | Pipeline A, A1 + A2 | `done`, outcome "awaiting SE approval: crew.main approve <run>" |
| `approve <run> [--note]` | A3 ingest + A4 verify | `done` (verified) or `failed` |
| `author <brief>` | Pipeline B | `done` with lint counts, reviewer verdict and the evidence pack path |
| `ask "<question>"` | the interactive crew | `done` with the answer file |
| `env` | step 0 with the Environment Engineer | `done` (GREEN LIGHT) or `failed` (NOT READY) |

**The worker never approves.** A review ends waiting for a person. An `approve` task is the SE's own decision,
queued by them and recorded with their login (`--by`); the same refusals apply as for `crew.main approve`
(no approval against the reviewer's recommendation without `--against-recommendation` and a note).

Each task records its worker, attempts, start and finish times, the run id (whose folder and usage-ledger rows
hold the detail), a one-line outcome, and on failure the reason and traceback. A refusal (`sys.exit` in a
command, e.g. "NOT READY: this environment disables save_draft") fails the task with that reason; the worker
carries on. `tasks cancel <id>` removes a queued task; `tasks requeue <id>` puts back a failed one, or one left
`running` by a worker that was stopped.

## Run logs (same mechanism as the aidev RTOS crew)

Every command that runs agents or changes the stores (`env`, `review`/`inbox`, `approve`, `undo`, `author`, `ask`,
and the same commands run by the worker) captures everything it prints, CrewAI's agent and tool panels included:

| File (in the run folder) | What |
|---|---|
| `terminal.log` | the raw output, ANSI colour codes kept (`TeeLogger`, as in `aidev/crew/main.py`) |
| `terminal.html` | the same rendered by `ansi2html` (dark background); the Cockpit links to it |
| `error.log` | on a failure: the traceback, or the reason a command stopped |
| `terminal_<command>.log/.html` | a second command on the same run (`approve`, `undo`) |

Capture starts in `work/logs/` and moves into the run folder when the command ends (the folder is named after the
run, so it exists only part-way through). A command that stops before making a run keeps its log in `work/logs/`.
Colours appear when run from a terminal; output piped to a file is plain, as in the aidev crew.

## Step 4: the Cockpit Tasks tab

`uvicorn cockpit.app:app --port 8080`, tab **Tasks**:

- **Waiting for your decision**: reviews stopped for SE approval (the count shows on the tab), with the approval
  request and the run log. *Approve* queues an `approve` task under the name the SE typed (a worker ingests and
  verifies); *Reject* needs a note and records it at once. Approving against the reviewer's recommendation needs the
  box ticked and a note, as on the command line.
- **Task list**: every task with its status, who added it, the outcome, a link to its run log, and *Cancel* while queued.
- **Recent runs**: each run folder with its state, reviewer verdict, lint counts, decision, and links to the terminal
  log, the reports and the evidence pack.
- **Add a task**: ask a question, review a document (from `work/inbox/` or lesson 06's staged files), draft an SRS
  from a brief in `data/briefs/`, check the environment. Only files the Cockpit lists can be queued; run files are
  served only from `work/runs/` and `work/drafts/` (no `..`, only `.html .md .json .log .csv`).
- In a snapshot the tab is read-only.

The Cockpit listens on 127.0.0.1 here. Before others can reach it (the pilot), these endpoints need the pilot's
sign-on: a decision records the name typed, and nothing checks who the person is.
