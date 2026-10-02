import hashlib
import logging
from collections.abc import Iterable
from io import BytesIO
from urllib.parse import quote
from uuid import uuid4

from azure.ai.documentintelligence.aio import DocumentIntelligenceClient
from azure.core import MatchConditions
from azure.search.documents.aio import SearchClient
from azure.storage.blob import ContentSettings
from azure.storage.blob.aio import ContainerClient

from .config import Settings
from .models import Chunk, ContentSpan, Document, DocumentManifest, Page, PageReference

logger = logging.getLogger(__name__)


def source_url(settings: Settings, blob_name: str) -> str:
    return (
        f"{settings.storage_account_url}/{settings.source_container}/{quote(blob_name, safe='/')}"
    )


def document_id(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()


def manifest_name(doc_id: str, revision: str) -> str:
    return f"{doc_id}/{revision}.json"


def markdown_name(doc_id: str, revision: str) -> str:
    return f"{doc_id}/{revision}/document.md"


def page_name(doc_id: str, revision: str, page: int) -> str:
    return f"{doc_id}/{revision}/pages/{page:04d}.md"


def span_text(content: str, spans: list[ContentSpan]) -> str:
    previous_end = 0
    parts = []
    for span in spans:
        end = span.offset + span.length
        if span.offset < previous_end or end > len(content):
            raise ValueError("Page spans overlap, are out of order, or exceed Markdown content")
        parts.append(content[span.offset : end])
        previous_end = end
    return "".join(parts)


def markdown_manifest(
    document: Document, content: str, spans: list[list[ContentSpan]]
) -> DocumentManifest:
    if len(spans) != len(document.pages):
        raise ValueError("Page span count does not match the document")
    references = []
    for page, page_spans in zip(document.pages, spans, strict=True):
        if span_text(content, page_spans) != page.text:
            raise ValueError("Page Markdown does not match its full-document spans")
        references.append(
            PageReference(
                number=page.number,
                markdown_blob=page_name(document.document_id, document.revision, page.number),
                sha256=hashlib.sha256(page.text.encode("utf-8")).hexdigest(),
                content_chars=len(page.text),
                spans=page_spans,
            )
        )
    return DocumentManifest(
        **document.model_dump(exclude={"schema_version", "pages"}),
        markdown_blob=markdown_name(document.document_id, document.revision),
        sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
        content_chars=len(content),
        pages=references,
    )


def chunks_for(document: Document, size: int, overlap: int) -> Iterable[Chunk]:
    if size <= 0 or overlap < 0 or overlap >= size:
        raise ValueError("Require size > overlap >= 0")
    for page in document.pages:
        for start in range(0, len(page.text), size - overlap):
            text = page.text[start : start + size]
            if text.strip():
                yield Chunk(
                    id=f"{document.document_id}-{document.revision}-{page.number}-{start}",
                    document_id=document.document_id,
                    revision=document.revision,
                    blob_name=document.blob_name,
                    source_url=document.source_url,
                    source_etag=document.source_etag,
                    title=document.title,
                    page_number=page.number,
                    page_count=len(document.pages),
                    content=text,
                )
            if start + size >= len(page.text):
                break


def odata_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


async def ingest_pdf(
    blob_name: str,
    settings: Settings,
    source: ContainerClient,
    pages: ContainerClient,
    intelligence: DocumentIntelligenceClient,
    search: SearchClient,
) -> int:
    if not blob_name.lower().endswith(".pdf"):
        raise ValueError(f"Only PDF blobs are supported: {blob_name}")
    blob = source.get_blob_client(blob_name)
    props = await blob.get_blob_properties()
    if props.size > settings.max_pdf_bytes:
        raise ValueError(f"PDF exceeds max_pdf_bytes: {blob_name}")
    download = await blob.download_blob(
        etag=props.etag,
        match_condition=MatchConditions.IfNotModified,
    )
    data = await download.readall()
    if b"%PDF-" not in data[:1024]:
        raise ValueError(f"Blob is not a PDF: {blob_name}")
    poller = await intelligence.begin_analyze_document(
        "prebuilt-layout",
        body=BytesIO(data),
        content_type="application/pdf",
        string_index_type="unicodeCodePoint",
        output_content_format="markdown",
    )
    result = await poller.result()
    if result.content_format != "markdown" or result.string_index_type != "unicodeCodePoint":
        raise ValueError("Document Intelligence must return Markdown with Unicode code-point spans")
    if not result.pages or len(result.pages) > settings.max_document_pages:
        raise ValueError("Document has no pages or exceeds max_document_pages")
    extracted = []
    page_spans = []
    for page in result.pages:
        spans = [ContentSpan(offset=span.offset, length=span.length) for span in page.spans or []]
        if not spans and (page.words or page.lines):
            raise ValueError("Nonblank Document Intelligence page has no Markdown spans")
        text = span_text(result.content, spans)
        extracted.append(Page(number=page.page_number, text=text))
        page_spans.append(spans)
    if [p.number for p in extracted] != list(range(1, len(extracted) + 1)):
        raise ValueError("Document Intelligence returned non-contiguous physical pages")
    if not any(page.text.strip() for page in extracted):
        raise ValueError("Document Intelligence extracted no searchable text")
    url = source_url(settings, blob_name)
    doc = Document(
        document_id=document_id(url),
        revision=uuid4().hex,
        blob_name=blob_name,
        source_url=url,
        source_etag=props.etag,
        title=blob_name.rsplit("/", 1)[-1],
        pages=extracted,
    )
    manifest = markdown_manifest(doc, result.content, page_spans)
    # Do not publish evidence for a PDF that changed while extraction was running.
    await blob.get_blob_properties(etag=props.etag, match_condition=MatchConditions.IfNotModified)
    for extracted_page, reference in zip(doc.pages, manifest.pages, strict=True):
        await pages.upload_blob(
            name=reference.markdown_blob,
            data=extracted_page.text.encode("utf-8"),
            overwrite=False,
            content_settings=ContentSettings(content_type="text/markdown; charset=utf-8"),
        )
    await pages.upload_blob(
        name=manifest.markdown_blob,
        data=result.content.encode("utf-8"),
        overwrite=False,
        content_settings=ContentSettings(content_type="text/markdown; charset=utf-8"),
    )
    # Publish the manifest last so it never points to Markdown that has not been uploaded.
    await pages.upload_blob(
        name=manifest_name(doc.document_id, doc.revision),
        data=manifest.model_dump_json().encode(),
        overwrite=False,
        content_settings=ContentSettings(content_type="application/json"),
    )
    chunks = list(chunks_for(doc, settings.chunk_chars, settings.chunk_overlap))
    for start in range(0, len(chunks), 100):
        results = await search.upload_documents(
            documents=[chunk.model_dump() for chunk in chunks[start : start + 100]],
        )
        failures = [r for r in results if not r.succeeded]
        if failures:
            details = [(r.key, r.error_message) for r in failures]
            raise RuntimeError(f"Index upload failed: {details}")
    # Upload the new revision first. Any partial failure remains visible to the operator.
    old = await search.search(
        search_text="*",
        filter=(
            f"document_id eq {odata_literal(doc.document_id)} "
            f"and revision ne {odata_literal(doc.revision)}"
        ),
        select=["id"],
    )
    stale = [{"id": row["id"]} async for row in old]
    for start in range(0, len(stale), 100):
        deleted = await search.delete_documents(documents=stale[start : start + 100])
        if any(not item.succeeded for item in deleted):
            raise RuntimeError("New revision indexed but stale-chunk removal failed; rerun ingest")
    logger.info("Indexed %s: %d pages, %d chunks", blob_name, len(doc.pages), len(chunks))
    return len(chunks)
