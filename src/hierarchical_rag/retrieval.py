import hashlib
import logging
from typing import Protocol

from azure.core import MatchConditions
from azure.core.exceptions import HttpResponseError
from azure.core.pipeline import PipelineResponse
from azure.core.rest import AsyncHttpResponse, HttpRequest
from azure.search.documents.knowledgebases.aio import KnowledgeBaseRetrievalClient
from azure.search.documents.knowledgebases.models import (
    KnowledgeBaseRetrievalRequest,
    KnowledgeBaseRetrievalResponse,
    KnowledgeBaseSearchIndexReference,
    KnowledgeRetrievalSemanticIntent,
    SearchIndexKnowledgeSourceParams,
)
from azure.storage.blob.aio import ContainerClient

from .config import Settings
from .ingestion import document_id, manifest_name, markdown_name, page_name, source_url, span_text
from .models import Chunk, Document, Page, StoredDocument, stored_document_adapter

logger = logging.getLogger(__name__)


class EvidenceBackend(Protocol):
    async def retrieve(self, query: str) -> list[Chunk]: ...

    async def load_document(self, hit: Chunk) -> StoredDocument: ...

    async def load_pages(self, document: StoredDocument, numbers: list[int]) -> list[Page]: ...

    async def load_all_pages(self, document: StoredDocument) -> list[Page]: ...


def validate_provenance(document: StoredDocument, hit: Chunk) -> None:
    for field in ("document_id", "revision", "blob_name", "source_url", "source_etag"):
        if getattr(document, field) != getattr(hit, field):
            raise ValueError(f"Page manifest provenance mismatch: {field}")
    if len(document.pages) != hit.page_count or [p.number for p in document.pages] != list(
        range(1, hit.page_count + 1),
    ):
        raise ValueError("Page manifest pagination does not match the index")
    if hit.page_number > len(document.pages):
        raise ValueError("Retrieved chunk points outside the document")


def validate_page(text: str, sha256: str, content_chars: int) -> None:
    if hashlib.sha256(text.encode("utf-8")).hexdigest() != sha256 or len(text) != content_chars:
        raise ValueError("Stored Markdown failed its manifest hash or character-length check")


def reject_partial_retrieval(response: PipelineResponse[HttpRequest, AsyncHttpResponse]) -> None:
    if response.http_response.status_code == 206:
        raise HttpResponseError(
            message="IQ returned partial retrieval (HTTP 206); retry before answering",
            response=response.http_response,
        )


def retrieval_request(query: str, source_name: str) -> KnowledgeBaseRetrievalRequest:
    if not query.strip():
        raise ValueError("Search query must not be empty")
    return KnowledgeBaseRetrievalRequest(
        include_activity=True,
        intents=[KnowledgeRetrievalSemanticIntent(search=query)],
        knowledge_source_params=[
            SearchIndexKnowledgeSourceParams(
                knowledge_source_name=source_name,
                include_references=True,
                include_reference_source_data=True,
            ),
        ],
    )


def parse_references(response: KnowledgeBaseRetrievalResponse) -> list[Chunk]:
    for activity in response.activity or []:
        if activity.error:
            raise RuntimeError(f"IQ retrieval activity failed: {activity.error.as_dict()}")
    hits = []
    seen: set[str] = set()
    for reference in response.references or []:
        if not isinstance(reference, KnowledgeBaseSearchIndexReference):
            raise ValueError("Expected search-index references from the configured source")
        if reference.source_data is None:
            raise ValueError("IQ reference is missing source data; check knowledge source fields")
        chunk = Chunk.model_validate(reference.source_data)
        if reference.doc_key and reference.doc_key != chunk.id:
            raise ValueError("IQ reference key does not match its source data")
        if chunk.id not in seen:
            seen.add(chunk.id)
            hits.append(chunk)
    return hits


class AzureEvidenceBackend:
    def __init__(
        self,
        settings: Settings,
        knowledge_base: KnowledgeBaseRetrievalClient,
        source: ContainerClient,
        pages: ContainerClient,
    ) -> None:
        self.settings = settings
        self.knowledge_base = knowledge_base
        self.source = source
        self.pages = pages

    async def retrieve(self, query: str) -> list[Chunk]:
        response = await self.knowledge_base.retrieve(
            retrieval_request(query, self.settings.knowledge_source_name),
            raw_response_hook=reject_partial_retrieval,
        )
        return parse_references(response)

    async def load_document(self, hit: Chunk) -> StoredDocument:
        expected_url = source_url(self.settings, hit.blob_name)
        if hit.source_url != expected_url or hit.document_id != document_id(expected_url):
            raise ValueError(
                "Reference is outside the configured Blob source or has invalid identity"
            )
        if not hit.revision.isalnum():
            raise ValueError("Invalid document revision")
        # Never fetch an arbitrary model-supplied URL; use the configured container.
        await self.source.get_blob_client(hit.blob_name).get_blob_properties(
            etag=hit.source_etag,
            match_condition=MatchConditions.IfNotModified,
        )
        manifest = self.pages.get_blob_client(manifest_name(hit.document_id, hit.revision))
        download = await manifest.download_blob()
        doc = stored_document_adapter.validate_json(await download.readall())
        validate_provenance(doc, hit)
        if isinstance(doc, Document):
            logger.warning(
                "Reading legacy inline-page JSON for %s; re-ingest for lazy Markdown reads",
                doc.document_id,
            )
            if hit.content not in doc.pages[hit.page_number - 1].text:
                raise ValueError("Retrieved chunk does not occur on its recorded physical page")
        else:
            if doc.markdown_blob != markdown_name(doc.document_id, doc.revision):
                raise ValueError("Manifest full-document Markdown path is outside its revision")
            for page in doc.pages:
                if page.markdown_blob != page_name(doc.document_id, doc.revision, page.number):
                    raise ValueError("Manifest page Markdown path is outside its revision")
                previous_end = 0
                for span in page.spans:
                    if span.offset < previous_end or span.offset + span.length > doc.content_chars:
                        raise ValueError("Manifest contains invalid page spans")
                    previous_end = span.offset + span.length
                if sum(span.length for span in page.spans) != page.content_chars:
                    raise ValueError("Manifest page span lengths do not match its Markdown length")
        return doc

    async def _markdown(self, name: str, sha256: str, content_chars: int) -> str:
        download = await self.pages.get_blob_client(name).download_blob()
        text = (await download.readall()).decode("utf-8")
        validate_page(text, sha256, content_chars)
        return text

    async def load_pages(self, document: StoredDocument, numbers: list[int]) -> list[Page]:
        if any(number < 1 or number > len(document.pages) for number in numbers):
            raise ValueError("Requested page is outside the document")
        if isinstance(document, Document):
            return [document.pages[number - 1] for number in numbers]
        result = []
        for number in numbers:
            reference = document.pages[number - 1]
            text = await self._markdown(
                reference.markdown_blob,
                reference.sha256,
                reference.content_chars,
            )
            result.append(Page(number=number, text=text))
        return result

    async def load_all_pages(self, document: StoredDocument) -> list[Page]:
        if isinstance(document, Document):
            return document.pages
        content = await self._markdown(
            document.markdown_blob,
            document.sha256,
            document.content_chars,
        )
        result = []
        for reference in document.pages:
            text = span_text(content, reference.spans)
            validate_page(text, reference.sha256, reference.content_chars)
            result.append(Page(number=reference.number, text=text))
        return result
