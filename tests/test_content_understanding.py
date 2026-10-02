import base64
import json
from unittest.mock import Mock

import pytest
from azure.ai.contentunderstanding.aio import ContentUnderstandingClient
from azure.ai.contentunderstanding.models import AnalysisInput, AnalysisResult, DocumentFigure
from azure.core.credentials import AzureKeyCredential
from azure.core.utils import case_insensitive_dict
from test_contracts import JsonResponse, RecordingTransport
from test_ingestion import analysis_result, ingestion_clients
from test_markdown_storage import MemoryBlobContainer

from hierarchical_rag.ingestion import CONTENT_UNDERSTANDING_API_VERSION, extract_pages, ingest_pdf
from hierarchical_rag.investigation import Investigation
from hierarchical_rag.models import Chunk
from hierarchical_rag.retrieval import AzureEvidenceBackend


async def test_visual_markdown_is_indexed_cached_searched_and_opened(settings):
    figure = (
        '![W E Depth 2000 m](figures/1.1 "Generated interpretation: basement conductor '
        'beneath the eastern side; the colour legend indicates conductivity.")\n'
        '```chart\n{"type":"line","data":{"labels":["W","E"]}}\n```\n'
    )
    text = figure + "Second page: limitations of the survey."
    _, source, pages, cu, search = ingestion_clients(text, len(figure))
    result = await cu.begin_analyze.return_value.result()
    result.contents[0].figures = [
        DocumentFigure(
            {
                "id": "1.1",
                "kind": "chart",
                "description": "Generated interpretation: basement conductor",
                "source": "D(1,0,0,8,0,8,6,0,6)",
                "span": {"offset": 0, "length": len(figure)},
                "content": {"type": "line", "data": {"labels": ["W", "E"]}},
            }
        )
    ]
    await ingest_pdf("survey.pdf", settings, source, pages, cu, search)
    chunks = [
        Chunk.model_validate(value)
        for value in search.upload_documents.call_args.kwargs["documents"]
    ]
    assert "basement conductor" in chunks[0].content
    assert "```chart" in chunks[0].content
    assert chunks[0].page_number == 1
    blobs = MemoryBlobContainer(
        {call.kwargs["name"]: call.kwargs["data"] for call in pages.upload_blob.call_args_list}
    )
    backend = AzureEvidenceBackend(settings, Mock(), source, blobs)
    state = Investigation(settings, backend)
    state.hits[chunks[0].id] = chunks[0]
    matches = json.loads(await state.search_document(chunks[0].document_id, "conductor"))
    assert matches["matching_pages"] == [{"page_number": 1, "matched_terms": 1}]
    opened = json.loads(await state.open_pages(chunks[0].document_id, 1, 1))
    assert opened["pages"][0]["text"] == figure
    assert opened["pages"][0]["content_origin"] == "mixed_extraction_and_generated_visuals"
    assert opened["pages"][0]["source_url"].endswith("survey.pdf#page=1")
    assert len(blobs.downloads) == 2  # Manifest and full Markdown, no raw JSON or re-analysis.


@pytest.mark.parametrize(
    "failure,match",
    [
        ("warnings", "warnings"),
        ("encoding", "code-point"),
        ("analyzer", "analyzer"),
        ("version", "API version"),
        ("multiple", "exactly one"),
        ("empty", "exactly one"),
        ("image", "exactly one"),
        ("pagination", "non-contiguous"),
        ("range", "non-contiguous"),
        ("overlap", "overlap"),
        ("bounds", "exceed"),
        ("no-spans", "no Markdown spans"),
        ("empty-text", "no searchable"),
        ("unmapped-figure", "figure"),
        ("too-many-pages", "max_document_pages"),
    ],
)
async def test_incomplete_or_invalid_analysis_never_publishes(settings, failure, match):
    _, source, pages, cu, search = ingestion_clients()
    wire = analysis_result().as_dict()
    content = wire["contents"][0]
    if failure == "warnings":
        wire["warnings"] = [{"code": "PartialResult", "message": "Page failed"}]
    elif failure == "encoding":
        wire["stringEncoding"] = "utf16"
    elif failure == "analyzer":
        wire["analyzerId"] = "prebuilt-layout"
    elif failure == "version":
        wire["apiVersion"] = "2024-12-01-preview"
    elif failure == "multiple":
        wire["contents"].append(content.copy())
    elif failure == "empty":
        wire["contents"] = []
    elif failure == "image":
        content["kind"] = "image"
    elif failure == "pagination":
        content["pages"][1]["pageNumber"] = 3
    elif failure == "range":
        content["endPageNumber"] = 3
    elif failure == "overlap":
        content["pages"][1]["spans"][0]["offset"] = 1
    elif failure == "bounds":
        content["pages"][1]["spans"][0]["length"] = 1000
    elif failure == "no-spans":
        content["pages"][0]["spans"] = []
        content["pages"][0]["lines"] = [{"content": "Unmapped source text"}]
    elif failure == "empty-text":
        content["markdown"] = " " * len(content["markdown"])
    elif failure == "unmapped-figure":
        content["figures"] = [{"id": "1.1", "kind": "image", "description": "Lost figure"}]
    elif failure == "too-many-pages":
        settings.max_document_pages = 1
    cu.begin_analyze.return_value.result.return_value = AnalysisResult(wire)
    with pytest.raises(ValueError, match=match):
        await ingest_pdf("survey.pdf", settings, source, pages, cu, search)
    pages.upload_blob.assert_not_called()
    search.upload_documents.assert_not_called()


def test_multiple_unicode_spans_and_blank_page(settings):
    wire = analysis_result().as_dict()
    content = wire["contents"][0]
    content["markdown"] = "# A\U0001f600\nignore<table>value</table>"
    content["pages"][0]["spans"] = [
        {"offset": 0, "length": 5},
        {"offset": 11, "length": len(content["markdown"]) - 11},
    ]
    content["pages"][1]["spans"] = []
    _, pages, _ = extract_pages(AnalysisResult(wire), settings)
    assert pages[0].text == "# A\U0001f600\n<table>value</table>"
    assert pages[1].text == ""


class AnalyzeResponse(JsonResponse):
    def __init__(self, request, payload, status):
        super().__init__(request, payload, status)
        self.headers = case_insensitive_dict(self.headers)

    async def iter_bytes(self):
        yield self.payload

    async def read(self):
        return self.payload

    async def close(self):
        pass


class AnalyzeTransport(RecordingTransport):
    async def send(self, request, **kwargs):
        self.requests.append(request)
        if request.method == "POST":
            response = AnalyzeResponse(request, {"id": "test", "status": "Running"}, 202)
            response.headers["operation-location"] = (
                "https://example.services.ai.azure.com/contentunderstanding/analyzerResults/test"
                "?api-version=2025-11-01"
            )
            response.headers["retry-after"] = "0"
            return response
        return AnalyzeResponse(
            request, {"id": "test", "status": "Succeeded", "result": self.payload}, 200
        )


async def test_real_sdk_analyze_http_contract(settings):
    transport = AnalyzeTransport(analysis_result().as_dict())
    mapping = {"prebuilt-analyzer-completion-mini": "my-gpt5"}
    async with ContentUnderstandingClient(
        settings.content_understanding_endpoint,
        AzureKeyCredential("offline-test-key-not-a-secret"),
        api_version=CONTENT_UNDERSTANDING_API_VERSION,
        transport=transport,
        polling_interval=0,
    ) as client:
        poller = await client.begin_analyze(
            settings.content_understanding_analyzer,
            inputs=[AnalysisInput(data=b"%PDF-test", mime_type="application/pdf")],
            model_deployments=mapping,
            processing_location="geography",
        )
        result = await poller.result()
    request = transport.requests[0]
    assert "api-version=2025-11-01" in request.url
    assert "prebuilt-documentSearch:analyze" in request.url
    body = json.loads(request.body)
    assert "stringEncoding=codePoint" in request.url
    assert "processingLocation=geography" in request.url
    assert body["modelDeployments"] == mapping
    assert body["inputs"][0]["mimeType"] == "application/pdf"
    assert base64.b64decode(body["inputs"][0]["data"]) == b"%PDF-test"
    assert result.string_encoding == "codePoint"
    assert extract_pages(result, settings)[1][1].number == 2
