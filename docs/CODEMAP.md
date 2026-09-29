# Code map: where things are

Read this first. Every module has a docstring that explains it in more detail.

## The flow in one picture

```
make ingest (ff/ingest/pipeline.py)
  └─ ff/ingest/dataset.py        builds the whole dataset, in this order:
       loghub.py                 1. read the REAL LogHub logs (5 components, last 72 h)
       log_events.py             2. detect anomalies in them
       plan.py                   3. plan 120 incidents (scenario, cause, alert, time, 80/20 split)
       telemetry.py              4. write synthetic logs for the planned incidents
       log_events.py             5. detect anomalies in all logs; each incident starts at one
       simulator.py              6. six months of changes + the causes behind each incident
         ├─ scenarios.py            what breaks in each scenario (cause, breaking line, fix)
         ├─ repo.py                 files, diff hunks, config / Terraform parameters
         ├─ cycles.py               release trains, features, knowledge documents
         └─ describe.py             change requests + plain-language descriptions
  ├─ deploy_summarizer.py        one narrative per event (what the retriever indexes)
  └─ doc_indexer.py              knowledge base -> docs/architecture/ -> Chroma

make app / make chat (an investigation)
  app/streamlit_app.py | ff/engine/cli.py
    └─ ff/engine/guide.py        the step-by-step Q&A (one question per step)
         └─ ff/engine/session.py one conversation with the model
              ├─ ff/retrieve/ranker.py      picks the records for each stage
              │    └─ diff_ranker.py         ranks diff hunks: which line broke it?
              ├─ ff/llm/model.py            prompt formats + the token-by-token generation loop
              ├─ ff/llm/streaming.py        StreamingLLM caches (sinks + anchored start region)
              ├─ ff/engine/ledger.py        what the engineer confirmed / ruled out
              └─ ff/engine/rca.py           the RCA report, built from the ledger only
```

## Folders

| Folder | What it holds |
| --- | --- |
| `ff/config.py` | every setting: timeline, the 15 platform components and their connections, budgets, model choice |
| `ff/ingest/` | building the dataset (see above) |
| `ff/store/` | SQLite (records) and Chroma (text search) |
| `ff/retrieve/` | retrieval and the suspect-line ranking |
| `ff/llm/` | the model, StreamingLLM caches, a tiny random model for tests |
| `ff/engine/` | sessions, stages, the guided Q&A, the terminal chat, the ledger, the RCA report, LangGraph flow |
| `ff/eval/` | `make verify`, walkthroughs, experiments and their reports |
| `ff/train/` | training data (`chat_data.py`: conversations; `lora_data.py`: guided-path examples) and the LoRA trainer |
| `app/` | the Streamlit app |
| `data/` | the generated dataset (`data/README.md`) and training data (`data/training/`) |
| `docs/architecture/` | the generated knowledge base the assistant reads |
| `tests/` | offline tests (tiny model, hash embeddings, small world: 24 incidents over 70 days) |

## Common tasks

| I want to ... | Look at |
| --- | --- |
| add a failure scenario | `ff/ingest/scenarios.py` (builder + `SCENARIOS` entry) and `ff/ingest/telemetry.py` (`FAULTS`: its log lines) |
| add a component or connection | `ff/config.py` (`COMPONENTS`, `TOPOLOGY_CALLS`), `ff/ingest/repo.py` (files, parameters), `telemetry.py` (log format) |
| change what the model sees per stage | `ff/retrieve/ranker.py` (`retrieve`, `KNOWLEDGE_KINDS`) |
| change the Q&A flow | `ff/engine/guide.py` |
| check everything still holds | `make verify` (`ff/eval/verify.py`), `make ci` |
