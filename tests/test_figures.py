import hashlib
import json

import pytest
from azure.ai.contentunderstanding.models import AnalysisResult, DocumentFigure
from test_ingestion import ingestion_clients

from hierarchical_rag.ingestion import ingest_pdf
from hierarchical_rag.models import DocumentManifest, FigureArtifact


def add_figure(result, *, kind="chart", description="A graph.", content=None, page=1):
    document = result.contents[0]
    span = document.pages[page - 1].spans[0]
    payload = {
        "id": f"{page}.1",
        "kind": kind,
        "span": span.as_dict(),
        "source": f"D({page},0,0,1,0,1,1,0,1)",
        "description": description,
    }
    if content is not None:
        payload["content"] = content
    document.figures = [*(document.figures or []), DocumentFigure(payload)]


async def test_chart_and_image_artifact_published_before_manifest(settings):
    first = '![Graph](figures/1.1 "Monthly readings")\n```chart\n{"type":"line"}\n```\n'
    second = '![Photo](figures/2.1 "Rock outcrop with visible layers.")'
    _, source, pages, cu, search = ingestion_clients(first + second, len(first))
    result = await cu.begin_analyze.return_value.result()
    chart = {
        "type": "line",
        "data": {"labels": ["Jan", "Feb"], "datasets": [{"label": "Readings", "data": [2, 4]}]},
        "options": {"scales": {"y": {"title": {"text": "m"}}}},
    }
    add_figure(result, description="Monthly readings", content=chart)
    add_figure(result, kind="image", description="Rock outcrop with visible layers.", page=2)
    cu.begin_analyze.return_value.result.return_value = AnalysisResult(
        json.loads(json.dumps(result.as_dict()))
    )
    await ingest_pdf("survey.pdf", settings, source, pages, cu, search, search.embedding_client)
    writes = [call.kwargs for call in pages.upload_blob.call_args_list]
    manifest = DocumentManifest.model_validate_json(writes[-1]["data"])
    reference = manifest.extraction.figures
    artifact = FigureArtifact.model_validate_json(writes[-2]["data"])
    assert writes[-2]["name"] == reference.blob
    assert reference.blob.endswith("/figures.json")
    assert writes[-2]["content_settings"].content_type == "application/json"
    assert writes[-2]["overwrite"] is False
    assert hashlib.sha256(writes[-2]["data"]).hexdigest() == reference.sha256
    assert artifact.document_id == manifest.document_id
    assert artifact.revision == manifest.revision
    assert artifact.source_etag == manifest.source_etag
    assert artifact.generated is True
    assert artifact.figures[0].chart == chart
    assert artifact.figures[0].page_number == 1
    assert artifact.figures[0].source_url.endswith("survey.pdf#page=1")
    assert artifact.figures[1].chart is None
    assert artifact.figures[1].description == "Rock outcrop with visible layers."
    assert artifact.figures[1].page_number == 2
    assert artifact.figures[1].markdown_span.offset == len(first)
    assert not any(figure.warnings for figure in artifact.figures)
    assert writes[0]["data"].decode() == first
    assert writes[1]["data"].decode() == second


async def test_mermaid_not_misrepresented_as_chart(settings):
    _, source, pages, cu, search = ingestion_clients()
    result = await cu.begin_analyze.return_value.result()
    add_figure(result, kind="mermaid", content="graph TD\n A --> B", description="A flows to B.")
    await ingest_pdf("a.pdf", settings, source, pages, cu, search, search.embedding_client)
    artifact = json.loads(pages.upload_blob.call_args_list[-2].kwargs["data"])
    assert artifact["figures"][0]["mermaid"] == "graph TD\n A --> B"
    assert artifact["figures"][0]["chart"] is None


async def test_missing_image_description_is_explicit(settings, caplog):
    _, source, pages, cu, search = ingestion_clients()
    result = await cu.begin_analyze.return_value.result()
    add_figure(result, kind="image", description=None)
    await ingest_pdf("a.pdf", settings, source, pages, cu, search, search.embedding_client)
    artifact = json.loads(pages.upload_blob.call_args_list[-2].kwargs["data"])
    assert artifact["figures"][0]["description"] is None
    assert artifact["figures"][0]["warnings"]
    assert "did not return a figure description" in caplog.text


@pytest.mark.parametrize("kind_present", [False, True])
@pytest.mark.parametrize("description", ["A geophysical cross-section.", None])
async def test_unclassified_figure_survives_sdk_and_ingestion(
    settings, caplog, kind_present, description
):
    first = '![Section](figures/1.1 "A geophysical cross-section.")\n'
    second = "Study limitations."
    _, source, pages, cu, search = ingestion_clients(first + second, len(first))
    result = await cu.begin_analyze.return_value.result()
    wire = result.as_dict()
    figure = {
        "id": "1.1",
        "span": {"offset": 0, "length": len(first)},
        "source": "D(1,0,0,1,0,1,1,0,1)",
        "description": description,
        "content": {"unclassified": "retained in raw analysis"},
    }
    if kind_present:
        figure["kind"] = None
    wire["contents"][0]["figures"] = [figure]
    cu.begin_analyze.return_value.result.return_value = AnalysisResult(wire)

    await ingest_pdf("survey.pdf", settings, source, pages, cu, search, search.embedding_client)

    writes = [call.kwargs for call in pages.upload_blob.call_args_list]
    artifact = FigureArtifact.model_validate_json(writes[-2]["data"])
    exported = artifact.figures[0]
    assert exported.kind is None
    assert json.loads(writes[-2]["data"])["figures"][0]["kind"] is None
    assert exported.description == description
    assert exported.chart is None and exported.mermaid is None
    assert exported.page_number == 1
    assert exported.source_url.endswith("survey.pdf#page=1")
    assert exported.source_region == figure["source"]
    assert exported.markdown_span.length == len(first)
    assert "preserved as unclassified" in exported.warnings[0]
    assert "Figure 1.1 on page 1 of survey.pdf" in caplog.text
    assert "did not return a figure kind" in caplog.text
    assert (len(exported.warnings) == 2) == (description is None)
    raw = json.loads(writes[3]["data"])
    assert raw["contents"][0]["figures"][0]["content"] == figure["content"]
    assert writes[0]["data"].decode() == first
    manifest = DocumentManifest.model_validate_json(writes[-1]["data"])
    assert manifest.extraction.figures.sha256 == hashlib.sha256(writes[-2]["data"]).hexdigest()
    search.upload_documents.assert_awaited_once()


@pytest.mark.parametrize(
    "failure",
    ["missing-chart", "duplicate", "nonfinite", "missing-diagram", "bad-chart", "bad-diagram"],
)
async def test_invalid_figure_never_publishes(settings, failure):
    _, source, pages, cu, search = ingestion_clients()
    result = await cu.begin_analyze.return_value.result()
    if failure == "missing-chart":
        add_figure(result)
    elif failure == "missing-diagram":
        add_figure(result, kind="mermaid")
    elif failure == "nonfinite":
        add_figure(result, content={"data": [float("nan")]})
    elif failure == "bad-chart":
        add_figure(result, content="not a chart object")
    elif failure == "bad-diagram":
        add_figure(result, kind="mermaid", content={"not": "text"})
    else:
        add_figure(result, content={"type": "bar"})
        add_figure(result, content={"type": "bar"})
    with pytest.raises(ValueError):
        await ingest_pdf("a.pdf", settings, source, pages, cu, search, search.embedding_client)
    pages.upload_blob.assert_not_called()
    search.upload_documents.assert_not_called()


async def test_failed_figures_upload_never_publishes_manifest(settings):
    _, source, pages, cu, search = ingestion_clients()
    pages.upload_blob.side_effect = [None, None, None, None, RuntimeError("figures upload failed")]
    with pytest.raises(RuntimeError, match="figures upload failed"):
        await ingest_pdf("a.pdf", settings, source, pages, cu, search, search.embedding_client)
    assert pages.upload_blob.call_args.kwargs["name"].endswith("/figures.json")
    search.upload_documents.assert_not_called()
