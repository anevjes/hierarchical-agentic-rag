import json
from typing import Protocol

from azure.search.documents.aio import SearchClient
from azure.search.documents.knowledgebases.aio import KnowledgeBaseRetrievalClient
from azure.search.documents.models import VectorizableTextQuery
from azure.storage.blob.aio import ContainerClient

from .catalog import CATALOG_SEMANTIC, CatalogDocument, publish_catalog
from .config import Settings
from .embeddings import VECTOR_FIELD
from .ingestion import document_id, odata_literal, source_url
from .models import Chunk, Document, DocumentIdentity, Page, StoredDocument
from .provision import SEMANTIC_CONFIGURATION
from .retrieval import AzureEvidenceBackend, EvidenceBackend, reject_partial_retrieval
from .usage import UsageTracker


class UnindexedDocumentError(ValueError):
    """The source PDF has no chunks in the configured index."""


class BroadBackend(EvidenceBackend, Protocol):
    async def discover_documents(self, query: str, limit: int) -> list[CatalogDocument]: ...

    async def retrieve_document(
        self, document: CatalogDocument, query: str, limit: int
    ) -> list[Chunk]: ...


class AzureBroadBackend(AzureEvidenceBackend):
    def __init__(
        self,
        settings: Settings,
        knowledge_base: KnowledgeBaseRetrievalClient,
        source: ContainerClient,
        pages: ContainerClient,
        *,
        catalog: SearchClient,
        chunks: SearchClient,
        usage: UsageTracker,
    ) -> None:
        super().__init__(settings, knowledge_base, source, pages, usage=usage)
        self.catalog = catalog
        self.chunks = chunks

    async def backfill_catalog(self, blob_name: str) -> None:
        self.usage.increment("documents_started")
        key = document_id(source_url(self.settings, blob_name))
        rows = await self.chunks.search(
            search_text="*",
            filter=f"document_id eq {odata_literal(key)}",
            top=1,
            select=list(Chunk.model_fields),
        )
        hits = [
            Chunk.model_validate({name: row[name] for name in Chunk.model_fields})
            async for row in rows
        ]
        if not hits:
            raise UnindexedDocumentError(
                f"No indexed chunks for {blob_name}; ingest this PDF first"
            )
        hit = hits[0]
        other_revisions = await self.chunks.search(
            search_text="*",
            filter=(
                f"document_id eq {odata_literal(key)} and revision ne {odata_literal(hit.revision)}"
            ),
            top=1,
            select=["id"],
        )
        async for _ in other_revisions:
            raise ValueError("Multiple indexed revisions; finish ingestion before catalog backfill")
        stored = await self.load_document(hit)
        pages = await self.load_all_pages(stored)
        if hit.content not in pages[hit.page_number - 1].text:
            raise ValueError("Indexed chunk does not match its stored page")
        document = Document(
            **{name: getattr(stored, name) for name in DocumentIdentity.model_fields},
            pages=pages,
        )
        await publish_catalog(document, self.catalog, self.usage)
        self.usage.increment("pages_catalogued", len(pages))
        self.usage.increment("documents_completed")

    async def discover_documents(self, query: str, limit: int) -> list[CatalogDocument]:
        with self.usage.operation("catalog_search"):
            rows = await self.catalog.search(
                search_text=query,
                query_type="semantic",
                semantic_configuration_name=CATALOG_SEMANTIC,
                top=limit,
                semantic_error_mode="fail",
                minimum_coverage=100,
                raw_response_hook=reject_partial_retrieval,
            )
            documents = []
            async for row in rows:
                fields = {
                    key: value
                    for key, value in row.items()
                    if not key.startswith("@") and key not in ("sections_json", "headings")
                }
                fields["sections"] = json.loads(row["sections_json"])
                documents.append(CatalogDocument.model_validate(fields))
        return documents

    async def retrieve_document(
        self, document: CatalogDocument, query: str, limit: int
    ) -> list[Chunk]:
        with self.usage.operation("document_search", document=document.blob_name):
            rows = await self.chunks.search(
                search_text=query,
                filter=(
                    f"document_id eq {odata_literal(document.document_id)} and "
                    f"revision eq {odata_literal(document.revision)}"
                ),
                query_type="semantic",
                semantic_configuration_name=SEMANTIC_CONFIGURATION,
                vector_queries=[
                    VectorizableTextQuery(text=query, fields=VECTOR_FIELD, k_nearest_neighbors=50)
                ],
                vector_filter_mode="preFilter",
                semantic_error_mode="fail",
                minimum_coverage=100,
                select=list(Chunk.model_fields),
                top=limit,
                raw_response_hook=reject_partial_retrieval,
            )
            hits = [
                Chunk.model_validate({key: row[key] for key in Chunk.model_fields})
                async for row in rows
            ]
        for hit in hits:
            if any(
                getattr(hit, field) != getattr(document, field)
                for field in (
                    "document_id",
                    "revision",
                    "source_url",
                    "source_etag",
                    "blob_name",
                    "page_count",
                )
            ):
                raise ValueError("Document-scoped search returned mismatched provenance")
        return hits


class ScopedBackend:
    def __init__(self, backend: BroadBackend, document: CatalogDocument, limit: int) -> None:
        self.backend = backend
        self.document = document
        self.limit = limit

    async def retrieve(self, query: str) -> list[Chunk]:
        hits = await self.backend.retrieve_document(self.document, query, min(self.limit * 4, 50))
        pages: set[int] = set()
        unique = []
        for hit in hits:
            if (
                hit.document_id != self.document.document_id
                or hit.revision != self.document.revision
            ):
                raise ValueError("Worker attempted to cross its document/revision boundary")
            if hit.page_number not in pages:
                unique.append(hit)
                pages.add(hit.page_number)
                if len(unique) == self.limit:
                    break
        return unique

    async def load_document(self, hit: Chunk) -> StoredDocument:
        return await self.backend.load_document(hit)

    async def load_pages(self, document: StoredDocument, numbers: list[int]) -> list[Page]:
        return await self.backend.load_pages(document, numbers)

    async def load_all_pages(self, document: StoredDocument) -> list[Page]:
        return await self.backend.load_all_pages(document)
