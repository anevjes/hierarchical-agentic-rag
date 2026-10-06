import logging
import re
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field

from azure.ai.contentunderstanding.models import DocumentContent, DocumentFigure, DocumentTable

from .models import ContentSpan

logger = logging.getLogger(__name__)


@dataclass
class PageLayout:
    excluded: list[ContentSpan] = field(default_factory=list)
    protected: list[ContentSpan] = field(default_factory=list)
    boundaries: set[int] = field(default_factory=set)


def retrieval_layout(
    content: DocumentContent, page_spans: list[list[ContentSpan]]
) -> dict[int, PageLayout]:
    """Project CU layout onto stored pages, without rewriting any source text."""
    layouts = {number: PageLayout() for number in range(1, len(page_spans) + 1)}
    markdown = content.markdown
    if markdown is None:
        raise ValueError("Chunk layout requires Markdown")
    metadata = list(
        re.finditer(r'<!--\s*Page(Header|Footer)\s*=\s*"(.*?)"\s*-->', markdown, re.DOTALL)
    )

    def project(offset: int, length: int) -> list[tuple[int, ContentSpan]]:
        if offset < 0 or length < 0 or offset + length > len(markdown):
            raise ValueError("Content Understanding layout span exceeds Markdown bounds")
        mapped = []
        for number, spans in enumerate(page_spans, 1):
            local_offset = 0
            for span in spans:
                start = max(offset, span.offset)
                end = min(offset + length, span.offset + span.length)
                if end > start:
                    mapped.append(
                        (
                            number,
                            ContentSpan(
                                offset=local_offset + start - span.offset, length=end - start
                            ),
                        )
                    )
                local_offset += span.length
        return mapped

    candidates: dict[tuple[str, str], list[tuple[int, ContentSpan]]] = defaultdict(list)
    for paragraph in content.paragraphs or []:
        if paragraph.span is None:
            logger.warning("CU paragraph has no span; retaining its text without role filtering")
            continue
        mapped = project(paragraph.span.offset, paragraph.span.length)
        for number, span in mapped:
            layouts[number].boundaries.update((span.offset, span.offset + span.length))
            if paragraph.role == "footnote":
                layouts[number].protected.append(span)
        if len(mapped) != 1 or mapped[0][1].length != paragraph.span.length:
            continue
        number, span = mapped[0]
        if paragraph.role == "pageNumber" and re.fullmatch(
            r"\s*(?:\d+|[ivxlcdmIVXLCDM]+)\s*", paragraph.content
        ):
            layouts[number].excluded.append(span)
        elif paragraph.role in ("pageHeader", "pageFooter"):
            if any(
                match.start() <= paragraph.span.offset
                and paragraph.span.offset + paragraph.span.length <= match.end()
                for match in metadata
            ):
                continue
            key = (str(paragraph.role), " ".join(paragraph.content.split()))
            if key[1]:
                candidates[key].append((number, span))

    elements: list[DocumentTable | DocumentFigure] = [
        *(content.tables or []),
        *(content.figures or []),
    ]
    for element in elements:
        if element.span is None:
            logger.warning("CU table/figure has no span; using Markdown structural boundaries")
            continue
        for number, span in project(element.span.offset, element.span.length):
            layouts[number].protected.append(span)
            layouts[number].boundaries.update((span.offset, span.offset + span.length))

    for match in metadata:
        mapped = project(match.start(), match.end() - match.start())
        if len(mapped) == 1 and mapped[0][1].length == match.end() - match.start():
            key = ("page" + match.group(1), " ".join(match.group(2).split()))
            if key[1]:
                candidates[key].append(mapped[0])

    # Keep the first occurrence searchable: even repeated footer text can be meaningful.
    for entries in candidates.values():
        first_page = min(number for number, _ in entries)
        for number, span in entries:
            if number != first_page:
                layouts[number].excluded.append(span)
    return layouts


def _merge(spans: list[ContentSpan]) -> list[ContentSpan]:
    merged: list[ContentSpan] = []
    for span in sorted(spans, key=lambda item: item.offset):
        if not span.length:
            continue
        if merged and span.offset < merged[-1].offset + merged[-1].length:
            previous = merged[-1]
            previous.length = max(previous.length, span.offset + span.length - previous.offset)
        else:
            merged.append(span.model_copy())
    return merged


def chunk_spans(text: str, size: int, overlap: int, layout: PageLayout) -> Iterable[ContentSpan]:
    """Yield contiguous source slices; overlap never crosses an excluded region."""
    protected = list(layout.protected)
    # These are CU Markdown structures, not a general-purpose Markdown parser.
    for match in re.finditer(
        r"<table\b[^>]*>.*?</table>"
        r"|^```[^\n]*\n.*?^```[ \t]*(?:\n|$)"
        r"|^~~~[^\n]*\n.*?^~~~[ \t]*(?:\n|$)"
        r"|^!\[[^\n]*\n?",
        text,
        re.MULTILINE | re.DOTALL,
    ):
        protected.append(ContentSpan(offset=match.start(), length=match.end() - match.start()))
    protected = _merge(protected)
    for block in protected:
        if block.length > size:
            logger.warning(
                "Splitting oversized page structure: %d characters exceeds chunk limit %d",
                block.length,
                size,
            )
    excluded = list(layout.excluded)
    for match in re.finditer(r'<!--\s*(?:PageBreak|PageNumber\s*=\s*"[^"]*")\s*-->', text):
        excluded.append(ContentSpan(offset=match.start(), length=match.end() - match.start()))
    # Header/footer roles never override a figure, table, code block or footnote.
    excluded = [
        span
        for span in excluded
        if not any(
            span.offset < block.offset + block.length and block.offset < span.offset + span.length
            for block in protected
        )
    ]
    excluded = _merge(excluded)
    logger.debug(
        "Retrieval chunk filtering: %d source ranges, %d characters excluded; evidence unchanged",
        len(excluded),
        sum(span.length for span in excluded),
    )
    boundaries = set(layout.boundaries)
    boundaries.update(match.end() for match in re.finditer(r"\n[ \t]*\n", text))
    boundaries.update(match.start() for match in re.finditer(r"^#{1,6}[ \t]+", text, re.MULTILINE))
    for block in protected:
        boundaries.update((block.offset, block.offset + block.length))
    boundaries = {
        position
        for position in boundaries
        if not any(block.offset < position < block.offset + block.length for block in protected)
    }

    cursor = 0
    regions = []
    for span in excluded:
        if span.offset < 0 or span.offset + span.length > len(text):
            raise ValueError("Excluded chunk span exceeds its physical page")
        if cursor < span.offset:
            regions.append((cursor, span.offset))
        cursor = span.offset + span.length
    if cursor < len(text):
        regions.append((cursor, len(text)))

    for region_start, region_end in regions:
        start = region_start
        while start < region_end:
            hard_end = min(start + size, region_end)
            end = hard_end
            if hard_end < region_end:
                # A structure that fits in a chunk should never be cut to fill the previous one.
                containing = next(
                    (
                        block
                        for block in protected
                        if block.offset < hard_end < block.offset + block.length
                        and block.length <= size
                    ),
                    None,
                )
                if containing is not None:
                    end = (
                        containing.offset
                        if containing.offset > start
                        else (containing.offset + containing.length)
                    )
                else:
                    useful = [
                        position for position in boundaries if start + overlap < position <= end
                    ]
                    if useful:
                        end = max(useful)
            if text[start:end].strip():
                yield ContentSpan(offset=start, length=end - start)
            if end == region_end:
                break
            next_start = max(start + 1, end - overlap)
            for block in protected:
                if block.length <= size and block.offset < next_start < block.offset + block.length:
                    next_start = (
                        block.offset if block.offset > start else block.offset + block.length
                    )
                    break
            # Avoid emitting an overlap-only chunk before a protected block.
            if any(block.offset == end and block.length <= size for block in protected):
                next_start = end
            start = next_start
