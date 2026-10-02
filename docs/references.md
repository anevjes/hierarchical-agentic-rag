# API decisions and official references

Checked against published SDKs and Microsoft documentation during implementation
(October 2026). The direct SDK versions are pinned in `pyproject.toml`.

- [Foundry IQ overview](https://learn.microsoft.com/azure/foundry/agents/concepts/what-is-foundry-iq)
- [Search-index knowledge sources](https://learn.microsoft.com/azure/search/agentic-knowledge-source-how-to-search-index)
- [Native Blob knowledge sources (alternative)](https://learn.microsoft.com/azure/search/agentic-knowledge-source-how-to-blob)
- [Create a knowledge base](https://learn.microsoft.com/azure/search/agentic-retrieval-how-to-create-knowledge-base)
- [Retrieve from a knowledge base](https://learn.microsoft.com/azure/search/agentic-retrieval-how-to-retrieve)
- [Search SDK](https://learn.microsoft.com/python/api/overview/azure/search-documents-readme)
- [Document Intelligence Python SDK](https://learn.microsoft.com/python/api/overview/azure/ai-documentintelligence-readme)
- [Document Intelligence Markdown elements](https://learn.microsoft.com/azure/ai-services/document-intelligence/concept/markdown-elements?view=doc-intel-4.0.0)
- [Document Intelligence layout and page spans](https://learn.microsoft.com/azure/ai-services/document-intelligence/prebuilt/layout?view=doc-intel-4.0.0)
- [MAF Foundry model provider](https://learn.microsoft.com/agent-framework/integrations/by-component/model-providers/microsoft-foundry)
- [MAF function tools and invocation limits](https://learn.microsoft.com/agent-framework/agents/tools/function-tools)
- [MAF structured outputs](https://learn.microsoft.com/agent-framework/agents/structured-outputs)
- [Azure Identity](https://learn.microsoft.com/python/api/overview/azure/identity-readme)

Important version distinctions:

- Search `12.0.0` and API `2026-04-01`: knowledge bases use semantic **intents**
  for minimal extractive retrieval. We do not send preview-only `messages`,
  LLM planning, or answer-synthesis settings to this stable contract.
- MAF `agent-framework-core==1.19.0` and
  `agent-framework-foundry==1.13.1`: `Agent` with
  `agent_framework.foundry.FoundryChatClient`. Older
  `AzureOpenAIChatClient` examples are not the API used here.
- The investigator uses the framework's function-invocation loop, serial tool
  execution, and structured `Assessment` output. It is not a hand-written LLM
  function-call parser.
- Document Intelligence `1.0.2` / API `2024-11-30` accepts
  `output_content_format="markdown"` and `string_index_type="unicodeCodePoint"`.
  Physical pages use `AnalyzeResult.pages[*].spans` over its Markdown `content`;
  HTML tables and page-break comments are retained, not parsed as pagination.
- Derived storage schema v2 uses a JSON manifest, per-page Markdown, and
  full-document Markdown, with canonical paths and SHA-256 integrity checks.
  The reader continues to support schema-v1 inline-page JSON.
- The SDK package versions and wire shapes are validated offline, while live
  service behavior still requires the smoke tests in [setup](setup.md).
