import hashlib
import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from azure.ai.contentunderstanding.models import AnalysisResult
from azure.core import MatchConditions
from azure.core.exceptions import ResourceModifiedError

from hierarchical_rag.ingestion import chunks_for, ingest_pdf
from hierarchical_rag.models import DocumentManifest, Page
from hierarchical_rag.retrieval import AzureEvidenceBackend


async def empty_results():
    if False:
        yield {}


def analysis_result(text="First page. Second page.", split=12):
    return AnalysisResult(
        {
            "apiVersion": "2025-11-01",
            "analyzerId": "prebuilt-documentSearch",
            "stringEncoding": "codePoint",
            "warnings": [],
            "contents": [
                {
                    "kind": "document",
                    "mimeType": "application/pdf",
                    "startPageNumber": 1,
                    "endPageNumber": 2,
                    "markdown": text,
                    "pages": [
                        {"pageNumber": 1, "spans": [{"offset": 0, "length": split}]},
                        {
                            "pageNumber": 2,
                            "spans": [{"offset": split, "length": len(text) - split}],
                        },
                    ],
                }
            ],
        }
    )


def ingestion_clients(text="First page. Second page.", split=12):
    result = analysis_result(text, split)
    blob = Mock(
        get_blob_properties=AsyncMock(
            return_value=SimpleNamespace(size=30, etag='"v1"'),
        ),
        download_blob=AsyncMock(return_value=Mock(readall=AsyncMock(return_value=b"%PDF-1.7\n"))),
    )
    source = Mock(get_blob_client=Mock(return_value=blob))
    pages = Mock(upload_blob=AsyncMock())
    cu = Mock(
        begin_analyze=AsyncMock(
            return_value=Mock(result=AsyncMock(return_value=result), operation_id="test-operation"),
        )
    )
    search = Mock(
        upload_documents=AsyncMock(return_value=[SimpleNamespace(succeeded=True)]),
        search=AsyncMock(return_value=empty_results()),
        delete_documents=AsyncMock(),
    )
    async def embed(**kwargs):
        return SimpleNamespace(
            data=[
                SimpleNamespace(index=i, embedding=[0.1] * kwargs["dimensions"])
                for i in range(len(kwargs["input"]))
            ]
        )

    search.embedding_client = Mock(embeddings=Mock(create=AsyncMock(side_effect=embed)))
    return blob, source, pages, cu, search


def test_page_bounded_chunking_and_overlap(document):
    document.pages = [Page(number=1, text="abcdefghij"), Page(number=2, text="klmnop")]
    chunks = list(chunks_for(document, 6, 2))
    assert [(chunk.page_number, chunk.content) for chunk in chunks] == [
        (1, "abcdef"),
        (1, "efghij"),
        (2, "klmnop"),
    ]
    assert all(chunk.source_etag == document.source_etag for chunk in chunks)


@pytest.mark.parametrize("size,overlap", [(0, 0), (3, 3), (3, -1)])
def test_invalid_chunk_settings(document, size, overlap):
    with pytest.raises(ValueError):
        list(chunks_for(document, size, overlap))


async def test_ingestion_persists_unicode_physical_pages(settings):
    text = "# First \U0001f600 page\n\n<table><tr><td>Exception.</td></tr></table>"
    split = text.index("<table>")
    blob, source, pages, di, search = ingestion_clients(text, split)
    count = await ingest_pdf("a.pdf", settings, source, pages, di, search, search.embedding_client)
    assert count == 2
    kwargs = di.begin_analyze.call_args.kwargs
    assert kwargs["processing_location"] == "geography"
    assert kwargs["inputs"][0].data.startswith(b"%PDF-")
    assert kwargs["inputs"][0].mime_type == "application/pdf"
    writes = [call.kwargs for call in pages.upload_blob.call_args_list]
    assert len(writes) == 6
    manifest = DocumentManifest.model_validate_json(writes[-1]["data"])
    assert "text" not in manifest.pages[0].model_dump()
    assert manifest.pages[1].spans[0].offset == split
    assert writes[0]["data"].decode() == text[:split]
    assert writes[1]["data"].decode() == text[split:]
    assert writes[2]["data"].decode() == text
    assert writes[0]["name"] == manifest.pages[0].markdown_blob
    assert writes[2]["name"] == manifest.markdown_blob
    assert all(write["overwrite"] is False for write in writes)
    assert all(
        write["content_settings"].content_type == "text/markdown; charset=utf-8"
        for write in writes[:3]
    )
    assert manifest.extraction.provider == "azure_content_understanding"
    assert manifest.extraction.analysis_blob == writes[3]["name"]
    assert hashlib.sha256(writes[3]["data"]).hexdigest() == manifest.extraction.analysis_sha256
    assert json.loads(writes[3]["data"])["stringEncoding"] == "codePoint"
    assert manifest.extraction.figures.blob == writes[4]["name"]
    assert hashlib.sha256(writes[4]["data"]).hexdigest() == manifest.extraction.figures.sha256
    assert json.loads(writes[4]["data"])["figures"] == []
    assert blob.get_blob_properties.call_count == 2
    assert blob.download_blob.call_args.kwargs["match_condition"] == MatchConditions.IfNotModified
    uploaded = search.upload_documents.call_args.kwargs["documents"]
    assert [chunk["page_number"] for chunk in uploaded] == [1, 2]
    assert uploaded[1]["source_etag"] == '"v1"'
    assert len(uploaded[0]["content_vector"]) == settings.embedding_dimensions
    assert search.embedding_client.embeddings.create.call_args.kwargs["input"] == [
        text[:split], text[split:]
    ]


async def test_changed_pdf_not_published(settings):
    blob, source, pages, di, search = ingestion_clients()
    blob.get_blob_properties.side_effect = [
        SimpleNamespace(size=30, etag='"v1"'),
        ResourceModifiedError("changed"),
    ]
    with pytest.raises(ResourceModifiedError):
        await ingest_pdf("a.pdf", settings, source, pages, di, search, search.embedding_client)
    pages.upload_blob.assert_not_called()
    search.upload_documents.assert_not_called()


async def test_index_partial_failure_is_explicit(settings):
    _, source, pages, di, search = ingestion_clients()
    search.upload_documents.return_value = [
        SimpleNamespace(succeeded=False, key="chunk", error_message="quota"),
    ]
    with pytest.raises(RuntimeError, match="quota"):
        await ingest_pdf("a.pdf", settings, source, pages, di, search, search.embedding_client)
    search.search.assert_not_called()


async def test_oversized_pdf_rejected_before_download(settings):
    blob, source, pages, di, search = ingestion_clients()
    settings.max_pdf_bytes = 20
    with pytest.raises(ValueError, match="max_pdf_bytes"):
        await ingest_pdf("a.pdf", settings, source, pages, di, search, search.embedding_client)
    blob.download_blob.assert_not_called()
    di.begin_analyze.assert_not_called()


async def test_source_and_manifest_provenance(settings, document, hit):
    source_blob = Mock(get_blob_properties=AsyncMock())
    source = Mock(get_blob_client=Mock(return_value=source_blob))
    manifest_blob = Mock(
        download_blob=AsyncMock(
            return_value=Mock(readall=AsyncMock(return_value=document.model_dump_json().encode())),
        )
    )
    pages = Mock(get_blob_client=Mock(return_value=manifest_blob))
    backend = AzureEvidenceBackend(settings, Mock(), source, pages)
    assert await backend.load_document(hit) == document
    assert source_blob.get_blob_properties.call_args.kwargs["etag"] == hit.source_etag
    with pytest.raises(ValueError, match="outside"):
        await backend.load_document(hit.model_copy(update={"source_url": "https://evil.example"}))
    with pytest.raises(ValueError, match="does not occur"):
        await backend.load_document(hit.model_copy(update={"content": "invented chunk"}))
    source_blob.get_blob_properties.side_effect = ResourceModifiedError("stale index")
    with pytest.raises(ResourceModifiedError):
        await backend.load_document(hit)


async def test_ingestion_logs_stages_without_document_content(settings, caplog):
    caplog.set_level(logging.DEBUG, logger="hierarchical_rag.ingestion")
    _, source, pages, cu, search = ingestion_clients("PRIVATE-PDF-TEXT", 8)
    await ingest_pdf("a.pdf", settings, source, pages, cu, search, search.embedding_client)
    messages = [record.getMessage() for record in caplog.records]
    stages = [
        "Ingesting a.pdf",
        "Downloading a.pdf",
        "Downloaded a.pdf",
        "Analyzing a.pdf",
        "Analysis submitted for a.pdf",
        "Analysis validated for a.pdf: 2 pages",
        "Rechecking source version",
        "Uploading artifacts",
        "Page uploads for a.pdf: 2/2 complete",
        "Artifacts published for a.pdf: 6 blobs",
        "Indexing a.pdf: 2 chunks",
        "Index uploads for a.pdf: 2/2 chunks complete",
        "Looking for stale",
        "Found 0 stale",
        "Stale cleanup finished",
        "Ingestion complete for a.pdf",
    ]
    positions = [
        next(i for i, message in enumerate(messages) if stage in message) for stage in stages
    ]
    assert positions == sorted(positions)
    assert "elapsed=" in messages[-1]
    assert "test-operation" in caplog.text
    assert "Uploaded page 1/2" in caplog.text
    assert "PRIVATE-" not in caplog.text
    assert "PDF-TEXT" not in caplog.text
    assert "%PDF-" not in caplog.text


async def test_ingestion_logs_batch_progress(settings, caplog):
    caplog.set_level(logging.INFO, logger="hierarchical_rag.ingestion")
    settings.chunk_chars = 200
    settings.chunk_overlap = 0
    _, source, pages, cu, search = ingestion_clients("x" * 20_400, 20_200)

    async def stale_results():
        for i in range(101):
            yield {"id": f"old-{i}"}

    search.search.return_value = stale_results()
    search.delete_documents.return_value = [SimpleNamespace(succeeded=True)]
    count = await ingest_pdf("a.pdf", settings, source, pages, cu, search, search.embedding_client)
    assert count == 102
    assert "Index uploads for a.pdf: 100/102 chunks complete" in caplog.text
    assert "Index uploads for a.pdf: 102/102 chunks complete" in caplog.text
    assert "Stale cleanup for a.pdf: 100/101 chunks removed" in caplog.text
    assert "Stale cleanup for a.pdf: 101/101 chunks removed" in caplog.text


@pytest.mark.parametrize("stage", ["analysis", "artifacts", "index", "cleanup"])
async def test_failure_never_logs_ingestion_completion(settings, caplog, stage):
    caplog.set_level(logging.INFO, logger="hierarchical_rag.ingestion")
    _, source, pages, cu, search = ingestion_clients()
    if stage == "analysis":
        cu.begin_analyze.return_value.result.side_effect = RuntimeError("analysis unavailable")
    elif stage == "artifacts":
        pages.upload_blob.side_effect = RuntimeError("storage unavailable")
    elif stage == "index":
        search.upload_documents.return_value = [
            SimpleNamespace(succeeded=False, key="chunk", error_message="quota")
        ]
    else:
        async def stale_results():
            yield {"id": "old"}

        search.search.return_value = stale_results()
        search.delete_documents.return_value = [SimpleNamespace(succeeded=False)]
    with pytest.raises(RuntimeError):
        await ingest_pdf("a.pdf", settings, source, pages, cu, search, search.embedding_client)
    assert "Ingestion complete" not in caplog.text
    if stage == "index":
        assert "stale cleanup will not run" in caplog.text
        search.search.assert_not_called()
    if stage == "cleanup":
        assert "new revision is indexed" in caplog.text
