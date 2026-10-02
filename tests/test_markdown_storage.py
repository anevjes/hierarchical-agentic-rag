import hashlib
import json
from unittest.mock import AsyncMock, Mock

import pytest
from azure.core.exceptions import ResourceNotFoundError
from test_ingestion import ingestion_clients

from hierarchical_rag.ingestion import (
    chunks_for,
    ingest_pdf,
    manifest_name,
    markdown_manifest,
    span_text,
)
from hierarchical_rag.investigation import BudgetExceeded, Investigation
from hierarchical_rag.models import Chunk, ContentSpan, DocumentManifest, Page
from hierarchical_rag.retrieval import AzureEvidenceBackend


class MemoryBlobContainer:
    def __init__(self, blobs):
        self.blobs = blobs
        self.downloads = []

    def get_blob_client(self, name):
        async def download():
            self.downloads.append(name)
            if name not in self.blobs:
                raise ResourceNotFoundError(f"Missing blob: {name}")
            return Mock(readall=AsyncMock(return_value=self.blobs[name]))

        return Mock(download_blob=download)


@pytest.fixture
def markdown_backend(settings, document):
    document.pages = [
        Page(number=1, text="# Warranty \U0001f600\n\nTwo years."),
        Page(number=2, text="<table><tr><td>Flood exclusion</td></tr></table>"),
        Page(number=3, text=""),
        Page(number=4, text="## Appeals\n\nAppeals must be filed within thirty days."),
    ]
    content = ""
    spans = []
    for page in document.pages:
        spans.append([ContentSpan(offset=len(content), length=len(page.text))])
        content += page.text + "\n<!-- PageBreak -->\n"
    manifest = markdown_manifest(document, content, spans)
    blobs = {
        manifest_name(document.document_id, document.revision): manifest.model_dump_json().encode(),
        manifest.markdown_blob: content.encode(),
    }
    blobs.update(
        {
            ref.markdown_blob: page.text.encode()
            for ref, page in zip(manifest.pages, document.pages, strict=True)
        }
    )
    pages = MemoryBlobContainer(blobs)
    source = Mock(get_blob_client=Mock(return_value=Mock(get_blob_properties=AsyncMock())))
    backend = AzureEvidenceBackend(settings, Mock(), source, pages)
    hit = next(chunks_for(document, 200, 20))
    backend.retrieve = AsyncMock(return_value=[hit])
    return backend, pages, manifest, hit


async def test_open_pages_downloads_only_manifest_and_requested_pages(settings, markdown_backend):
    backend, blobs, manifest, _ = markdown_backend
    state = Investigation(settings, backend)
    await state.search_knowledge_base("warranty")
    await state.open_pages(manifest.document_id, 1, 1)
    assert blobs.downloads == [
        manifest_name(manifest.document_id, manifest.revision),
        manifest.pages[0].markdown_blob,
    ]
    await state.open_pages(manifest.document_id, 1, 1)
    assert len(blobs.downloads) == 2
    await state.open_pages(manifest.document_id, 2, 2)
    assert blobs.downloads[-1] == manifest.pages[1].markdown_blob
    assert manifest.markdown_blob not in blobs.downloads
    assert len(state.evidence) == 2


async def test_search_loads_full_markdown_once_and_caches_pages(settings, markdown_backend):
    backend, blobs, manifest, _ = markdown_backend
    state = Investigation(settings, backend)
    await state.search_knowledge_base("warranty")
    await state.open_pages(manifest.document_id, 1, 1)
    result = await state.search_document(manifest.document_id, "appeals")
    assert '"page_number": 4' in result
    assert blobs.downloads == [
        manifest_name(manifest.document_id, manifest.revision),
        manifest.pages[0].markdown_blob,
        manifest.markdown_blob,
    ]
    assert len(state.evidence) == 1  # Searching is not the same as opening grounding evidence.
    await state.search_document(manifest.document_id, "flood")
    await state.open_pages(manifest.document_id, 4, 4)
    assert len(blobs.downloads) == 3
    assert len(state.evidence) == 2


async def test_search_first_never_downloads_individual_pages(settings, markdown_backend):
    backend, blobs, manifest, _ = markdown_backend
    state = Investigation(settings, backend)
    await state.search_knowledge_base("warranty")
    await state.search_document(manifest.document_id, "flood")
    await state.open_pages(manifest.document_id, 1, 4)
    assert blobs.downloads == [
        manifest_name(manifest.document_id, manifest.revision),
        manifest.markdown_blob,
    ]
    assert len(state.evidence) == 4
    assert state.evidence[f"{manifest.document_id}:{manifest.revision}:p3"].text == ""


async def test_blank_page_has_its_own_empty_markdown_blob(settings, markdown_backend):
    backend, blobs, manifest, _ = markdown_backend
    state = Investigation(settings, backend)
    await state.search_knowledge_base("warranty")
    await state.open_pages(manifest.document_id, 3, 3)
    assert blobs.downloads[-1] == manifest.pages[2].markdown_blob
    assert next(iter(state.evidence.values())).text == ""


async def test_repeated_iq_hit_is_validated_against_cached_page(settings, markdown_backend):
    backend, _, manifest, hit = markdown_backend
    state = Investigation(settings, backend)
    await state.search_knowledge_base("warranty")
    await state.open_pages(manifest.document_id, 1, 1)
    backend.retrieve.return_value = [hit.model_copy(update={"id": "new", "content": "invented"})]
    await state.search_knowledge_base("exceptions")
    with pytest.raises(ValueError, match="does not occur"):
        await state.open_pages(manifest.document_id, 1, 1)


async def test_page_budget_rejects_before_downloading_markdown(settings, markdown_backend):
    backend, blobs, manifest, _ = markdown_backend
    settings.max_pages = 1
    state = Investigation(settings, backend)
    await state.search_knowledge_base("warranty")
    with pytest.raises(BudgetExceeded):
        await state.open_pages(manifest.document_id, 1, 2)
    assert blobs.downloads == [manifest_name(manifest.document_id, manifest.revision)]


@pytest.mark.parametrize("failure", ["span", "length", "version"])
async def test_invalid_manifest_fails_before_content_download(markdown_backend, failure):
    backend, blobs, manifest, hit = markdown_backend
    data = manifest.model_dump()
    if failure == "span":
        data["pages"][0]["spans"][0]["offset"] = manifest.content_chars + 1
    elif failure == "length":
        data["pages"][0]["content_chars"] += 1
    else:
        data["schema_version"] = 99
    blobs.blobs[manifest_name(manifest.document_id, manifest.revision)] = json.dumps(data).encode()
    with pytest.raises(ValueError):
        await backend.load_document(hit)
    assert len(blobs.downloads) == 1


@pytest.mark.parametrize("target,method", [("page", "open"), ("full", "search")])
async def test_modified_markdown_fails_closed(settings, markdown_backend, target, method):
    backend, blobs, manifest, _ = markdown_backend
    name = manifest.pages[0].markdown_blob if target == "page" else manifest.markdown_blob
    blobs.blobs[name] = b"corrupted"
    state = Investigation(settings, backend)
    await state.search_knowledge_base("warranty")
    with pytest.raises(ValueError, match="hash"):
        if method == "open":
            await state.open_pages(manifest.document_id, 1, 1)
        else:
            await state.search_document(manifest.document_id, "warranty")
    assert not state.evidence


async def test_missing_page_is_explicit_not_silently_reconstructed(settings, markdown_backend):
    backend, blobs, manifest, _ = markdown_backend
    del blobs.blobs[manifest.pages[0].markdown_blob]
    state = Investigation(settings, backend)
    await state.search_knowledge_base("warranty")
    with pytest.raises(ResourceNotFoundError):
        await state.open_pages(manifest.document_id, 1, 1)
    assert manifest.markdown_blob not in blobs.downloads


async def test_manifest_path_cannot_redirect_reads(markdown_backend):
    backend, blobs, manifest, hit = markdown_backend
    manifest.pages[0].markdown_blob = "other-document/secret.md"
    blobs.blobs[manifest_name(manifest.document_id, manifest.revision)] = (
        manifest.model_dump_json().encode()
    )
    with pytest.raises(ValueError, match="outside its revision"):
        await backend.load_document(hit)
    assert len(blobs.downloads) == 1


async def test_chunk_text_must_match_opened_markdown(settings, markdown_backend):
    backend, _, manifest, hit = markdown_backend
    backend.retrieve.return_value = [hit.model_copy(update={"content": "invented"})]
    state = Investigation(settings, backend)
    await state.search_knowledge_base("warranty")
    with pytest.raises(ValueError, match="does not occur"):
        await state.open_pages(manifest.document_id, 1, 1)
    assert not state.evidence


async def test_full_markdown_page_hashes_must_match_manifest(settings, markdown_backend):
    backend, blobs, manifest, _ = markdown_backend
    manifest.pages[0].sha256 = hashlib.sha256(b"other").hexdigest()
    blobs.blobs[manifest_name(manifest.document_id, manifest.revision)] = (
        manifest.model_dump_json().encode()
    )
    state = Investigation(settings, backend)
    await state.search_knowledge_base("warranty")
    with pytest.raises(ValueError, match="hash"):
        await state.search_document(manifest.document_id, "warranty")


async def test_legacy_manifest_remains_readable(settings, document, hit, caplog):
    name = manifest_name(document.document_id, document.revision)
    blobs = MemoryBlobContainer({name: document.model_dump_json().encode()})
    source = Mock(get_blob_client=Mock(return_value=Mock(get_blob_properties=AsyncMock())))
    backend = AzureEvidenceBackend(settings, Mock(), source, blobs)
    backend.retrieve = AsyncMock(return_value=[hit])
    state = Investigation(settings, backend)
    await state.search_knowledge_base("warranty")
    await state.open_pages(document.document_id, 1, 1)
    await state.search_document(document.document_id, "appeals")
    await state.open_pages(document.document_id, 4, 4)
    assert blobs.downloads == [name]
    assert len(state.evidence) == 2
    assert "legacy inline-page JSON" in caplog.text


@pytest.mark.parametrize(
    "spans",
    [
        [ContentSpan(offset=0, length=10)],
        [ContentSpan(offset=2, length=2), ContentSpan(offset=1, length=2)],
    ],
)
def test_invalid_markdown_spans_rejected(spans):
    with pytest.raises(ValueError, match="spans"):
        span_text("hello", spans)


def test_multiple_spans_preserve_unicode_and_structural_markup():
    content = "# A\U0001f600\nignore<table>value</table>"
    spans = [ContentSpan(offset=0, length=5), ContentSpan(offset=11, length=len(content) - 11)]
    assert span_text(content, spans) == "# A\U0001f600\n<table>value</table>"


async def test_missing_markdown_response_fails_before_publishing(settings):
    _, source, pages, di, search = ingestion_clients()
    result = await di.begin_analyze_document.return_value.result()
    result.content_format = "text"
    with pytest.raises(ValueError, match="must return Markdown"):
        await ingest_pdf("a.pdf", settings, source, pages, di, search)
    pages.upload_blob.assert_not_called()
    search.upload_documents.assert_not_called()


async def test_failed_page_write_does_not_publish_manifest_or_index(settings):
    _, source, pages, di, search = ingestion_clients()
    pages.upload_blob.side_effect = [None, RuntimeError("upload failed")]
    with pytest.raises(RuntimeError, match="upload failed"):
        await ingest_pdf("a.pdf", settings, source, pages, di, search)
    assert all(
        not call.kwargs["name"].endswith(".json") for call in pages.upload_blob.call_args_list
    )
    search.upload_documents.assert_not_called()


async def test_ingested_artifacts_are_readable_by_investigation(settings):
    _, source, pages, di, search = ingestion_clients("# Policy\n\nFloods are excluded.", 10)
    await ingest_pdf("policies/policy.pdf", settings, source, pages, di, search)
    blobs = MemoryBlobContainer(
        {call.kwargs["name"]: call.kwargs["data"] for call in pages.upload_blob.call_args_list}
    )
    chunks = [
        Chunk.model_validate(data) for data in search.upload_documents.call_args.kwargs["documents"]
    ]
    backend = AzureEvidenceBackend(settings, Mock(), source, blobs)
    backend.retrieve = AsyncMock(return_value=[chunks[0]])
    state = Investigation(settings, backend)
    await state.search_knowledge_base("policy")
    doc_id = chunks[0].document_id
    await state.open_pages(doc_id, 1, 1)
    assert next(iter(state.evidence.values())).text == "# Policy\n\n"
    result = json.loads(await state.search_document(doc_id, "floods"))
    assert result["matching_pages"] == [{"page_number": 2, "matched_terms": 1}]
    await state.open_pages(doc_id, 2, 2)
    assert len(blobs.downloads) == 3  # Manifest, first page, then whole-document Markdown.
    assert list(state.evidence.values())[1].text == "Floods are excluded."


async def test_manifest_contains_no_inline_page_text(markdown_backend):
    backend, _, _, hit = markdown_backend
    manifest = await backend.load_document(hit)
    assert isinstance(manifest, DocumentManifest)
    assert all("text" not in page.model_dump() for page in manifest.pages)
