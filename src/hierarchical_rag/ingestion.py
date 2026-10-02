import hashlib
import json
import logging
from collections.abc import Iterable
from time import perf_counter
from urllib.parse import quote
from uuid import uuid4

from azure.ai.contentunderstanding.aio import ContentUnderstandingClient
from azure.ai.contentunderstanding.models import AnalysisInput, AnalysisResult, DocumentContent
from azure.core import MatchConditions
from azure.search.documents.aio import SearchClient
from azure.storage.blob import ContentSettings
from azure.storage.blob.aio import ContainerClient
from openai import AsyncAzureOpenAI

from .config import Settings
from .embeddings import VECTOR_FIELD, embed_chunks
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
    embeddings: AsyncAzureOpenAI,
) -> int:
    started = perf_counter()
    logger.info("Ingesting %s: reading source blob properties", blob_name)
    if not blob_name.lower().endswith(".pdf"):
        raise ValueError(f"Only PDF blobs are supported: {blob_name}")
    blob = source.get_blob_client(blob_name)
    props = await blob.get_blob_properties()
    if props.size > settings.max_pdf_bytes:
        raise ValueError(f"PDF exceeds max_pdf_bytes: {blob_name}")
    stage_started = perf_counter()
    logger.info("Downloading %s: %d bytes", blob_name, props.size)
    download = await blob.download_blob(
        etag=props.etag,
        match_condition=MatchConditions.IfNotModified,
    )
    data = await download.readall()
    if b"%PDF-" not in data[:1024]:
        raise ValueError(f"Blob is not a PDF: {blob_name}")
    logger.info(
        "Downloaded %s: %d bytes in %.1fs", blob_name, len(data), perf_counter() - stage_started
    )
    stage_started = perf_counter()
    logger.info(
        "Analyzing %s with Content Understanding: analyzer=%s, processing_location=%s",
        blob_name,
        settings.content_understanding_analyzer,
        settings.content_understanding_processing_location,
    )
    poller = await intelligence.begin_analyze(
        settings.content_understanding_analyzer,
        inputs=[AnalysisInput(data=data, mime_type="application/pdf")],
        model_deployments=settings.content_understanding_model_deployments or None,
        processing_location=settings.content_understanding_processing_location,
    )
    logger.info(
        "Analysis submitted for %s; waiting for Content Understanding (may take several minutes)",
        blob_name,
    )
    logger.debug("Content Understanding operation for %s: %s", blob_name, poller.operation_id)
    result = await poller.result()
    content, extracted, page_spans = extract_pages(result, settings)
    logger.info(
        "Analysis validated for %s: %d pages, %d Markdown characters in %.1fs",
        blob_name,
        len(extracted),
        len(content),
        perf_counter() - stage_started,
    )
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
    logger.info("Rechecking source version before publishing %s", blob_name)
    await blob.get_blob_properties(etag=props.etag, match_condition=MatchConditions.IfNotModified)
    stage_started = perf_counter()
    logger.info(
        "Uploading artifacts for %s: revision=%s, %d pages plus Markdown, analysis and manifest",
        blob_name,
        doc.revision,
        len(doc.pages),
    )
    for extracted_page, reference in zip(doc.pages, manifest.pages, strict=True):
        await pages.upload_blob(
            name=reference.markdown_blob,
            data=extracted_page.text.encode("utf-8"),
            overwrite=False,
            content_settings=ContentSettings(content_type="text/markdown; charset=utf-8"),
        )
        logger.debug(
            "Uploaded page %d/%d for %s: %d characters",
            extracted_page.number,
            len(doc.pages),
            blob_name,
            len(extracted_page.text),
        )
        if extracted_page.number % 25 == 0 or extracted_page.number == len(doc.pages):
            logger.info(
                "Page uploads for %s: %d/%d complete",
                blob_name,
                extracted_page.number,
                len(doc.pages),
            )
    await pages.upload_blob(
        name=manifest.markdown_blob,
        data=content.encode("utf-8"),
        overwrite=False,
        content_settings=ContentSettings(content_type="text/markdown; charset=utf-8"),
    )
    logger.debug("Uploaded full-document Markdown for %s", blob_name)
    await pages.upload_blob(
        name=manifest.extraction.analysis_blob,
        data=analysis,
        overwrite=False,
        content_settings=ContentSettings(content_type="application/json"),
    )
    logger.debug("Uploaded raw analysis JSON for %s", blob_name)
    # Publish the manifest last so it never points to Markdown that has not been uploaded.
    await pages.upload_blob(
        name=manifest_name(doc.document_id, doc.revision),
        data=manifest.model_dump_json().encode(),
        overwrite=False,
        content_settings=ContentSettings(content_type="application/json"),
    )
    logger.info(
        "Artifacts published for %s: %d blobs in %.1fs",
        blob_name,
        len(doc.pages) + 3,
        perf_counter() - stage_started,
    )
    chunks = list(chunks_for(doc, settings.chunk_chars, settings.chunk_overlap))
    stage_started = perf_counter()
    logger.info(
        "Indexing %s: %d chunks into %s (batches of up to 100)",
        blob_name,
        len(chunks),
        settings.index_name,
    )
    for start in range(0, len(chunks), 100):
        batch = chunks[start : start + 100]
        logger.info(
            "Embedding %s: chunks %d-%d/%d using %s (%d dimensions)",
            blob_name,
            start + 1,
            start + len(batch),
            len(chunks),
            settings.embedding_deployment,
            settings.embedding_dimensions,
        )
        vectors = await embed_chunks(batch, settings, embeddings)
        results = await search.upload_documents(
            documents=[
                {**chunk.model_dump(), VECTOR_FIELD: vector}
                for chunk, vector in zip(batch, vectors, strict=True)
            ],
        )
        failures = [r for r in results if not r.succeeded]
        if failures:
            logger.error(
                "Index batch failed for %s: %d failed chunks; stale cleanup will not run",
                blob_name,
                len(failures),
            )
            details = [(r.key, r.error_message) for r in failures]
            raise RuntimeError(f"Index upload failed: {details}")
        logger.info(
            "Index uploads for %s: %d/%d chunks complete",
            blob_name,
            min(start + 100, len(chunks)),
            len(chunks),
        )
    logger.info("Indexing finished for %s in %.1fs", blob_name, perf_counter() - stage_started)
    # Upload the new revision first. Any partial failure remains visible to the operator.
    stage_started = perf_counter()
    logger.info("Looking for stale indexed chunks for %s", blob_name)
    old = await search.search(
        search_text="*",
        filter=(
            f"document_id eq {odata_literal(doc.document_id)} "
            f"and revision ne {odata_literal(doc.revision)}"
        ),
        select=["id"],
    )
    stale = [{"id": row["id"]} async for row in old]
    logger.info("Found %d stale indexed chunks for %s", len(stale), blob_name)
    for start in range(0, len(stale), 100):
        deleted = await search.delete_documents(documents=stale[start : start + 100])
        if any(not item.succeeded for item in deleted):
            logger.error("Stale-chunk removal failed for %s; new revision is indexed", blob_name)
            raise RuntimeError("New revision indexed but stale-chunk removal failed; rerun ingest")
        logger.info(
            "Stale cleanup for %s: %d/%d chunks removed",
            blob_name,
            min(start + 100, len(stale)),
            len(stale),
        )
    logger.info("Stale cleanup finished for %s in %.1fs", blob_name, perf_counter() - stage_started)
    logger.info(
        "Ingestion complete for %s: %d pages, %d chunks, revision=%s, elapsed=%.1fs",
        blob_name,
        len(doc.pages),
        len(chunks),
        doc.revision,
        perf_counter() - started,
    )
    return len(chunks)
