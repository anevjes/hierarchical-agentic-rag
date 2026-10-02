from azure.search.documents.indexes.aio import SearchIndexClient
from azure.search.documents.indexes.models import (
    KnowledgeBase,
    KnowledgeSourceReference,
    SearchableField,
    SearchIndex,
    SearchIndexFieldReference,
    SearchIndexKnowledgeSource,
    SearchIndexKnowledgeSourceParameters,
    SemanticConfiguration,
    SemanticField,
    SemanticPrioritizedFields,
    SemanticSearch,
    SimpleField,
)

from .config import Settings
from .models import Chunk

SEARCH_API_VERSION = "2026-04-01"
SEMANTIC_CONFIGURATION = "page-content"


def index_definition(settings: Settings) -> SearchIndex:
    return SearchIndex(
        name=settings.index_name,
        fields=[
            SimpleField(name="id", type="Edm.String", key=True),
            SimpleField(name="document_id", type="Edm.String", filterable=True),
            SimpleField(name="revision", type="Edm.String", filterable=True),
            SimpleField(name="blob_name", type="Edm.String"),
            SimpleField(name="source_url", type="Edm.String"),
            SimpleField(name="source_etag", type="Edm.String"),
            SearchableField(name="title", type="Edm.String"),
            SimpleField(name="page_number", type="Edm.Int32", filterable=True),
            SimpleField(name="page_count", type="Edm.Int32"),
            SearchableField(name="content", type="Edm.String"),
        ],
        semantic_search=SemanticSearch(
            default_configuration_name=SEMANTIC_CONFIGURATION,
            configurations=[
                SemanticConfiguration(
                    name=SEMANTIC_CONFIGURATION,
                    prioritized_fields=SemanticPrioritizedFields(
                        title_field=SemanticField(field_name="title"),
                        content_fields=[SemanticField(field_name="content")],
                    ),
                ),
            ],
        ),
    )


def knowledge_source_definition(settings: Settings) -> SearchIndexKnowledgeSource:
    return SearchIndexKnowledgeSource(
        name=settings.knowledge_source_name,
        description="PDF chunks with physical page numbers and versioned Blob provenance.",
        search_index_parameters=SearchIndexKnowledgeSourceParameters(
            search_index_name=settings.index_name,
            semantic_configuration_name=SEMANTIC_CONFIGURATION,
            search_fields=[SearchIndexFieldReference(name="content")],
            source_data_fields=[
                SearchIndexFieldReference(name=name) for name in Chunk.model_fields
            ],
        ),
    )


def knowledge_base_definition(settings: Settings) -> KnowledgeBase:
    # The stable API is extractive-only: no synthesis model or planning model is configured.
    return KnowledgeBase(
        name=settings.knowledge_base_name,
        description="Extractive PDF evidence for client-side, page-expanding investigation.",
        knowledge_sources=[KnowledgeSourceReference(name=settings.knowledge_source_name)],
    )


async def provision(settings: Settings, client: SearchIndexClient) -> None:
    await client.create_or_update_index(index_definition(settings))
    await client.create_or_update_knowledge_source(knowledge_source_definition(settings))
    await client.create_or_update_knowledge_base(knowledge_base_definition(settings))
