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
or service-generated cross-page chunks. No embedding field is added to Search.

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
| Query knowledge base | Search Index Data Reader on Search |
| Read original PDFs | Storage Blob Data Reader on source container |
| Write extracted pages / create derived container | Storage Blob Data Contributor on derived container (account scope if it must create the container) |
| Query extracted pages | Storage Blob Data Reader on derived container |
| Run Content Understanding | Cognitive Services User on Foundry resource |
| Call Foundry deployed model | Azure AI User on Foundry project (and any model inference role required by your resource configuration) |

Search does not call Blob, Content Understanding, or a generative model in this design.
The Python application does. Therefore there is no Search managed-identity Blob
indexer role or Search-to-model synthesis role to configure for this baseline.

Enable private endpoint connectivity/DNS or permitted public network access for
each service. RBAC alone does not bypass firewalls. Run the application from a
network with access to all endpoints.

## Commands

```powershell
hrag provision
hrag ingest --blob "policies/claims.pdf"
hrag ingest --prefix "policies/"
hrag ask "Which exclusions affect the emergency coverage?"
```

`provision` operates on the names in configuration and updates existing objects.
Do not point this sample at an unrelated production index. Schema changes that
Search cannot apply in place should use a new index/source/base name and a
controlled migration; this tool never deletes and recreates an index silently.

Ingest files after putting them in Blob Storage. CU analysis runs once per ingestion, not
once per query. Re-ingestion produces a new extraction revision even for an
unchanged PDF, and removes older chunks only after new uploads succeed.

Use `--verbose` before the subcommand for application-level progress logs.
Query JSON is written to stdout; progress/errors go to stderr. Keep output files,
logs, and any enabled framework traces private: they can contain document text.
The accelerator does not enable content-bearing telemetry exporters by default.

## Markdown storage and migration

New ingestion writes schema-v2 JSON manifests plus UTF-8 Markdown into the
configured `HRAG_PAGES_CONTAINER` (default `document-pages`):

```text
<document-id>/<revision>.json
<document-id>/<revision>/document.md
<document-id>/<revision>/analysis.json
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

No Search schema changes or IQ reprovisioning are required. CU dependencies,
endpoint, RBAC and model mappings must be ready first. A new extraction revision is published;
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
model calls, Search/semantic retrieval, Storage operations, and MAF model calls.
Ingestion page limits are checked
after extraction, so Content Understanding can already have incurred cost for an
over-limit file. The byte cap is enforced before download/extraction.

For a P-page PDF, ingestion writes P page blobs, one full-document Markdown blob,
one raw analysis JSON and one manifest: **P + 3 writes**, plus Search operations. Storage holds both
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
