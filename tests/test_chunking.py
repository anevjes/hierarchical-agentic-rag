import json
from unittest.mock import AsyncMock, Mock

import pytest
from azure.ai.contentunderstanding.models import DocumentContent
from test_ingestion import ingestion_clients
from test_markdown_storage import MemoryBlobContainer

from hierarchical_rag.chunking import PageLayout, chunk_spans, retrieval_layout
from hierarchical_rag.ingestion import chunks_for, ingest_pdf
from hierarchical_rag.investigation import Investigation
from hierarchical_rag.models import Chunk, ContentSpan, Page
from hierarchical_rag.retrieval import AzureEvidenceBackend


def layout_content(texts, roles):
    markdown = "".join(texts)
    page_spans = []
    paragraphs = []
    offset = 0
    for number, text in enumerate(texts, 1):
        page_spans.append([ContentSpan(offset=offset, length=len(text))])
        for role, value in roles[number - 1]:
            paragraphs.append(
                {
                    "role": role,
                    "content": value,
                    "span": {"offset": offset + text.index(value), "length": len(value)},
                }
            )
        offset += len(text)
    content = DocumentContent(
        {
            "kind": "document",
            "mimeType": "application/pdf",
            "startPageNumber": 1,
            "endPageNumber": len(texts),
            "markdown": markdown,
            "paragraphs": paragraphs,
        }
    )
    return content, page_spans


def test_repeated_roles_keep_first_occurrence_and_unique_footers(document):
    texts = [
        "Survey Report\n\nNorth section.\n\nInternal use only\n1",
        "Survey Report\n\nSouth section.\n\nInternal use only\n2",
        "Survey Report\n\nEast section.\n\nEstimate only; not a measurement.\n3",
    ]
    roles = [
        [("pageHeader", "Survey Report"), ("pageFooter", "Internal use only"), ("pageNumber", "1")],
        [("pageHeader", "Survey Report"), ("pageFooter", "Internal use only"), ("pageNumber", "2")],
        [
            ("pageHeader", "Survey Report"),
            ("pageFooter", "Estimate only; not a measurement."),
            ("pageNumber", "3"),
        ],
    ]
    content, spans = layout_content(texts, roles)
    document.pages = [Page(number=i, text=text) for i, text in enumerate(texts, 1)]
    chunks = list(chunks_for(document, 2400, 200, layouts=retrieval_layout(content, spans)))
    by_page = {i: "".join(c.content for c in chunks if c.page_number == i) for i in (1, 2, 3)}
    assert "Survey Report" in by_page[1] and "Internal use only" in by_page[1]
    assert "Survey Report" not in by_page[2] and "Internal use only" not in by_page[2]
    assert "Estimate only; not a measurement." in by_page[3]
    for chunk in chunks:
        assert chunk.content in texts[chunk.page_number - 1]
        start = int(chunk.id.rsplit("-", 1)[1])
        assert texts[chunk.page_number - 1][start : start + len(chunk.content)] == chunk.content
    assert [p.text for p in document.pages] == texts


def test_markers_without_layout_roles_and_repeated_body_are_conservative(document):
    texts = [
        '<!-- PageHeader="Company" -->\nCompany\nBody A\n<!-- PageFooter="Notice" -->\n'
        '<!-- PageNumber="1" -->\n<!-- PageBreak -->\n',
        '<!-- PageHeader="Company" -->\nCompany\nBody B\n<!-- PageFooter="Notice" -->\n'
        '<!-- PageNumber="2" -->\n',
    ]
    content, spans = layout_content(texts, [[], []])
    document.pages = [Page(number=i, text=text) for i, text in enumerate(texts, 1)]
    chunks = list(chunks_for(document, 2400, 200, layouts=retrieval_layout(content, spans)))
    assert all(
        "PageNumber" not in chunk.content and "PageBreak" not in chunk.content for chunk in chunks
    )
    second = "".join(chunk.content for chunk in chunks if chunk.page_number == 2)
    assert "PageHeader" not in second and "PageFooter" not in second
    assert "Company\nBody B" in second  # Repeated body text is not boilerplate by position.
    assert any('PageFooter="Notice"' in chunk.content for chunk in chunks if chunk.page_number == 1)


def test_repeated_footnotes_and_dates_are_preserved(document):
    texts = [
        "Body\nMeasurements approximate.\n2025-01-01",
        "Body\nMeasurements approximate.\n2026-01-01",
    ]
    roles = [
        [("footnote", "Measurements approximate."), ("pageFooter", "2025-01-01")],
        [("footnote", "Measurements approximate."), ("pageFooter", "2026-01-01")],
    ]
    content, spans = layout_content(texts, roles)
    layouts = retrieval_layout(content, spans)
    assert all(not layout.excluded for layout in layouts.values())


def test_same_page_duplicates_do_not_count_as_running_header():
    content, spans = layout_content(["Title\nBody\nTitle"], [[("pageHeader", "Title")]])
    content.paragraphs.append(content.paragraphs[0])
    assert not retrieval_layout(content, spans)[1].excluded


def test_metadata_wrapper_is_not_partially_removed():
    texts = [
        '<!-- PageFooter="Report 2025" -->\nBody',
        '<!-- PageFooter="Report 2026" -->\nBody',
    ]
    content, spans = layout_content(
        texts, [[("pageFooter", "Report")], [("pageFooter", "Report")]]
    )
    assert all(not layout.excluded for layout in retrieval_layout(content, spans).values())


def test_unmapped_header_is_retained_with_warning(caplog):
    content, spans = layout_content(["Header\nBody"], [[("pageHeader", "Header")]])
    content.paragraphs[0].span = None
    assert not retrieval_layout(content, spans)[1].excluded
    assert "retaining its text" in caplog.text


def test_high_overlap_and_fitting_structure_do_not_loop_or_lose_text():
    for size in range(10, 31, 5):
        for overlap in (0, 1, size - 1):
            for prefix in (1, size - 1, size + 1):
                text = "a" * prefix + "x" * size + "z" * (size + 1)
                layout = PageLayout(protected=[ContentSpan(offset=prefix, length=size)])
                spans = list(chunk_spans(text, size, overlap, layout))
                assert all(0 < span.length <= size for span in spans)
                assert [s.offset for s in spans] == sorted({s.offset for s in spans})
                covered = {
                    i for span in spans for i in range(span.offset, span.offset + span.length)
                }
                assert covered == set(range(len(text)))
                assert any(span.offset == prefix and span.length == size for span in spans)


def test_layout_projects_unicode_and_disjoint_page_spans():
    text = "\U0001f600intro OMIT Header Body Header"
    content = DocumentContent(
        {
            "kind": "document",
            "mimeType": "application/pdf",
            "startPageNumber": 1,
            "endPageNumber": 2,
            "markdown": text,
            "paragraphs": [
                {"role": "pageHeader", "content": "Header", "span": {"offset": 12, "length": 6}},
                {"role": "pageHeader", "content": "Header", "span": {"offset": 24, "length": 6}},
            ],
        }
    )
    spans = [
        [ContentSpan(offset=0, length=6), ContentSpan(offset=12, length=12)],
        [ContentSpan(offset=24, length=6)],
    ]
    layouts = retrieval_layout(content, spans)
    assert layouts[1].boundaries == {6, 12}
    assert layouts[2].excluded == [ContentSpan(offset=0, length=6)]


def test_invalid_layout_span_fails_explicitly():
    content, spans = layout_content(["Header"], [[("pageHeader", "Header")]])
    content.paragraphs[0].span.length = 1000
    with pytest.raises(ValueError, match="layout span"):
        retrieval_layout(content, spans)


@pytest.mark.parametrize(
    "structure",
    [
        "<table><tr><td>Alpha\n\nBeta</td></tr></table>",
        "```chart\nFirst\n\nSecond\n```\n",
        "~~~mermaid\nA --> B\n\nB --> C\n~~~\n",
        '![Axes](figures/1.1 "Long generated chart description")\n',
    ],
)
def test_fitting_structures_stay_intact_with_exact_substrings(structure):
    text = "Opening paragraph.\n\n" + structure + "\n\nClosing paragraph."
    size = len(structure) + 5
    spans = list(chunk_spans(text, size, 5, PageLayout()))
    chunks = [text[s.offset : s.offset + s.length] for s in spans]
    assert any(structure in chunk for chunk in chunks)
    assert all(len(chunk) <= size for chunk in chunks)
    assert all(chunk in text for chunk in chunks)
    covered = {i for span in spans for i in range(span.offset, span.offset + span.length)}
    assert all(i in covered for i, char in enumerate(text) if not char.isspace())


def test_cu_figure_span_keeps_description_and_chart_together():
    figure = '![Axis](figures/1.1 "Description")\n\n```chart\n{"type":"bar"}\n```\n'
    text = "Intro text.\n\n" + figure + "\nAfter."
    start = text.index("![")
    layout = PageLayout(
        protected=[ContentSpan(offset=start, length=len(figure))],
        excluded=[ContentSpan(offset=start + 2, length=3)],
    )
    spans = list(chunk_spans(text, len(figure) + 2, 10, layout))
    assert any(figure in text[s.offset : s.offset + s.length] for s in spans)


def test_paragraph_and_heading_boundaries_preferred():
    text = "A" * 30 + "\n\n## Heading\n\n" + "B" * 30
    spans = list(chunk_spans(text, 40, 5, PageLayout()))
    assert spans[0].offset + spans[0].length == 32
    assert all(span.length <= 40 for span in spans)


def test_oversized_structure_is_bounded_and_coverage_is_complete(caplog):
    text = "<table>" + "x" * 500 + "</table>"
    spans = list(chunk_spans(text, 100, 20, PageLayout()))
    assert all(span.length <= 100 for span in spans)
    covered = {i for span in spans for i in range(span.offset, span.offset + span.length)}
    assert covered == set(range(len(text)))
    assert "Splitting oversized page structure" in caplog.text


def test_exclusion_does_not_join_passages_or_overlap_across_gap():
    text = "Body before FOOTER Body after"
    layout = PageLayout(excluded=[ContentSpan(offset=12, length=6)])
    spans = list(chunk_spans(text, 15, 5, layout))
    assert [text[s.offset : s.offset + s.length] for s in spans] == ["Body before ", " Body after"]


async def test_clean_index_and_unchanged_page_evidence(settings):
    first = '<!-- PageHeader="Report" -->\nNorth measurements.\n<!-- PageNumber="1" -->\n'
    second = '<!-- PageHeader="Report" -->\nSouth measurements.\n<!-- PageNumber="2" -->\n'
    _, source, pages, cu, search = ingestion_clients(first + second, len(first))
    await ingest_pdf("report.pdf", settings, source, pages, cu, search, search.embedding_client)
    uploaded = search.upload_documents.call_args.kwargs["documents"]
    assert all("PageNumber" not in row["content"] for row in uploaded)
    assert all("PageHeader" not in row["content"] for row in uploaded if row["page_number"] == 2)
    embedded = [
        value
        for call in search.embedding_client.embeddings.create.call_args_list
        for value in call.kwargs["input"]
    ]
    assert embedded == [row["content"] for row in uploaded]
    chunks = [
        Chunk.model_validate({k: v for k, v in row.items() if k != "content_vector"})
        for row in uploaded
    ]
    blobs = MemoryBlobContainer(
        {call.kwargs["name"]: call.kwargs["data"] for call in pages.upload_blob.call_args_list}
    )
    backend = AzureEvidenceBackend(settings, Mock(), source, blobs)
    backend.retrieve = AsyncMock(return_value=chunks)
    state = Investigation(settings, backend)
    await state.search_knowledge_base("measurements")
    opened = json.loads(await state.open_pages(chunks[0].document_id, 1, 2))
    assert [page["text"] for page in opened["pages"]] == [first, second]


async def test_metadata_only_document_fails_before_publishing(settings):
    first, second = '<!-- PageNumber="1" -->', '<!-- PageNumber="2" -->'
    _, source, pages, cu, search = ingestion_clients(first + second, len(first))
    with pytest.raises(ValueError, match="No searchable body content"):
        await ingest_pdf("empty.pdf", settings, source, pages, cu, search, search.embedding_client)
    pages.upload_blob.assert_not_called()
    search.upload_documents.assert_not_called()
    search.embedding_client.embeddings.create.assert_not_called()
