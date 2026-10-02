# Design: retrieve, inspect, expand, assess

## Data plane

`Blob PDF -> Content Understanding visual-enriched Markdown -> page/full Markdown blobs + JSON
manifest + embedded Search chunks -> hybrid knowledge source -> knowledge base`

The knowledge base and knowledge source are real Azure AI Search data-plane
objects underlying Foundry IQ. They are not local aliases or a separate,
invented "Foundry IQ index" API. The physical index lives in Search. A Foundry
portal project must be connected to that Search service to discover/use its
knowledge bases through the portal; creating the data-plane objects does not
automatically create that project connection.

Each extraction gets a new revision. The original Blob ETag is checked before
download and again before publishing. Each chunk stays inside one physical page.
Ingestion uses the CU async SDK with `prebuilt-documentSearch`, inline PDF bytes,
request-scoped model mappings and API `2025-11-01`. The SDK automatically requests
`stringEncoding=codePoint`. The result must report this encoding and supply
Markdown; otherwise ingestion fails explicitly. Each physical page's Markdown
comes from `DocumentContent.pages[*].spans` over `DocumentContent.markdown`, not splitting on
headings, printed page numbers, or literal `<!-- PageBreak -->` comments.
Unicode code-point offsets match Python string indexing, including emoji.
Out-of-range, overlapping, or out-of-order spans are rejected. A single
unsegmented document with contiguous pages is required. Service warnings and
figures without spans contained in a physical page are errors, not partial success.

### Hybrid retrieval

The ingester embeds each exact chunk's enriched Markdown using Azure OpenAI,
then pushes its text/provenance plus `content_vector`. It batches embedding
requests (16 inputs) within Search upload batches (100 records) and validates
response indices, dimensions, finite values and nonzero vectors.
Embedding failures stop ingestion; no empty-vector/text-only fallback is used.
Previously indexed revisions are removed only after all new uploads succeed.

Search uses a non-retrievable, non-stored vector field with HNSW/cosine and a
query vectorizer configured for the same endpoint, deployment, model and
dimensions as ingestion. The index retains searchable text and its semantic
configuration. The IQ knowledge source explicitly searches both text and vector
fields; a natural-language semantic intent drives parallel text/vector retrieval
and semantic ranking. The application does not send unsupported vector-query
parameters to the stable IQ retrieve API.

The application embeds chunks with `DefaultAzureCredential`; Search embeds
queries using its own system-assigned managed identity. Both need access to the
embedding deployment. Embeddings never enter `Chunk`, source-data references,
Markdown artifacts, the evidence ledger, or the writer's prompt. Only indexed
upload records have the vector field.

Ingestion preflights the index embedding contract; provisioning rejects changes
that would mix embedding models/spaces. A text-only index can gain the vector
field, but its old documents remain unvectorized until re-ingestion. See the
[migration procedure](setup.md#hybrid-vector-retrieval-and-migration).

### Versioned Markdown artifacts

All artifacts live in the private derived container:

| Artifact | Content |
|---|---|
| `<document-id>/<revision>.json` | Schema-v2 manifest: source identity/ETag, full-Markdown path/hash/length, and each physical page's path/hash/length/spans |
| `<document-id>/<revision>/document.md` | Complete, unmodified Content Understanding Markdown |
| `<document-id>/<revision>/analysis.json` | Raw CU result, including figures, source geometry and fields; audit only |
| `<document-id>/<revision>/pages/0001.md` | Page 1's Markdown; analogous files for every page, including blank pages |

The manifest contains **no inline page text**. Its optional `extraction` block
identifies CU, the analyzer/API version and raw-analysis path/hash. Older DI
manifests do not have this block and remain valid. Markdown and raw analysis are uploaded first,
then the manifest, then the Search chunks. A failed Markdown upload cannot publish
a new manifest/index revision. Incomplete uploads can leave orphaned blobs and
require operational cleanup; this is not a cross-service transaction.

SHA-256 hashes and character lengths verify that retrieved Markdown matches its
manifest. Page paths must match the document's canonical revision paths; a
manifest cannot redirect the reader to another blob. Hashes detect accidental
artifact corruption/mismatch, not malicious modification by an identity allowed
to rewrite both manifest and Markdown.

### Reading and searching without re-extraction

The first document access checks the original PDF's ETag and loads its manifest,
validating identity and physical pagination. Then:

- `open_pages` downloads **only requested uncached page Markdown blobs**. It
  verifies their hashes and any retrieved chunks on those pages before accepting
  evidence. It does not download the full Markdown for an individual page read.
- `search_document` downloads the **full Markdown once** if some pages are not
  cached. It slices the exact stored spans and verifies each page's hash. All page
  text is then cached, so repeated searches and subsequent opens need no further
  content downloads. If all pages were already cached, it skips this download.
- Reading text internally for keyword search does **not** mark those pages as
  opened grounding evidence. The model receives page locations and must still call
  `open_pages` for the pages it wants to use in its sufficiency assessment.
- A missing or corrupt blob fails explicitly; the reader does not silently fall
  back to PDF OCR, an empty page, or a different revision.

This is stored OCR/layout plus generated visual-analysis Markdown, not rendered
PDF pixels, and neither tool re-extracts the PDF. MAF sees headings, paragraphs,
tables, figure descriptions, chart data and Mermaid diagrams as data.
Content Understanding can emit **HTML tables inside Markdown**. Page-local slices
can be fragments of structures spanning pages, rather than independently
renderable Markdown/HTML. The full-document artifact preserves the complete
service output; adjacent page expansion provides surrounding evidence.
Image/figure references may be present, but this accelerator does not download
their image assets or follow embedded links.

CU evidence is conservatively tagged
`content_origin=mixed_extraction_and_generated_visuals`, even when a page has no
detected figure. This is page-level provenance, not a claim that every generated
token can be distinguished from OCR. Both agents are instructed to treat visual
descriptions/derived chart data as interpretations, to check axes/units/legends,
and to leave gaps when essential visual context is absent. Exact quote validation
checks the stored Markdown only; it cannot establish that generated descriptions
or values appeared in the original PDF. Raw analysis is retained for audit but
never fetched during page expansion.

Caches are scoped to one investigation, not shared across callers. A source can
change after its ETag check; the evidence records a versioned snapshot, not a
transactional lock over the original Blob.

### Compatibility

Schema-v1 inline-page JSON is still supported, with an explicit migration warning.
It necessarily loads all page text on first access and needs no additional
Markdown reads. New ingestion always writes schema v2. Re-ingesting an existing
PDF produces the CU Markdown/analysis artifacts and replaces its old indexed chunks, without
changing the Search schema. Old revisions are not rewritten in place.

PDF links use `#page=N`, where N is the **1-based physical PDF page**, not the
printed page label. They are private URLs, not SAS tokens. Browser access depends
on the caller's storage permissions and PDF viewer; an authenticated document
viewer may be needed. An overwritten URL no longer displays the old revision.
For durable historical links, enable Blob versioning and extend ingestion and
link generation to persist version IDs.

## Investigation

Each question gets fresh mutable state; never share an `Investigation` between
users or concurrent requests.

1. Mandatory IQ retrieval seeds the investigation. Source data is parsed into
   typed chunks. Unexpected reference kinds or missing provenance are errors.
   Partial HTTP 206 results and failed retrieval activities are rejected rather
   than being treated as complete grounding evidence.
2. A configurable number of returned references is selected. The limit and total
   count are explicit in the tool response. Selected hit pages become required.
3. The MAF investigator uses real function tools:
   - `open_pages(document_id, start_page, end_page)`: complete page Markdown
     (plain text for legacy revisions).
   - `search_document(document_id, query)`: keyword-based page locator over the
     full extracted PDF, including pages that were not top Search hits.
   - `search_knowledge_base(query)`: cross-document exploration or reformulation.
4. MAF's native function-invocation loop executes calls serially. Application
   counters enforce strict limits before work, in addition to framework limits.
5. The investigator returns a structured assessment, not an answer. If gaps
   remain and it made progress, the session continues. No-progress and exhausted
   budgets stop investigation.
6. A code gate requires retrieved hits, all selected hit pages opened, known
   evidence IDs, and no declared gaps. Only then can a separate, tool-free writer
   synthesize the answer from approved page evidence.
7. Every citation must name an approved opened page and quote an exact substring.
   This catches fabricated IDs/quotes; it does **not** mathematically establish
   that every claim follows from its citations. Sufficiency and entailment remain
   model judgments and require domain evaluation.

Chunks, PDF content, titles, and tool results are untrusted **data**, never agent
instructions. The model supplies document IDs, not URLs; the backend always uses
configured Storage clients. A model cannot ask the page reader to fetch arbitrary
Internet URLs.

## Budgets

The defaults allow 20 tool calls (including seed retrieval), four knowledge-base
searches, eight hits per search, 24 unique opened pages, 100,000 evidence-text
characters, and 180 seconds for the complete query.

The character counter includes newly delivered chunks and opened pages. It is
not a tokenizer or a limit on total billed tokens: prompts, JSON metadata,
reasoning, repeated model inputs, and output consume extra tokens. Choose budgets
that fit your model's context window. Whole pages are never silently truncated.
An oversized page or range produces an explicit budget stop with no answer.
Duplicate queries/pages do not return their text again or count twice as evidence.

## Known limitations / extension points

- PDFs only. Word, HTML, and slide navigation need separate location contracts.
- CU can describe figures and analyze supported charts/diagrams, but arbitrary
  scientific cross-sections, maps and colour scales are not guaranteed to yield
  accurate quantitative data. Human/domain review or a separate rendered-page
  vision tool may still be required. The current tools return text, not pixels.
- No automatic access-control propagation. Application RBAC is not end-user ACL
  trimming. Add caller-scoped filtering before returning hits **and** enforce the
  same policy when reading source/page blobs.
- Hybrid keyword/vector retrieval with semantic ranking still requires domain
  evaluation for recall and ranking quality. In-document `search_document` is
  intentionally a keyword locator over cached pages.
- Native Blob knowledge sources are an alternative, not an additional duplicate
  ingestion path. Adopt one only after verifying their page metadata contract.
- Ingestion is explicit, sequential, and not transactional across Blob and Search.
  Run one writer per source document. New chunks upload before old revisions are
  removed; queries during an update can fail closed on mixed/stale revisions.
  Rerun ingestion after partial failures. An operator can remove stale revisions
  after verifying a successful publish.
- Deleted original PDFs fail page opening; they are not automatically purged
  from Search. Add reconciliation/deletion handling before production use.
- Old manifests and Markdown blobs are retained for audit; configure a
  retention/cleanup job that preserves every artifact of revisions still
  referenced by the index and ongoing queries. Remove orphaned failed-upload
  revisions only after confirming that no index entries refer to them.
- A source may change after an ETag check. The returned evidence is a versioned
  snapshot, not a promise about the latest file at answer time.
- No web server/hosted runtime is included. The Python API and CLI are intended
  for reuse behind project-specific identity, authorization, and hosting layers.
