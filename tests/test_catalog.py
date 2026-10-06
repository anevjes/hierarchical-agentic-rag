import argparse
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from azure.core.credentials import AzureKeyCredential
from azure.core.exceptions import HttpResponseError
from azure.search.documents.aio import SearchClient
from test_contracts import RecordingTransport
from test_ingestion import ingestion_clients

from hierarchical_rag import cli
from hierarchical_rag.broad_retrieval import AzureBroadBackend, UnindexedDocumentError
from hierarchical_rag.catalog import (
    catalog_document,
    catalog_index,
    publish_catalog,
    validate_catalog_index,
)
from hierarchical_rag.ingestion import ingest_pdf
from hierarchical_rag.models import Page
from hierarchical_rag.provision import SEARCH_API_VERSION, index_definition, provision
from hierarchical_rag.usage import UsageTracker


def test_catalog_navigation_is_extractive_bounded_and_page_mapped(document):
    document.pages = [
        Page(number=1, text="# Introduction\nScope of survey\n```json\n# Not a heading\n{}\n```"),
        Page(number=2, text="## Method\nObserved data"),
        Page(number=3, text="## Limits\nNo exact measurements"),
    ]
    entry = catalog_document(document)
    assert entry.overview_kind == "extractive_navigation"
    assert [section.heading for section in entry.sections] == ["Introduction", "Method", "Limits"]
    assert [(section.start_page, section.end_page) for section in entry.sections] == [
        (1, 2),
        (2, 3),
        (3, 3),
    ]
    assert "Not a heading" not in entry.overview
    assert "No exact measurements" in entry.overview
    assert entry.source_etag == document.source_etag


def test_overview_samples_beyond_front_matter_and_is_bounded(document):
    document.pages = [Page(number=i, text=f"Page{i}: " + "text " * 300) for i in range(1, 101)]
    entry = catalog_document(document)
    assert len(entry.overview) <= 3005
    assert "Page100:" in entry.overview
    assert "Page1:" in entry.overview


def test_catalog_schema_sdk_contract(settings):
    index = catalog_index(settings.catalog_index_name)
    validate_catalog_index(index)
    wire = index.as_dict()
    key = next(field for field in wire["fields"] if field.get("key"))
    assert key["name"] == "document_id"
    assert "vectorSearch" not in wire
    assert wire["semantic"]["defaultConfiguration"] == "document-overview"


async def test_opt_in_provision_keeps_original_resources(settings):
    client = Mock(
        get_index=AsyncMock(
            side_effect=[index_definition(settings), catalog_index(settings.catalog_index_name)]
        ),
        create_or_update_index=AsyncMock(),
        create_or_update_knowledge_source=AsyncMock(),
        create_or_update_knowledge_base=AsyncMock(),
    )
    await provision(settings, client, include_catalog=True)
    assert client.create_or_update_index.await_count == 2
    assert client.create_or_update_knowledge_base.await_count == 1


async def test_ingest_catalog_published_only_after_chunk_cleanup(settings):
    _, source, pages, cu, search = ingestion_clients()
    order = []
    original_search = search.search.side_effect

    def lookup(**kwargs):
        order.append("cleanup")
        return original_search(**kwargs) if original_search else search.search.return_value

    search.search.side_effect = lookup
    catalog = Mock()

    async def upload(**kwargs):
        order.append("catalog")
        assert search.upload_documents.await_count >= 1
        row = kwargs["documents"][0]
        assert row["document_id"] and row["revision"]
        assert json.loads(row["sections_json"]) == []
        return [Mock(succeeded=True)]

    catalog.upload_documents = AsyncMock(side_effect=upload)
    await ingest_pdf(
        "a.pdf", settings, source, pages, cu, search, search.embedding_client, catalog=catalog
    )
    assert order[-1] == "catalog"
    assert "cleanup" in order


async def test_failed_chunk_upload_never_publishes_catalog(settings):
    _, source, pages, cu, search = ingestion_clients()
    search.upload_documents.side_effect = RuntimeError("Search unavailable")
    catalog = Mock(upload_documents=AsyncMock())
    with pytest.raises(RuntimeError, match="Search unavailable"):
        await ingest_pdf(
            "a.pdf", settings, source, pages, cu, search, search.embedding_client, catalog=catalog
        )
    catalog.upload_documents.assert_not_called()


async def test_catalog_publication_failure_is_not_success(document):
    usage = UsageTracker("catalog")
    client = Mock(upload_documents=AsyncMock(return_value=[Mock(succeeded=False)]))
    with pytest.raises(RuntimeError, match="catalog publication failed"):
        await publish_catalog(document, client, usage)
    assert usage.events[0].status == "failed"
    assert usage.counters.get("catalog_documents_uploaded", 0) == 0


async def test_real_sdk_document_search_filters_and_vectorizes(settings, document, hit):
    transport = RecordingTransport({"value": [hit.model_dump()]})
    async with SearchClient(
        settings.search_endpoint,
        settings.index_name,
        AzureKeyCredential("offline-test-key-not-a-secret"),
        api_version=SEARCH_API_VERSION,
        transport=transport,
    ) as search:
        backend = AzureBroadBackend(
            settings,
            Mock(),
            Mock(),
            Mock(),
            catalog=Mock(),
            chunks=search,
            usage=UsageTracker("ask"),
        )
        assert await backend.retrieve_document(catalog_document(document), "warranty", 5) == [hit]
    body = json.loads(transport.requests[0].body)
    assert body["filter"] == (
        f"document_id eq '{document.document_id}' and revision eq '{document.revision}'"
    )
    assert body["queryType"] == "semantic"
    assert body["vectorFilterMode"] == "preFilter"
    assert body["vectorQueries"] == [
        {"kind": "text", "text": "warranty", "fields": "content_vector", "k": 50}
    ]
    assert body["minimumCoverage"] == 100
    assert body["semanticErrorHandling"] == "fail"
    assert "content_vector" not in body["select"]


async def test_real_sdk_catalog_search(settings, document):
    entry = catalog_document(document)
    row = {
        **entry.model_dump(exclude={"sections"}),
        "headings": "",
        "sections_json": "[]",
        "@search.score": 1.0,
    }
    transport = RecordingTransport({"value": [row]})
    async with SearchClient(
        settings.search_endpoint,
        settings.catalog_index_name,
        AzureKeyCredential("offline-test-key-not-a-secret"),
        api_version=SEARCH_API_VERSION,
        transport=transport,
    ) as search:
        backend = AzureBroadBackend(
            settings,
            Mock(),
            Mock(),
            Mock(),
            catalog=search,
            chunks=Mock(),
            usage=UsageTracker("ask"),
        )
        assert await backend.discover_documents("warranty", 20) == [entry]
    assert json.loads(transport.requests[0].body)["semanticConfiguration"] == "document-overview"


@pytest.mark.parametrize("method", ["catalog", "document"])
async def test_direct_search_partial_response_fails(settings, document, method):
    transport = RecordingTransport({"value": []}, status=206)
    async with SearchClient(
        settings.search_endpoint, settings.index_name,
        AzureKeyCredential("offline-test-key-not-a-secret"),
        api_version=SEARCH_API_VERSION, transport=transport,
    ) as search:
        backend = AzureBroadBackend(
            settings, Mock(), Mock(), Mock(), catalog=search, chunks=search,
            usage=UsageTracker("ask"),
        )
        with pytest.raises(HttpResponseError, match="partial retrieval"):
            if method == "catalog":
                await backend.discover_documents("warranty", 20)
            else:
                await backend.retrieve_document(catalog_document(document), "warranty", 3)


async def rows(values):
    for value in values:
        yield value


async def test_backfill_uses_stored_pages_without_analysis(settings, document, hit):
    chunks = Mock(search=AsyncMock(side_effect=[rows([hit.model_dump()]), rows([])]))
    catalog = Mock(upload_documents=AsyncMock(return_value=[Mock(succeeded=True)]))
    usage = UsageTracker("catalog")
    backend = AzureBroadBackend(
        settings, Mock(), Mock(), Mock(), catalog=catalog, chunks=chunks, usage=usage
    )
    backend.load_document = AsyncMock(return_value=document)
    backend.load_all_pages = AsyncMock(return_value=document.pages)
    await backend.backfill_catalog(document.blob_name)
    assert catalog.upload_documents.await_count == 1
    assert usage.counters["documents_completed"] == 1
    assert usage.counters["pages_catalogued"] == 4
    assert [event.stage for event in usage.events] == ["catalog_upload"]


@pytest.mark.parametrize(
    "first,second,match",
    [
        ([], [], "No indexed chunks"),
        ("hit", [{"id": "stale"}], "Multiple indexed revisions"),
    ],
)
async def test_backfill_incomplete_index_fails(settings, hit, first, second, match):
    chunks = Mock(
        search=AsyncMock(
            side_effect=[rows([hit.model_dump()] if first == "hit" else first), rows(second)]
        )
    )
    backend = AzureBroadBackend(
        settings,
        Mock(),
        Mock(),
        Mock(),
        catalog=Mock(),
        chunks=chunks,
        usage=UsageTracker("catalog"),
    )
    with pytest.raises(ValueError, match=match):
        await backend.backfill_catalog(hit.blob_name)


@pytest.mark.parametrize(
    "arguments,field",
    [
        (["ask", "compare", "--broad"], "broad"),
        (["ingest", "--catalog", "--top", "2"], "catalog"),
        (["provision", "--catalog"], "catalog"),
    ],
)
def test_opt_in_cli_flags(monkeypatch, arguments, field):
    execute = AsyncMock()
    monkeypatch.setattr(cli, "run", execute)
    monkeypatch.setattr("sys.argv", ["hrag", *arguments])
    cli.main()
    assert getattr(execute.call_args.args[0], field) is True


def test_catalog_backfill_cli_parser(monkeypatch):
    execute = AsyncMock()
    monkeypatch.setattr(cli, "run", execute)
    monkeypatch.setattr("sys.argv", ["hrag", "catalog", "--prefix", "manuals/", "--top", "20"])
    cli.main()
    args = execute.call_args.args[0]
    assert args.command == "catalog" and args.top == 20 and args.prefix == "manuals/"


async def test_broad_flag_routes_to_broad_investigator(settings, monkeypatch):
    def context(value):
        manager = AsyncMock()
        manager.__aenter__.return_value = value
        return manager

    search = Mock(get_document_count=AsyncMock(return_value=2))
    for name, value in [
        ("DefaultAzureCredential", Mock()),
        ("BlobServiceClient", Mock()),
        ("KnowledgeBaseRetrievalClient", Mock()),
        ("SearchClient", search),
    ]:
        monkeypatch.setattr(cli, name, Mock(return_value=context(value)))
    client = Mock(project_client=Mock(close=AsyncMock()), client=Mock(close=AsyncMock()))
    monkeypatch.setattr(cli, "FoundryChatClient", Mock(return_value=client))
    broad = AsyncMock(return_value=None)
    focused = AsyncMock(return_value=None)
    monkeypatch.setattr(cli, "investigate_broad", broad)
    monkeypatch.setattr(cli, "investigate", focused)
    await cli._run(
        argparse.Namespace(command="ask", question="compare", broad=True),
        settings,
        UsageTracker("ask"),
    )
    broad.assert_awaited_once()
    focused.assert_not_called()


async def test_catalog_command_never_initializes_cu_or_embeddings(settings, monkeypatch):
    def context(value):
        manager = AsyncMock()
        manager.__aenter__.return_value = value
        return manager

    for name, value in [
        ("DefaultAzureCredential", Mock()),
        ("BlobServiceClient", Mock()),
        ("KnowledgeBaseRetrievalClient", Mock()),
        ("SearchClient", Mock()),
        ("SearchIndexClient", Mock(
            get_index=AsyncMock(return_value=catalog_index(settings.catalog_index_name))
        )),
    ]:
        monkeypatch.setattr(cli, name, Mock(return_value=context(value)))
    cu = Mock()
    embeddings = Mock()
    monkeypatch.setattr(cli, "ContentUnderstandingClient", cu)
    monkeypatch.setattr(cli, "AsyncAzureOpenAI", embeddings)
    backfill = AsyncMock()
    monkeypatch.setattr(AzureBroadBackend, "backfill_catalog", backfill)
    await cli._run(
        argparse.Namespace(command="catalog", blob="one.pdf"), settings, UsageTracker("catalog")
    )
    backfill.assert_awaited_once_with("one.pdf")
    cu.assert_not_called()
    embeddings.assert_not_called()


@pytest.fixture
def catalog_batch_cli(settings, monkeypatch):
    visited = []
    names = [
        "reports/notes.txt", "reports/unindexed.pdf", "reports/one.pdf",
        "reports/another-unindexed.pdf", "reports/two.PDF", "reports/three.pdf",
    ]

    async def listing(**kwargs):
        for name in names:
            visited.append(name)
            yield SimpleNamespace(name=name)

    source = Mock(list_blobs=Mock(side_effect=listing))
    storage = Mock(get_container_client=Mock(side_effect=[source, Mock()]))

    def context(value):
        manager = AsyncMock()
        manager.__aenter__.return_value = value
        return manager

    for name, value in [
        ("DefaultAzureCredential", Mock()),
        ("BlobServiceClient", storage),
        ("KnowledgeBaseRetrievalClient", Mock()),
        ("SearchClient", Mock()),
        ("SearchIndexClient", Mock(
            get_index=AsyncMock(return_value=catalog_index(settings.catalog_index_name))
        )),
    ]:
        monkeypatch.setattr(cli, name, Mock(return_value=context(value)))
    return source, visited


async def missing_index_error(settings):
    backend = AzureBroadBackend(
        settings, Mock(), Mock(), Mock(), catalog=Mock(),
        chunks=Mock(search=AsyncMock(return_value=rows([]))), usage=UsageTracker("catalog"),
    )
    with pytest.raises(ValueError) as error:
        await backend.backfill_catalog("reports/unindexed.pdf")
    return error.value


async def test_catalog_batch_skips_unindexed_and_top_counts_successes(
    settings, monkeypatch, catalog_batch_cli, caplog
):
    source, visited = catalog_batch_cli
    missing = await missing_index_error(settings)
    backfill = AsyncMock(side_effect=[missing, None, missing, None])
    monkeypatch.setattr(AzureBroadBackend, "backfill_catalog", backfill)
    usage = UsageTracker("catalog")
    await cli._run(
        argparse.Namespace(command="catalog", blob=None, prefix="reports/", top=2),
        settings, usage,
    )
    source.list_blobs.assert_called_once_with(name_starts_with="reports/")
    assert [call.args[0] for call in backfill.await_args_list] == [
        "reports/unindexed.pdf", "reports/one.pdf", "reports/another-unindexed.pdf",
        "reports/two.PDF",
    ]
    assert visited[-1] == "reports/two.PDF"
    assert usage.counters["documents_skipped_unindexed"] == 2
    assert "Skipping unindexed PDF: reports/unindexed.pdf" in caplog.text
    assert "Skipping unindexed PDF: reports/another-unindexed.pdf" in caplog.text


@pytest.mark.parametrize("top", [20, None])
async def test_catalog_batch_accepts_fewer_indexed_documents(
    settings, monkeypatch, catalog_batch_cli, top
):
    _, visited = catalog_batch_cli
    missing = await missing_index_error(settings)
    backfill = AsyncMock(side_effect=[missing, None, missing, None, None])
    monkeypatch.setattr(AzureBroadBackend, "backfill_catalog", backfill)
    usage = UsageTracker("catalog")
    await cli._run(
        argparse.Namespace(command="catalog", blob=None, prefix="", top=top), settings, usage
    )
    assert backfill.await_count == 5
    assert visited[-1] == "reports/three.pdf"
    assert usage.counters["documents_skipped_unindexed"] == 2


async def test_catalog_batch_with_only_unindexed_pdfs_fails(
    settings, monkeypatch, catalog_batch_cli
):
    missing = await missing_index_error(settings)
    backfill = AsyncMock(side_effect=missing)
    monkeypatch.setattr(AzureBroadBackend, "backfill_catalog", backfill)
    usage = UsageTracker("catalog")
    with pytest.raises(ValueError, match="No indexed PDF blobs matched"):
        await cli._run(
            argparse.Namespace(command="catalog", blob=None, prefix="", top=20), settings, usage
        )
    assert backfill.await_count == 5
    assert usage.counters["documents_skipped_unindexed"] == 5
    assert usage.counters.get("documents_completed", 0) == 0


async def test_catalog_batch_with_no_pdfs_still_fails(
    settings, monkeypatch, catalog_batch_cli
):
    source, _ = catalog_batch_cli
    source.list_blobs.side_effect = lambda **kwargs: rows([SimpleNamespace(name="notes.txt")])
    backfill = AsyncMock()
    monkeypatch.setattr(AzureBroadBackend, "backfill_catalog", backfill)
    with pytest.raises(ValueError, match="No indexed PDF blobs matched"):
        await cli._run(
            argparse.Namespace(command="catalog", blob=None, prefix="", top=20),
            settings, UsageTracker("catalog"),
        )
    backfill.assert_not_called()


async def test_explicit_catalog_blob_remains_strict(
    settings, monkeypatch, catalog_batch_cli
):
    source, _ = catalog_batch_cli
    missing = await missing_index_error(settings)
    backfill = AsyncMock(side_effect=missing)
    monkeypatch.setattr(AzureBroadBackend, "backfill_catalog", backfill)
    usage = UsageTracker("catalog")
    with pytest.raises(UnindexedDocumentError, match="ingest this PDF first"):
        await cli._run(
            argparse.Namespace(command="catalog", blob="reports/unindexed.pdf"), settings, usage
        )
    assert "documents_skipped_unindexed" not in usage.counters
    source.list_blobs.assert_not_called()


@pytest.mark.parametrize("error", [
    ValueError("Multiple indexed revisions"),
    ValueError("Stored Markdown failed its hash check"),
    HttpResponseError("Forbidden"),
    RuntimeError("Catalog upload failed"),
])
async def test_catalog_batch_does_not_skip_real_failures(
    settings, monkeypatch, catalog_batch_cli, error, caplog
):
    _, visited = catalog_batch_cli
    backfill = AsyncMock(side_effect=error)
    monkeypatch.setattr(AzureBroadBackend, "backfill_catalog", backfill)
    usage = UsageTracker("catalog")
    with pytest.raises(type(error)) as raised:
        await cli._run(
            argparse.Namespace(command="catalog", blob=None, prefix="", top=20), settings, usage
        )
    assert raised.value is error
    assert backfill.await_count == 1
    assert len(visited) == 2
    assert "documents_skipped_unindexed" not in usage.counters
    assert "Skipping unindexed" not in caplog.text


async def test_catalog_usage_report_records_skips_and_successes(
    settings, monkeypatch, catalog_batch_cli, tmp_path, capsys
):
    missing = await missing_index_error(settings)
    monkeypatch.setattr(cli, "Settings", lambda: settings)

    async def backfill(self, blob_name):
        self.usage.increment("documents_started")
        if "unindexed" in blob_name:
            raise missing
        self.usage.increment("documents_completed")

    monkeypatch.setattr(AzureBroadBackend, "backfill_catalog", backfill)
    path = tmp_path / "catalog-usage.json"
    await cli.run(argparse.Namespace(
        command="catalog", blob=None, prefix="", top=2, usage_report=path
    ))
    report = json.loads(path.read_text())
    assert report["status"] == "completed"
    assert report["counters"] == {
        "documents_started": 4, "documents_completed": 2, "documents_skipped_unindexed": 2,
    }
    assert json.loads(capsys.readouterr().out) == report
