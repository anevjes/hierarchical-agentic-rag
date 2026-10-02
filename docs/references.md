# API decisions and official references

Checked against published SDKs and Microsoft documentation during implementation
(October 2026). The direct SDK versions are pinned in `pyproject.toml`.

- [Foundry IQ overview](https://learn.microsoft.com/azure/foundry/agents/concepts/what-is-foundry-iq)
- [Search-index knowledge sources](https://learn.microsoft.com/azure/search/agentic-knowledge-source-how-to-search-index)
- [Native Blob knowledge sources (alternative)](https://learn.microsoft.com/azure/search/agentic-knowledge-source-how-to-blob)
- [Create a knowledge base](https://learn.microsoft.com/azure/search/agentic-retrieval-how-to-create-knowledge-base)
- [Retrieve from a knowledge base](https://learn.microsoft.com/azure/search/agentic-retrieval-how-to-retrieve)
- [Search SDK](https://learn.microsoft.com/python/api/overview/azure/search-documents-readme)
- [Index criteria and hybrid query execution for agentic retrieval](https://learn.microsoft.com/azure/search/agentic-retrieval-how-to-create-index)
- [Azure OpenAI query vectorizer and managed identity](https://learn.microsoft.com/azure/search/vector-search-vectorizer-azure-open-ai)
- [Official OpenAI Python SDK, including Azure clients](https://github.com/openai/openai-python)
- [Content Understanding Python SDK](https://learn.microsoft.com/python/api/overview/azure/ai-contentunderstanding-readme)
- [Content Understanding prebuilt analyzers](https://learn.microsoft.com/azure/ai-services/content-understanding/concepts/prebuilt-analyzers)
- [Content Understanding Markdown and generated figure descriptions](https://learn.microsoft.com/azure/ai-services/content-understanding/document/markdown)
- [Content Understanding elements and physical-page spans](https://learn.microsoft.com/azure/ai-services/content-understanding/document/elements)
- [MAF Foundry model provider](https://learn.microsoft.com/agent-framework/integrations/by-component/model-providers/microsoft-foundry)
- [MAF function tools and invocation limits](https://learn.microsoft.com/agent-framework/agents/tools/function-tools)
- [MAF structured outputs](https://learn.microsoft.com/agent-framework/agents/structured-outputs)
- [Azure Identity](https://learn.microsoft.com/python/api/overview/azure/identity-readme)

Important version distinctions:

- Search `12.0.0` and API `2026-04-01`: knowledge bases use semantic **intents**
  for minimal extractive retrieval. We do not send preview-only `messages`,
  LLM planning, or answer-synthesis settings to this stable contract.
- OpenAI Python SDK `3.19.2` / Azure embedding API `2024-10-21` generates chunk
  vectors with `AsyncAzureOpenAI` and an async Azure Identity bearer token
  provider. The index vectorizer uses the same deployment/model/dimensions.
  Both text and vector fields are selected in the IQ knowledge source; vectors
  are excluded from source-data fields. Hybrid service behavior follows the
  documented index criteria; this enhancement was validated offline only.
- MAF `agent-framework-core==1.19.0` and
  `agent-framework-foundry==1.13.1`: `Agent` with
  `agent_framework.foundry.FoundryChatClient`. Older
  `AzureOpenAIChatClient` examples are not the API used here.
- The investigator uses the framework's function-invocation loop, serial tool
  execution, and structured `Assessment` output. It is not a hand-written LLM
  function-call parser.
- Content Understanding `1.1.0` / API `2025-11-01` uses
  `begin_analyze(inputs=[AnalysisInput(data=pdf_bytes, mime_type="application/pdf")])`.
  Its Python SDK automatically requests `stringEncoding=codePoint`.
  Physical pages use `AnalysisResult.contents[0].pages[*].spans` over that
  content item's `markdown`; HTML tables and page comments are retained.
  `prebuilt-documentSearch` enables both `enableFigureDescription` and
  `enableFigureAnalysis`. The returned Markdown includes those generated
  representations, so they enter our existing page-bounded chunker.
- The live analyzer definition and synthetic-PDF call verified request-scoped
  `model_deployments` mappings for `prebuilt-analyzer-completion-mini` and
  `prebuilt-analyzer-embedding`. Consult your actual analyzer definition rather
  than assuming older documentation's model keys match the live service.
- Derived storage schema v2 uses a JSON manifest, per-page Markdown, and
  full-document Markdown, with canonical paths and SHA-256 integrity checks.
  CU adds raw `analysis.json` and optional manifest extraction provenance.
  The reader continues to support schema-v1 inline-page JSON and older DI v2 manifests.
- The SDK package versions and wire shapes are validated offline, while live
  service behavior still requires the smoke tests in [setup](setup.md).
