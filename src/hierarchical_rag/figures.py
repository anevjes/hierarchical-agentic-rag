import json
import logging

from azure.ai.contentunderstanding.models import DocumentContent

from .models import ContentSpan, Document, FigureArtifact, FigureRecord

logger = logging.getLogger(__name__)


def figure_artifact(
    document: Document,
    content: DocumentContent,
    page_spans: list[list[ContentSpan]],
    analyzer_id: str,
    api_version: str,
) -> FigureArtifact:
    records = []
    seen = set()
    for figure in content.figures or []:
        if not figure.id or figure.id in seen:
            raise ValueError("Content Understanding figure IDs must be nonempty and unique")
        seen.add(figure.id)
        span = figure.span
        if span is None or span.length <= 0:
            raise ValueError("Figure has no nonempty Markdown span")
        if span.offset < 0 or span.offset + span.length > len(content.markdown or ""):
            raise ValueError("Figure span exceeds Markdown bounds")
        numbers = [
            number for number, spans in enumerate(page_spans, 1)
            if any(
                part.offset <= span.offset
                and span.offset + span.length <= part.offset + part.length
                for part in spans
            )
        ]
        if len(numbers) != 1:
            raise ValueError("Figure must belong to exactly one physical page")
        record = FigureRecord(
            figure_id=figure.id,
            kind=figure.kind,
            page_number=numbers[0],
            source_url=f"{document.source_url}#page={numbers[0]}",
            source_region=figure.source,
            markdown_span=ContentSpan(offset=span.offset, length=span.length),
            description=figure.description,
        )
        structured = figure.as_dict().get("content")
        if figure.kind is None:
            record.warnings.append(
                "Content Understanding did not return a figure kind; preserved as unclassified. "
                "Any unclassified structured content remains in analysis.json."
            )
        if figure.kind == "chart":
            if not isinstance(structured, dict) or not structured:
                raise ValueError("CU chart figure has no structured chart content")
            json.dumps(structured, allow_nan=False)
            record.chart = structured
        elif figure.kind == "mermaid":
            if not isinstance(structured, str) or not structured.strip():
                raise ValueError("CU diagram figure has no Mermaid content")
            record.mermaid = structured
        if not figure.description or not figure.description.strip():
            record.warnings.append("Content Understanding did not return a figure description.")
        for warning in record.warnings:
            logger.warning(
                "Figure %s on page %d of %s: %s",
                figure.id, numbers[0], document.blob_name, warning,
            )
        records.append(record)
    return FigureArtifact(
        **document.model_dump(exclude={"schema_version", "pages"}),
        analyzer_id=analyzer_id,
        api_version=api_version,
        figures=records,
    )
