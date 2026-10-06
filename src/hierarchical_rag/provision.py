import logging

from azure.core.exceptions import ResourceNotFoundError
from azure.search.documents.indexes.aio import SearchIndexClient
from azure.search.documents.indexes.models import (
    AzureOpenAIVectorizer,
    AzureOpenAIVectorizerParameters,
    HnswAlgorithmConfiguration,
    HnswParameters,
    KnowledgeBase,
    KnowledgeSourceReference,
    SearchableField,
    SearchField,
    SearchIndex,
    SearchIndexFieldReference,
    SearchIndexKnowledgeSource,
    SearchIndexKnowledgeSourceParameters,
    SemanticConfiguration,
    SemanticField,
    SemanticPrioritizedFields,
    SemanticSearch,
    SimpleField,
    VectorSearch,
    VectorSearchProfile,
)

from .catalog import catalog_index, validate_catalog_index
from .config import Settings
from .embeddings import VECTOR_FIELD
from .models import Chunk

logger = logging.getLogger(__name__)
SEARCH_API_VERSION = "2026-04-01"
SEMANTIC_CONFIGURATION = "page-content"
VECTOR_PROFILE = "page-content-vector"
VECTORIZER = "page-content-openai"


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
            SearchField(
                name=VECTOR_FIELD,
                type="Collection(Edm.Single)",
                searchable=True,
                retrievable=False,
                stored=False,
                vector_search_dimensions=settings.embedding_dimensions,
                vector_search_profile_name=VECTOR_PROFILE,
            ),
        ],
        vector_search=VectorSearch(
            algorithms=[
                HnswAlgorithmConfiguration(
                    name="page-content-hnsw",
                    parameters=HnswParameters(metric="cosine"),
                )
            ],
            profiles=[
                VectorSearchProfile(
                    name=VECTOR_PROFILE,
                    algorithm_configuration_name="page-content-hnsw",
                    vectorizer_name=VECTORIZER,
                )
            ],
            vectorizers=[
                AzureOpenAIVectorizer(
                    vectorizer_name=VECTORIZER,
                    parameters=AzureOpenAIVectorizerParameters(
                        resource_url=settings.embedding_endpoint,
                        deployment_name=settings.embedding_deployment,
                        model_name=settings.embedding_model,
                    ),
                )
            ],
        ),
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
        description="Hybrid PDF chunk retrieval with physical pages and versioned Blob provenance.",
        search_index_parameters=SearchIndexKnowledgeSourceParameters(
            search_index_name=settings.index_name,
            semantic_configuration_name=SEMANTIC_CONFIGURATION,
            search_fields=[
                SearchIndexFieldReference(name="content"),
                SearchIndexFieldReference(name=VECTOR_FIELD),
            ],
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


def validate_embedding_index(settings: Settings, index: SearchIndex) -> None:
    vector = next((field for field in index.fields if field.name == VECTOR_FIELD), None)
    if vector is None:
        raise ValueError("Index has no content_vector field. Run hrag provision before ingestion.")
    vectorizers = index.vector_search.vectorizers if index.vector_search else []
    profiles = index.vector_search.profiles if index.vector_search else []
    profile = next((item for item in profiles or [] if item.name == VECTOR_PROFILE), None)
    configured = next(
        (item for item in vectorizers or [] if item.vectorizer_name == VECTORIZER), None
    )
    params = configured.parameters if isinstance(configured, AzureOpenAIVectorizer) else None
    if (
        vector.type != "Collection(Edm.Single)"
        or not vector.searchable
        or vector.vector_search_dimensions != settings.embedding_dimensions
        or vector.vector_search_profile_name != VECTOR_PROFILE
        or profile is None
        or profile.vectorizer_name != VECTORIZER
        or params is None
        or (params.resource_url or "").rstrip("/") != settings.embedding_endpoint
        or params.deployment_name != settings.embedding_deployment
        or params.model_name != settings.embedding_model
    ):
        raise ValueError(
            "Existing index has incompatible embedding settings. Use new index, "
            "knowledge-source and knowledge-base names, then re-ingest; do not mix models."
        )


async def provision(
    settings: Settings, client: SearchIndexClient, *, include_catalog: bool = False
) -> None:
    try:
        existing = await client.get_index(settings.index_name)
    except ResourceNotFoundError:
        existing = None
    if existing is not None:
        if any(field.name == VECTOR_FIELD for field in existing.fields):
            validate_embedding_index(settings, existing)
        else:
            logger.warning(
                "Adding vector search to %s; existing chunks have no vectors until re-ingested",
                settings.index_name,
            )
    await client.create_or_update_index(index_definition(settings))
    await client.create_or_update_knowledge_source(knowledge_source_definition(settings))
    await client.create_or_update_knowledge_base(knowledge_base_definition(settings))
    if include_catalog:
        try:
            existing_catalog = await client.get_index(settings.catalog_index_name)
        except ResourceNotFoundError:
            existing_catalog = None
        if existing_catalog is not None:
            validate_catalog_index(existing_catalog)
        await client.create_or_update_index(catalog_index(settings.catalog_index_name))
