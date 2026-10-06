# Setup and operations

## Python environment troubleshooting

If CLI startup reports `No module named 'pydantic_core._pydantic_core'`, inspect
the **first** import error, not just the final Agent Framework message suggesting
that `agent-framework-foundry` is missing. The connector can already be installed
while one of its native dependencies is incompatible with the interpreter.

One observed cause was recreating `.venv` with Python 3.14 over an environment
previously installed with Python 3.13. Its Pydantic extension still had a
`cp313` binary tag; Python 3.14 requires a compatible `cp314` or stable-ABI build.
Other compiled dependencies can have the same mismatch.

```powershell
.\.venv\Scripts\python.exe --version
Get-ChildItem .\.venv\Lib\site-packages\pydantic_core\*.pyd
.\.venv\Scripts\python.exe -m pip check
```

Do not reuse installed packages across Python minor versions. For a clean rebuild,
close processes using the environment, deactivate it if active, and preserve it
under a backup name before creating a fresh one. Choose an unused backup name:

```powershell
deactivate  # Only if this environment is active
Move-Item .venv ..\hierarchical-agentic-rag-venv-backup
python --version  # Confirm this is the Python version you intend to use
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe -c "import pydantic_core; from agent_framework.foundry import FoundryChatClient; print('Imports OK')"
.\.venv\Scripts\hrag.exe provision --help
.\.venv\Scripts\Activate.ps1
```

This leaves `.env` and Azure resources unchanged. The `--help` check verifies
startup without provisioning anything. Keep the backup outside Git and do not
delete it until the replacement has been verified. Alternatively, repair only
the identified incompatible distributions using the current interpreter's
`python -m pip install --force-reinstall`, preserving their versions. Merely
reinstalling the pure-Python Foundry connector does not repair native dependencies.

## Existing resources

Create/configure these through your normal approved infrastructure process:

| Resource | Requirements |
|---|---|
| Azure Storage | Private source PDF container; separate derived-page container |
| Azure AI Search | Region/tier supporting knowledge bases and semantic ranking; semantic ranker enabled; Entra data-plane authentication enabled |
| Content Understanding | Foundry resource endpoint in a supported region; `prebuilt-documentSearch` with API `2025-11-01`; resolvable completion/embedding model deployments |
| Microsoft Foundry | Project endpoint and a deployed Responses-compatible model with function calling and structured outputs |

GPT-4.1 is a reasonable initial model choice. The setting is a **deployment name**,
not an assurance that that model is available in every region. Validate model
availability and limits in your own project.

Fill in `.env` using `.env.example`. `DefaultAzureCredential` uses your Azure CLI
sign-in locally and can use managed identity in Azure. No API keys, connection
strings, storage account keys, or SAS tokens are required. For production,
constrain the credential chain/environment so the intended managed identity is
selected; do not leave unintended developer credentials on the host.

### Content Understanding configuration

Install updated dependencies with `python -m pip install -e ".[dev]"`.
Replace `HRAG_DOCUMENT_INTELLIGENCE_ENDPOINT` with
`HRAG_CONTENT_UNDERSTANDING_ENDPOINT=https://YOUR-FOUNDRY.services.ai.azure.com`.
This is the **resource endpoint**, not the project endpoint or the old
Document Intelligence `cognitiveservices.azure.com` endpoint.

The default analyzer is `prebuilt-documentSearch`. Its verified configuration
enables OCR, layout, figure descriptions and figure analysis, and returns detailed
physical pages. Descriptions appear in Markdown image titles; supported chart
analysis appears as chart data, and diagram analysis as Mermaid. The application
indexes page-bounded windows of that enriched Markdown, not the document summary
or service-generated cross-page chunks. The application separately embeds those
chunks for Search; it does not reuse CU-generated embeddings.

Model requirements belong to CU, independently of `HRAG_MODEL_DEPLOYMENT`, which
still selects the MAF query model. Use `ContentUnderstandingClient.get_analyzer()`
and `get_defaults()` to inspect current model keys and mappings. The analyzer
definition can evolve even under a pinned API version. If resource defaults do
not resolve its models, set **request-scoped** mappings in `.env`, for example:

```dotenv
HRAG_CONTENT_UNDERSTANDING_ANALYZER=prebuilt-documentSearch
HRAG_CONTENT_UNDERSTANDING_MODEL_DEPLOYMENTS={"prebuilt-analyzer-completion-mini":"gpt-5","prebuilt-analyzer-embedding":"text-embedding-3-large"}
HRAG_CONTENT_UNDERSTANDING_PROCESSING_LOCATION=geography
```

Use keys reported by your analyzer and actual supported deployment names, not
model names guessed from the agent deployment. The example above was live-tested
on a synthetic PDF against the configured resource with its existing deployments.
The unmodified resource defaults failed with "no completion model deployment ...
resolved"; request-scoped mappings fixed this without changing shared defaults.
A further live two-page synthetic bar-chart test returned a figure description
and structured chart data on page 1, with limitations text on page 2; the
application's span validation and page slicing passed. These are SDK/ingestion
smoke tests, not scientific visual-accuracy or end-to-end RAG tests.

An empty mapping uses resource defaults. The application never deploys models or
patches shared defaults. Default processing is restricted to `geography`;
`dataZone` or `global` must be explicitly configured if required and approved.
Custom analyzers must return one unsegmented document with full Markdown,
physical page spans and figure descriptions/analysis enabled. Multiple content
items, service warnings, invalid spans or unmapped figures fail before publishing,
rather than silently dropping content or falling back to OCR-only extraction.

## Least-privilege role assignments

Assign these roles at the narrowest practical resource/container scope. The
provisioner, ingester, and query service can be separate identities.

| Identity/action | Role |
|---|---|
| Provision Search index/source/base | Search Service Contributor on Search |
| Ingest Search documents / remove stale chunks | Search Index Data Contributor on Search |
| Read index definition for ingestion preflight | Search Service Contributor on Search, or a custom role allowing index-definition reads |
| Query knowledge base | Search Index Data Reader on Search |
| Read original PDFs | Storage Blob Data Reader on source container |
| Write extracted pages / create derived container | Storage Blob Data Contributor on derived container (account scope if it must create the container) |
| Query extracted pages | Storage Blob Data Reader on derived container |
| Run Content Understanding | Cognitive Services User on Foundry resource |
| Generate chunk embeddings (application identity) | Cognitive Services OpenAI User on embedding resource |
| Generate query embeddings (Search system-assigned managed identity) | Cognitive Services OpenAI User on the same embedding resource |
| Call Foundry deployed model | Azure AI User on Foundry project (and any model inference role required by your resource configuration) |

Search calls the embedding deployment for query vectorization using its
system-assigned managed identity; no API key is stored in the index definition.
It does not call Blob, Content Understanding, or a generative answer model.
There is no Search managed-identity Blob indexer or answer-synthesis role to configure.

Enable private endpoint connectivity/DNS or permitted public network access for
each service. RBAC alone does not bypass firewalls. Run the application from a
network with access to all endpoints.

## Commands

```powershell
hrag provision
hrag ingest --blob "policies/claims.pdf"
hrag ingest --prefix "policies/"
hrag ingest --prefix "policies/" --top 20
hrag ask "Which exclusions affect the emergency coverage?"
```

`--top N` limits a container or prefix scan to the first N PDFs returned by Blob
listing. N must be a positive integer; omitting it retains the unlimited scan.
Non-PDFs are skipped without consuming the limit. A smaller matching set is
ingested in full; no matching PDFs remains an error. Processing still stops on
the first ingestion failure. The limit does not sort by modification time,
skip previously ingested PDFs, or provide a continuation cursor.
Use `--blob "name.pdf"` for one exact file; combining `--blob` with `--top` is
rejected before Azure calls.

## Usage and cost reporting

```powershell
hrag ingest --blob "policies/claims.pdf" --usage-report .\claims-usage.json
hrag ingest --prefix "policies/" --top 20 --usage-report .\batch-usage.json
hrag ask "Which exclusions affect emergency coverage?" --usage-report .\ask-usage.json
```

The optional report path must not exist and its parent directory must exist.
It is reserved before Azure calls, avoiding accidental overwrites. Without the
option, ingestion still prints the summary JSON to stdout and `ask` includes it
in the `usage` field of the answer JSON. Progress and summaries go to stderr.
Separate report files contain usage metadata only, not retrieved text or prompts;
they do include source blob names and deployment names. Protect them accordingly.

Reports contain a run ID, UTC start/end, elapsed seconds, outcome, counters,
per-operation timing/status/token events, applied rates and cost limitations.
The ingestion period covers client setup/preflight through all selected PDFs and
client cleanup. Stats accumulate across documents and completed batches even if
a later document fails. Ask retains its `answered`, `insufficient_context` or
`budget_exhausted` outcome. Exceptions produce `failed` reports before propagating
with a nonzero exit code. A process kill or machine shutdown cannot produce a
final report. Report write failures are logged and fail successful commands;
they do not replace an already-active service exception.

| Component | Measured here | Not measured here |
|---|---|---|
| Content Understanding | Analyze operations/durations, validated extracted physical pages, Markdown characters, figures | Internal model tokens, billable page meters |
| Chunk embeddings | Each returned response's prompt/total tokens, batch durations, chunk counts | Hidden retry/server work without usage |
| MAF investigator and writer | Returned input/output/total tokens, available cached/reasoning subsets, run durations | Usage from an aborted agent run without a final response |
| IQ / Search | Retrieval operations/durations, index upload/delete counts, indexed/deleted chunks | Search-side query embeddings, semantic/capacity charges |
| Blob | Downloaded source/artifact bytes, successful artifact upload/download counts | Azure transaction meters, source-property checks, retries, storage/egress charges |
| Investigation | Tool/search attempts, unique selected chunks/documents, opened/cached pages, evidence characters | Per-underlying-model-call timing within one MAF run |

MAF aggregates the native tool loop's usage into each `Agent.run` response.
We count that response **once**, then add subsequent assessment rounds and the
writer. SDK retries are not additional visible events. Interrupted runs can
have billed calls without returned counters, even if earlier model calls within
that same native loop completed. Successful prior runs/batches retain their usage.
An embedding response's usage is retained before validating vectors or uploading
to Search. Counters denote application operations, not invoice quantities.
Omitted operation counters mean that stage was not reached.

Configure optional prices using **actual deployment names** and your applicable
USD per million token rates, for example:

```dotenv
# Illustrative numbers ONLY; replace names and values with your contracted prices.
HRAG_TOKEN_RATES_USD_PER_MILLION={"my-chat":{"input":1.0,"output":4.0,"cached_input":0.25},"my-embedding":{"input":0.1,"output":0}}
```

Rates must be finite and nonnegative. For a priced response:

```text
USD = ((input - cached) * input_rate + cached * cached_rate
       + output * output_rate) / 1,000,000
```

Cached tokens are a subset of input; reasoning tokens are a subset of output.
Neither is added again to total tokens or cost. If `cached_input` is omitted,
all input tokens use the input rate (no assumed cache discount). If a discounted
rate is configured but no cached-token count was returned, cost is unavailable
for that event. `output` defaults to zero for embedding-only rate entries; always
set it explicitly for chat deployments.

`reported_tokens` sums only available counters, and may therefore be incomplete.
`null` means no counter was returned, not zero consumption; an empty `events`
list means no tracked operation was attempted. `estimated_token_cost_usd` is
the sum of priceable events only, or `null` when none can be priced.
`unpriced_events` identifies events omitted from that subtotal. Missing prices
or usage do not prevent ingestion or answering. There are no built-in current
Azure prices or automatic billing queries.

Use Azure Cost Management and the corresponding resource/deployment meters to
reconcile full costs, especially CU processing and Search-hosted vectorization.
This report does not include CU page meters, Search capacity/semantic charges,
Blob/storage/network costs, taxes, or unseen billed retries. It must not be used
as a complete invoice or a hard spend limit.

## Broad cross-document workflow

This is an explicit `hrag ask --broad` mode. Existing focused queries, indexes and
stored manifests remain compatible; nothing requires a new CU extraction merely
to enable broader reasoning.

### Enable and populate the document catalog

1. Set `HRAG_CATALOG_INDEX_NAME` to a dedicated index name, different from
   `HRAG_INDEX_NAME`. The default is `document-catalog`.
2. Run `hrag provision --catalog` using the existing provisioning identity.
   This also follows the existing chunk-index/source/base provisioning path.
3. For **already indexed** PDFs, run `hrag catalog --top 20`, optionally with
   `--prefix "reports/"`, or `--blob "reports/study.pdf"` for one PDF.
   This reads current indexed provenance and persisted full Markdown; it does not
   analyze PDFs, generate embeddings, create a new extraction revision or modify
   existing chunks. Source ETag, manifest identity and Markdown hashes are checked.
4. For newly ingested PDFs, use `hrag ingest --catalog` with the usual selectors.
   The catalog row is published only after chunk uploads and stale-revision
   cleanup succeed.
5. Run `hrag ask --broad "Compare ..."` with an optional new `--usage-report` path.

Batch `catalog` scans Blob listing order and skips non-PDFs and PDFs without
indexed chunks. Each unindexed PDF emits a warning and increments the usage
counter `documents_skipped_unindexed`. `--top N` counts **successfully catalogued
documents**, so skipped files do not consume the limit; fewer than N eligible
documents is valid, but zero remains an error. `documents_started` includes
attempted unindexed PDFs; `documents_completed` counts only published entries.
This differs from ingestion, which still processes every selected PDF.

An exact `catalog --blob "name.pdf"` request remains strict: an unindexed PDF
raises an error explaining that ingestion is needed first. Skipping is limited
to the specific no-indexed-chunks condition in batch mode. Authentication,
service, stale-source, missing-artifact, integrity and mixed-revision failures
still stop the run. No PDFs are automatically ingested during backfill.

Entries published before a failure remain available. Rerunning the command
refreshes catalog rows without repeating CU/embedding work. Use a new usage-report
filename, for example `--usage-report .\catalog-usage-retry.json`, because report
files are never overwritten. Batch skipping may require scanning many blobs when
only a small proportion are indexed; use `--prefix` to narrow the scan.

Catalog provisioning requires Search Service Contributor. Backfill requires
index-definition read permission for preflight, Search Index Data Reader on the
chunk index, Search Index Data Contributor on the catalog, and Blob read access
to both source and derived artifacts. Broad queries require Search Index Data
Reader on both indexes plus existing IQ, Blob and model permissions. Search's
managed identity still performs query vectorization.

The catalog has one replaceable row per document ID, with revision/ETag, source
identity, title, page count, a bounded extractive overview and section navigation.
Up to six 500-character prose samples are spread across a report; up to 200
Markdown headings are retained. Fence content is excluded from the prose samples.
Ranges are navigation approximations and may overlap on heading pages. Dates,
entities and report categories are not guessed. These fields are not citations.
There is no additional model call for this metadata.

Catalog discovery uses text search plus semantic ranking. IQ retains hybrid chunk
discovery, including visual descriptions/chart content. A document appearing only
in IQ can participate with an explicitly labelled chunk navigation hint; the
catalog must exist and contain at least one row. Partially catalogued corpora have
less document-level discovery coverage, which should be considered in evaluation.

Use `ingest --catalog` for future updates. Ordinary `ingest` does not update the
optional catalog. Refresh with `catalog` afterward: mixed catalog/IQ revisions
fail rather than combining old and new evidence. Publishing indexes is not
transactional; avoid querying while re-ingestion/backfill is active. A catalog
write failure leaves successfully indexed chunks intact and reports failure;
retry `catalog` without paying for CU again.

### Query execution and budgets

1. A tool-free MAF planner produces bounded, focused research facets and explicit
   minimum document counts (at least two overall). A request exceeding the
   configured document budget returns `budget_exhausted`; it is not quietly relaxed.
2. Each facet runs one catalog search and one IQ search. Document IDs are
   deduplicated per result list, ranked using reciprocal rank contributions,
   then selected with preference for less-covered facets.
3. Independent MAF workers investigate selected documents under a semaphore.
   Workers use document/revision-filtered hybrid chunk searches, returning up to
   three distinct candidate pages per search after overfetching. They open
   physical pages lazily, can expand adjacent pages or search non-adjacent pages,
   and do not load the full Markdown merely to locate keywords.
4. Each worker returns bounded evidence notes with exact quotes from opened
   pages. A note may cover multiple facets. An unsupported quote or unknown
   evidence ID fails the query.
5. The coverage assessor reviews notes and gaps across documents. Code checks
   per-facet and overall distinct-document requirements. Missing coverage returns
   `insufficient_context`, or `budget_exhausted` when relevant worker limits were
   reached; no answer is synthesized. Workers can loop on gaps locally, but the
   coordinator does not automatically restart discovery after this final gate.
6. The writer receives compact verified-quote notes and authoritative source/page
   metadata, not all opened page bodies. Final quotes must occur in both the
   approved notes and the full opened pages. Final citations must also cover
   required documents for every facet.

Defaults (all have the `HRAG_` prefix):

| Setting | Default | Meaning |
|---|---:|---|
| `BROAD_MAX_DOCUMENTS` | 6 | Maximum selected document workers |
| `BROAD_CONCURRENCY` | 3 | Concurrent workers, each with separate session/state |
| `BROAD_MAX_FACETS` | 4 | Planner search facets |
| `BROAD_CANDIDATES_PER_QUERY` | 20 | Distinct-document candidates retained per discovery source/facet |
| `BROAD_MAX_TOOL_CALLS` | 60 | Total worker-tool allocation |
| `BROAD_MAX_SEARCHES` | 24 | Catalog + IQ discovery + document-scoped searches |
| `BROAD_MAX_PAGES` | 48 | Total unique opened-page allocation |
| `BROAD_MAX_CONTEXT_CHARS` | 150000 | Total unique retrieved/opened text charged to workers |
| `BROAD_TIMEOUT_SECONDS` | 300 | Whole query, including planning and synthesis |
| `BROAD_MAX_EVIDENCE_RECORDS` | 8 | Maximum notes returned by each worker |
| `BROAD_QUOTE_CHARS` | 1200 | Maximum length of each verified quote |

After discovery, remaining search allowance and other worker budgets are split
equally using integer division across selected documents. Unused quota is not
reassigned. This is intentionally conservative and deterministic: workers never
mutate one shared investigation state. Search budget must cover two calls per
maximum facet plus at least one search per maximum document; tool/page/character
budgets must permit each selected document to start. Invalid combinations fail
configuration validation.

Character limits count unique discovery/evidence text, **not actual cumulative
model input tokens**; repeated conversation context, navigation, model output and
the coordinator add usage. This is not a hard token or dollar spending cap.
The native MAF invocation limits remain in effect per run as well. Reduce
concurrency for quota pressure; increasing it can lower latency but does not
reduce the number of model calls or guarantee lower cost.

Service errors cancel sibling workers and propagate. They are never converted
into missing evidence. A global deadline cancels workers and returns an explicit
budget outcome with completed usage retained. An individual worker budget gap
can coexist with a valid answer only if the remaining verified evidence passes
all coverage/citation requirements; its gap remains in the result.

### Evaluate breadth and efficiency

Use the result's `broad.documents`, `broad.coverage` and `usage` together:

- Distinct discovered, selected, investigated and cited documents.
- Covered facets, documented omissions, and requirements not met.
- Opened pages not cited, verified quote characters and model-stage token totals.
- Latency and measured-token cost at a fixed answer-quality threshold.

The application cannot calculate relevant-document recall or factual correctness
without labelled reference cases. Evaluate a fixed collection of cross-report
questions with known supporting pages, contradictions and absent evidence;
compare focused/broad outputs against those references. Offline tests verify
isolation, limits, citations, coverage and SDK serialization, not real-corpus
retrieval quality or model reasoning accuracy. A broad answer is always a
comparison of **selected retrieved reports**, never proof of exhaustive coverage.

## Hybrid vector retrieval and migration

Configure one embedding endpoint/deployment/model/dimension combination for both
ingestion and query vectorization:

```dotenv
HRAG_EMBEDDING_ENDPOINT=https://YOUR-FOUNDRY.openai.azure.com
HRAG_EMBEDDING_DEPLOYMENT=text-embedding-3-large
HRAG_EMBEDDING_MODEL=text-embedding-3-large
HRAG_EMBEDDING_DIMENSIONS=3072
```

`text-embedding-3-small` is also supported, with at most 1536 dimensions. The
deployment must actually host the configured model. Do not repoint the same
deployment name to a different embedding model after indexing.

The ingester uses the official OpenAI Python SDK's `AsyncAzureOpenAI`, Azure API
`2024-10-21`, and a bearer token provider backed by `DefaultAzureCredential`.
Embedding requests contain up to 16 chunk texts; vectors are validated for
response alignment, dimensions and finite/nonzero values before upload.
Embedding/API errors propagate without falling back to text-only indexing.
The character-based chunk limit is not a token limit: if a multilingual or large
chunk exceeds the embedding deployment's input-token limit, lower
`HRAG_CHUNK_CHARS` (and keep overlap smaller), then retry. Text is never silently
truncated for embedding.

The Search index adds a non-retrievable `content_vector` field, HNSW/cosine
configuration, and Azure OpenAI query vectorizer. The knowledge source searches
both `content` and `content_vector`. IQ's semantic intent remains unchanged:
the service combines text and vector retrieval and applies semantic ranking.
Returned source data still contains only the original text/provenance fields.
`search_document` remains an in-document keyword locator, not a vector query.

For an existing text-only installation:

1. Install updated dependencies: `python -m pip install -e ".[dev]"`.
2. Configure the embedding settings above and both identities' RBAC.
   Ensure the Search service can reach the embedding endpoint through applicable
   firewalls/private networking, not just from your workstation.
3. Run `hrag provision` to add the vector field/profile/vectorizer and update the
   IQ knowledge source. This does not generate embeddings for existing records.
4. Run `hrag ingest` to reprocess/re-embed the corpus. This also reruns CU and
   incurs its costs; there is no embeddings-only backfill command.
5. Only regard migration as complete after all intended PDFs succeed.
   During migration, old chunks without vectors can still participate in text
   retrieval, so vector coverage is partial.

For uninterrupted use of the old corpus, set new `HRAG_INDEX_NAME`,
`HRAG_KNOWLEDGE_SOURCE_NAME` and `HRAG_KNOWLEDGE_BASE_NAME` values in a separate
ingestion environment, provision/re-ingest there, then switch query configuration.
Old objects and artifacts are never automatically deleted.

Provisioning refuses to change an existing vector field's model/deployment,
endpoint or dimensions; use new names and re-ingest instead of mixing embedding
spaces. CLI ingestion checks that index contract before analyzing any PDFs.
This read requires index-definition permissions in addition to document-write
permissions. The query service does not need those administrative permissions.

Offline tests verify SDK wire contracts, vector values/alignment, schema
configuration and regression behavior. Live hybrid retrieval was not run as part
of this enhancement. After provisioning, verify a paraphrased query retrieves
the expected page and inspect Search diagnostics/activity for vectorization.

`provision` operates on the names in configuration and updates existing objects.
Do not point this sample at an unrelated production index. Schema changes that
Search cannot apply in place should use a new index/source/base name and a
controlled migration; this tool never deletes and recreates an index silently.

Ingest files after putting them in Blob Storage. CU analysis runs once per ingestion, not
once per query. Re-ingestion produces a new extraction revision even for an
unchanged PDF, and removes older chunks only after new uploads succeed.

Ingestion logs timestamped progress at INFO by default: source download, CU
submission/wait and validated results, artifact publication, Search upload
batches, stale-chunk cleanup, and completion counts/timings. Page-upload progress
is reported every 25 pages and on the final page. CU may take several minutes;
its wait message is not a percentage-complete estimate or a periodic heartbeat.
Use `--verbose` before the subcommand for individual page-upload details and
the CU operation ID. Application ingestion progress logs contain names, IDs,
counts and timings, not PDF bytes, extracted text or raw analysis payloads.
Query JSON is written to stdout; progress/errors go to stderr. Keep output files,
logs, and any enabled framework traces private: they can contain document text.
The accelerator does not enable content-bearing telemetry exporters by default.

## Applying retrieval cleanup

New ingestion uses layout-aware filtering before chunk embedding. It removes
page-number/page-break markers and later duplicates of confidently labelled
running headers/footers, retaining the first occurrence and preserving footnotes,
unique footer values and unlabelled body repetition. Stored source Markdown is
not changed. See [chunking details](architecture.md#retrieval-chunking-versus-source-evidence).

`HRAG_CHUNK_CHARS` defaults to 2400, with `HRAG_CHUNK_OVERLAP=200`.
The size is a hard character cap; overlap can shrink at structural boundaries.
CU paragraph spans and Markdown headings guide splitting, while fitting
tables/figures/code blocks stay intact. Oversized structures produce a warning
and use bounded slices instead of silently exceeding the cap.

To update already indexed chunks, run:

```powershell
hrag ingest --blob "policies/claims.pdf"
# Or the entire corpus:
hrag ingest
```

This reruns CU and embeddings and publishes a new revision; it is not a free
query-time cleanup. No extra provisioning is necessary on an already hybrid
index. Validate that repeated boilerplate is reduced in Search `content`, while
full page Markdown still includes it. Check figures, meaningful disclaimers and
page links on representative PDFs before re-ingesting a large corpus.

## Markdown storage and migration

New ingestion writes schema-v2 JSON manifests plus UTF-8 Markdown into the
configured `HRAG_PAGES_CONTAINER` (default `document-pages`):

```text
<document-id>/<revision>.json
<document-id>/<revision>/document.md
<document-id>/<revision>/analysis.json
<document-id>/<revision>/figures.json
<document-id>/<revision>/pages/0001.md
<document-id>/<revision>/pages/0002.md
...
```

The JSON holds provenance, physical-page spans, canonical blob names, character
lengths, and SHA-256 hashes, not the page text itself. Markdown blobs use
`text/markdown; charset=utf-8`. The full-document file retains the original
Content Understanding Markdown, including generated figure descriptions/analysis,
structural markup and page comments.
Individual page files are derived from the service's physical-page spans.
`analysis.json` preserves the raw result (including figures, descriptions, source
geometry and summary fields) for audit, not for query-time downloads. The optional
manifest `extraction` block records provider, analyzer, API version, raw-result
blob path and SHA-256. Page/full Markdown hashes are verified on query reads;
the raw-result hash is available for separate audit verification.

`figures.json` is a dedicated, versioned export of detected figures:

- `chart`: CU's Chart.js JSON for supported graphs, otherwise null.
- `description`: CU-generated text for graphs/images, when returned.
- `mermaid`: CU's Mermaid representation for diagrams, otherwise null.
- `figure_id`, `kind`, `page_number`, `source_url` (including `#page=N`),
  `source_region`, and `markdown_span` locate each figure in the source.
- CU may omit `kind` or return null. Such figures are exported with `kind: null`,
  their description and provenance intact, plus a log/per-figure warning.
  Classification is not guessed; any unclassified structured content remains
  in `analysis.json`, not mislabelled as Chart.js or Mermaid. Consumers must
  accept a nullable `kind`. Explicitly classified charts/diagrams still undergo
  their existing content validation.
- Top-level document identity, revision, ETag, analyzer/API version and
  `generated: true` identify provenance. `markdown_span` uses Unicode code-point
  offsets into full `document.md`, not offsets into the individual page file.
- Missing descriptions generate both a log warning and a per-figure `warnings`
  entry. Unsupported plots retain whatever description CU returned; no numerical
  dataset is fabricated. Malformed/missing chart or Mermaid content is an error.

Every successful ingestion writes this artifact, including `figures: []` for a
document with no detected figures. It is uploaded before manifest publication.
`extraction.figures` in the manifest records its `blob` path and `sha256`.
Old manifests without it remain readable. Query-time tools still read Markdown,
not this sidecar; consumers may independently download and verify its hash.
No new model request is needed to export figures. Normal re-ingestion is
required for existing documents and still incurs CU/embedding costs.
Treat chart values and descriptions as generated interpretations, not verified
measurements, and never execute returned chart/diagram content as trusted code.

**Existing documents:** schema-v1 inline-page JSON remains readable and emits a
warning suggesting re-ingestion. It does not get silently converted to Markdown
at query time. Existing schema-v2 DI Markdown also remains readable; it will not
gain visual descriptions without re-analysis. Upgrade through normal ingestion
after installing dependencies and configuring CU:

```powershell
hrag ingest --blob "policies/claims.pdf"
# Or re-ingest the whole corpus:
hrag ingest
```

CU Markdown storage itself does not require a Search schema change. For the
hybrid enhancement, provision the vector-enabled index/source first as described
above. CU and embedding dependencies, endpoints, RBAC and mappings must be ready.
A new extraction revision is published;
old indexed chunks are removed only after the new upload succeeds. Re-ingestion
incurs Content Understanding and underlying model costs. Retain or clean up old artifact revisions
according to your retention policy.

Avoid lifecycle rules that independently expire page Markdown while retaining
its manifest or Search chunks. Missing/corrupt artifacts are errors, not an
invitation to repeat OCR or answer without evidence. Ingestion failures before
manifest publication can leave orphaned Markdown files; clean these up only
after checking active index revisions and ongoing investigations.

## Live smoke test (requires your Azure configuration)

1. Prepare a PDF with at least three pages. Put a rule on page 1 and an important
   exception on page 2; place an unrelated topic on page 3. Include a scanned PDF
   in a second test to exercise OCR.
2. Upload the PDFs to the source container. Run `provision` and `ingest`.
3. Confirm Search has chunks with `page_number`, `page_count`, `source_url`,
   `source_etag`, and `revision`, and that the knowledge source/base exist.
   Verify the derived container contains a schema-v2 manifest, full-document
   Markdown, and a Markdown blob for every physical page. Check headings and an
   HTML table against the source; include a Unicode character and a blank page.
4. Ask a question requiring the rule **and** exception. Inspect full opened
   evidence and citations, not only the answer. Verify page 2 was actually opened.
5. Ask a cross-document question. Confirm evidence contains both document IDs.
6. Ask an unsupported question. Expect `insufficient_context`, or a budget stop,
   never an invented answer.
7. Set `HRAG_MAX_PAGES=1` and ask a multi-page question. Verify the result refuses
   to synthesize if required pages cannot be read.
8. Replace a PDF without re-ingesting. Page expansion must fail on the ETag check.
   Re-ingest and retry.
9. Compare extracted text and visual interpretations against the source PDF,
   including physical page numbering. Exact quote checks verify Markdown, not
   whether generated words or numbers were printed in the PDF. Browser `#page`
   links may require an authenticated viewer.
10. Use Storage diagnostics to verify a fresh `open_pages` reads only the manifest
    and requested pages, not the full-document Markdown. A first whole-document
    search can download the full Markdown; repeated searches and page opens in
    the same investigation should reuse its cached pages.
11. In a disposable test revision, modify or remove a page Markdown blob.
    Page opening must fail its hash check or return a missing-blob error.
    Re-ingest to publish a new consistent revision.
12. Ingest a representative visual PDF. Compare figure descriptions, axes, units,
    legends, arrows, direction and depth with its source pages. Inspect both
    `analysis.json` and page Markdown; confirm visual terms enter indexed chunks.
    Ask a question about a figure and check `content_origin` in opened evidence.
    Generated interpretations must be qualified; missing quantitative evidence
    must produce a gap, not invented measurements. A screenshot alone is not a
    test of multi-page PDF extraction.

Run these domain-specific checks on model/dependency changes. Offline tests
cannot prove model sufficiency judgments, OCR accuracy, or retrieval recall.

## Costs and scale

Expect charges for Content Understanding extraction/analysis and its underlying
model calls, chunk/query embedding calls, Search/vector/semantic retrieval,
Storage operations, and MAF model calls. Vector indexing adds memory/storage cost.
Ingestion page limits are checked
after extraction, so Content Understanding can already have incurred cost for an
over-limit file. The byte cap is enforced before download/extraction.

For a P-page PDF, ingestion writes P page blobs, one full-document Markdown blob,
raw analysis JSON, figures JSON and one manifest: **P + 4 writes**, plus Search operations. Storage holds both
the full Markdown and the page slices, trading some duplication for efficient
page reads and efficient whole-document search.

The baseline processes documents sequentially. Page opening caches only requested
pages; a whole-document keyword search downloads the full Markdown and caches all
its pages for that question. This scan still has whole-document memory/network
cost, but no new OCR/model cost, and subsequent searches reuse the cache. Search
results do not automatically deliver all cached text to the model.

Apply workload-specific file/page limits, concurrency control, ingestion queues,
retries/dead-letter handling, and retention before increasing scale. Azure SDK
retry policies handle transient requests; the application does not mask
persistent failures.
