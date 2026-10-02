import hashlib
import json
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
            return_value=Mock(result=AsyncMock(return_value=result)),
        )
    )
    search = Mock(
        upload_documents=AsyncMock(return_value=[SimpleNamespace(succeeded=True)]),
        search=AsyncMock(return_value=empty_results()),
        delete_documents=AsyncMock(),
    )
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
    count = await ingest_pdf("a.pdf", settings, source, pages, di, search)
    assert count == 2
    kwargs = di.begin_analyze.call_args.kwargs
    assert kwargs["processing_location"] == "geography"
    assert kwargs["inputs"][0].data.startswith(b"%PDF-")
    assert kwargs["inputs"][0].mime_type == "application/pdf"
    writes = [call.kwargs for call in pages.upload_blob.call_args_list]
    assert len(writes) == 5
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
    assert blob.get_blob_properties.call_count == 2
    assert blob.download_blob.call_args.kwargs["match_condition"] == MatchConditions.IfNotModified
    uploaded = search.upload_documents.call_args.kwargs["documents"]
    assert [chunk["page_number"] for chunk in uploaded] == [1, 2]
    assert uploaded[1]["source_etag"] == '"v1"'


async def test_changed_pdf_not_published(settings):
    blob, source, pages, di, search = ingestion_clients()
    blob.get_blob_properties.side_effect = [
        SimpleNamespace(size=30, etag='"v1"'),
        ResourceModifiedError("changed"),
    ]
    with pytest.raises(ResourceModifiedError):
        await ingest_pdf("a.pdf", settings, source, pages, di, search)
    pages.upload_blob.assert_not_called()
    search.upload_documents.assert_not_called()


async def test_index_partial_failure_is_explicit(settings):
    _, source, pages, di, search = ingestion_clients()
    search.upload_documents.return_value = [
        SimpleNamespace(succeeded=False, key="chunk", error_message="quota"),
    ]
    with pytest.raises(RuntimeError, match="quota"):
        await ingest_pdf("a.pdf", settings, source, pages, di, search)
    search.search.assert_not_called()


async def test_oversized_pdf_rejected_before_download(settings):
    blob, source, pages, di, search = ingestion_clients()
    settings.max_pdf_bytes = 20
    with pytest.raises(ValueError, match="max_pdf_bytes"):
        await ingest_pdf("a.pdf", settings, source, pages, di, search)
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
