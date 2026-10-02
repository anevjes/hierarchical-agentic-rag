# Hierarchical agentic RAG accelerator

A PDF-first Python accelerator using **Microsoft Agent Framework (MAF)**,
**Foundry IQ / Azure AI Search knowledge bases**, Azure Blob Storage, and Azure
Content Understanding. All Azure clients use `DefaultAzureCredential`.

**Retrieve chunks to find documents. Open the actual referenced pages to gather
evidence. Investigate missing context before allowing answer generation.**

This repository is a local, code-owned agent application, not a deployment of a
Foundry-hosted agent. No Azure resources are created simply by installing or testing it.

## What it does

1. Reads PDFs from a private Blob container.
2. Uses Content Understanding `prebuilt-documentSearch` for PDF OCR/layout plus
   generated figure descriptions and chart/diagram analysis in Markdown.
3. Persists per-page Markdown, the full-document Markdown, and a versioned JSON
   manifest in a separate private Blob container. Page opens download only the
   requested pages; document searches load the full Markdown once per investigation.
4. Creates page-bounded chunks in an Azure AI Search index, preserving the PDF URL,
   physical page number, source ETag, and extraction revision.
5. Registers that index as an IQ **search-index knowledge source**, then creates an
   IQ **knowledge base** referencing it.
6. Retrieves extractive chunks and source metadata through the knowledge-base API.
7. Runs a MAF agent with `open_pages`, `search_document`, and
   `search_knowledge_base` tools. It opens hit pages, expands adjacent pages,
   locates non-adjacent passages, and searches other documents when needed.
8. Separates the investigator's structured sufficiency assessment from a final,
   tool-free answer writer. Only approved, opened pages reach the writer. Citation
   IDs and exact extracted-Markdown quotes are validated in code. Visual
   interpretations are flagged as potentially generated, not verbatim source facts.

The stable Search API `2026-04-01` supports minimal extractive retrieval, without
knowledge-base LLM query planning or answer synthesis. MAF does the iterative
planning here. Retrieval uses **keyword search plus semantic ranking**, not vector
search. Search does not use embeddings; the Content Understanding analyzer has
its own completion/embedding deployment requirements (see setup).

### Why not the automatic Blob knowledge source?

Foundry IQ supports a native Blob knowledge source that creates its own indexer,
skillset, and index. This accelerator deliberately uses the documented
**existing-index knowledge-source** path to own a strict chunk-to-physical-page
contract and full-page extraction cache. Blob remains the original source. We do not
pretend that Text Split "pages" are physical PDF pages, or that every native
ingestion configuration guarantees the metadata this tool needs.

### Persisted document cache

Each ingestion writes an immutable extraction revision:

```text
document-pages/
  <document-id>/
    <revision>.json              # metadata, physical-page spans, paths, SHA-256 hashes
    <revision>/
      document.md               # full Content Understanding Markdown
      analysis.json             # raw CU result, figures, geometry and provenance
      pages/
        0001.md                 # physical PDF page 1
        0002.md                 # physical PDF page 2
```

`open_pages` reads the manifest and only missing page blobs, then caches them.
`search_document` reads the full Markdown if any pages are not yet cached and
uses the manifest's Unicode spans to recover physical pages. Subsequent page
opens reuse that cache. Neither phase downloads or re-extracts the original PDF;
the source ETag is checked when loading its manifest.

Existing inline-page JSON and Document Intelligence Markdown revisions remain
readable. **Re-run ingestion to add Content Understanding visual analysis** after
updating dependencies and CU configuration; no Search schema change is needed.
See [storage and migration](docs/setup.md#markdown-storage-and-migration).

### Visual-rich PDFs

Figure descriptions make labels, legends and depicted relationships searchable,
including in scientific cross-sections and maps. This is not guaranteed scientific
chart digitisation: colour-scale values, depth estimates and geological conclusions
need validation against the PDF. The agent reads cached enriched Markdown, not
rendered pixels, and is instructed to report missing/ambiguous visual context.
Raw figure assets are not downloaded; their descriptions/analysis are retained.

## Quick start (PowerShell)

Requires Python 3.11+; the CU migration was locally validated with Python 3.14.

The environment-creation command below is for a **new** virtual environment.
Do not run it over an existing environment with a different Python version:
compiled packages can remain tied to the old interpreter. See
[environment troubleshooting](docs/setup.md#python-environment-troubleshooting).

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
Copy-Item .env.example .env
# Fill in endpoints and your model deployment in .env.
az login
hrag provision
hrag ingest --blob "manuals/product.pdf"
hrag ask "What conditions apply to the extended warranty?"
```

Use `hrag ingest` without `--blob` to process all PDFs in the source container;
`--prefix "manuals/"` narrows the scan. Non-PDF blobs are reported and skipped.
Put PDFs into the source container with your normal Azure upload tooling first.

`provision` creates/updates the Search index, knowledge source, and knowledge base.
It does **not** provision an Azure subscription, Search service, Storage account,
Foundry project/model, or Content Understanding configuration. It also checks that the
source container exists and creates the derived-page container if absent.
Use dedicated names: `provision` updates objects with those names.

Query output is structured JSON with status, sufficiency rationale/gaps, answer,
citations, retrieved chunks, full opened evidence, document links, and usage counters.
`insufficient_context` and `budget_exhausted` never contain a synthesized answer.
Service, authentication, malformed-reference, and citation-validation errors fail
explicitly with a nonzero exit code.

## Azure prerequisites

See [setup and operations](docs/setup.md) for resources, RBAC, model capabilities,
private links, live verification, costs, and ingestion lifecycle.

## Architecture and behavior

- [Design and limitations](docs/architecture.md)
- [Editable architecture diagram](docs/architecture.excalidraw)
- [Editable investigation-loop diagram](docs/investigation-loop.excalidraw)
- [Verified API references](docs/references.md)

Open the diagrams in <https://aka.ms/excalidraw> using **Open**.

## Validate locally

```powershell
python -m pytest
python -m ruff check .
python -m mypy
python -m build --wheel
```

Tests use in-memory evidence and mocked Azure transport responses. They check
actual SDK serialization and MAF tool invocation, but do not establish that your
Azure region, permissions, model, or documents work end to end. Follow the live
smoke test in the setup guide after configuration.

In VS Code, press **F5** and select **Investigate PDFs (configured Azure)** to
debug a question through the CLI after setup. This is a console debugger
configuration, not an Agent Inspector HTTP endpoint. A hosted-agent wrapper and
deployment can be added separately.

## Scope

This is a single-trust-boundary accelerator, not a multi-tenant service. It does
not implement per-user document ACL trimming, event-driven ingestion, a web UI,
or infrastructure deployment. Do not expose its application-identity credentials
as a public search API without adding authorization to **both** retrieval and
page expansion.
