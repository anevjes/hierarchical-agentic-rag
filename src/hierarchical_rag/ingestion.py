import hashlib
import json
import logging
from collections.abc import Iterable
from urllib.parse import quote
from uuid import uuid4

from azure.ai.contentunderstanding.aio import ContentUnderstandingClient
from azure.ai.contentunderstanding.models import AnalysisInput, AnalysisResult, DocumentContent
from azure.core import MatchConditions
from azure.search.documents.aio import SearchClient
from azure.storage.blob import ContentSettings
from azure.storage.blob.aio import ContainerClient

from .config import Settings
from .models import (
    Chunk,
    ContentSpan,
    Document,
    DocumentManifest,
    ExtractionMetadata,
    Page,
    PageReference,
)

logger = logging.getLogger(__name__)
CONTENT_UNDERSTANDING_API_VERSION = "2025-11-01"


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


def extract_pages(
    result: AnalysisResult, settings: Settings
) -> tuple[str, list[Page], list[list[ContentSpan]]]:
    if result.warnings:
        raise ValueError(f"Content Understanding returned warnings: {result.warnings}")
    if result.analyzer_id != settings.content_understanding_analyzer:
        raise ValueError("Content Understanding returned an unexpected analyzer")
    if result.api_version != CONTENT_UNDERSTANDING_API_VERSION:
        raise ValueError("Content Understanding returned an unexpected API version")
    if result.string_encoding != "codePoint":
        raise ValueError("Content Understanding must return Unicode code-point spans")
    # One input PDF with no segmentation; never silently discard other content items.
    if len(result.contents) != 1 or not isinstance(result.contents[0], DocumentContent):
        raise ValueError("Expected exactly one Content Understanding document result")
    content = result.contents[0]
    if content.markdown is None:
        raise ValueError("Content Understanding must return Markdown")
    if not content.pages or len(content.pages) > settings.max_document_pages:
        raise ValueError("Document has no pages or exceeds max_document_pages")
    numbers = [page.page_number for page in content.pages]
    if (
        numbers != list(range(1, len(numbers) + 1))
        or content.start_page_number != 1
        or content.end_page_number != len(numbers)
    ):
        raise ValueError("Content Understanding returned non-contiguous physical pages")
    extracted = []
    page_spans = []
    previous_end = 0
    for page in content.pages:
        spans = [
            ContentSpan(offset=span.offset, length=span.length) for span in page.spans or []
        ]
        if not spans and (page.words or page.lines):
            raise ValueError("Nonblank Content Understanding page has no Markdown spans")
        for span in spans:
            if span.offset < previous_end:
                raise ValueError("Content Understanding page spans overlap or are out of order")
            previous_end = span.offset + span.length
        extracted.append(Page(number=page.page_number, text=span_text(content.markdown, spans)))
        page_spans.append(spans)
    # Figure descriptions/analysis must be in the page text we index, not only in raw JSON.
    for figure in content.figures or []:
        figure_span = figure.span
        if figure_span is None or figure_span.length <= 0 or not any(
            page_span.offset <= figure_span.offset
            and figure_span.offset + figure_span.length <= page_span.offset + page_span.length
            for spans in page_spans
            for page_span in spans
        ):
            raise ValueError("Content Understanding figure is not contained in a physical page")
    if not any(page.text.strip() for page in extracted):
        raise ValueError("Content Understanding extracted no searchable content")
    return content.markdown, extracted, page_spans


async def ingest_pdf(
    blob_name: str,
    settings: Settings,
    source: ContainerClient,
    pages: ContainerClient,
    intelligence: ContentUnderstandingClient,
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
    poller = await intelligence.begin_analyze(
        settings.content_understanding_analyzer,
        inputs=[AnalysisInput(data=data, mime_type="application/pdf")],
        model_deployments=settings.content_understanding_model_deployments or None,
        processing_location=settings.content_understanding_processing_location,
    )
    result = await poller.result()
    content, extracted, page_spans = extract_pages(result, settings)
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
    manifest = markdown_manifest(doc, content, page_spans)
    analysis = json.dumps(result.as_dict(), ensure_ascii=False).encode("utf-8")
    manifest.extraction = ExtractionMetadata(
        analyzer_id=settings.content_understanding_analyzer,
        api_version=CONTENT_UNDERSTANDING_API_VERSION,
        analysis_blob=f"{doc.document_id}/{doc.revision}/analysis.json",
        analysis_sha256=hashlib.sha256(analysis).hexdigest(),
    )
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
        data=content.encode("utf-8"),
        overwrite=False,
        content_settings=ContentSettings(content_type="text/markdown; charset=utf-8"),
    )
    await pages.upload_blob(
        name=manifest.extraction.analysis_blob,
        data=analysis,
        overwrite=False,
        content_settings=ContentSettings(content_type="application/json"),
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
