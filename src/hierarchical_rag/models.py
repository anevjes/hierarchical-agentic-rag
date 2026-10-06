from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, TypeAdapter


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Page(Record):
    number: int = Field(ge=1)
    text: str


class DocumentIdentity(Record):
    document_id: str
    revision: str
    blob_name: str
    source_url: str
    source_etag: str
    title: str


class Document(DocumentIdentity):
    """Legacy inline-page storage, also used as the in-memory ingestion representation."""

    schema_version: Literal[1] = 1
    pages: list[Page]


class ContentSpan(Record):
    offset: int = Field(ge=0)
    length: int = Field(ge=0)


class PageReference(Record):
    number: int = Field(ge=1)
    markdown_blob: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    content_chars: int = Field(ge=0)
    spans: list[ContentSpan]


class ArtifactReference(Record):
    blob: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class FigureRecord(Record):
    figure_id: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    page_number: int = Field(ge=1)
    source_url: str
    source_region: str | None = None
    markdown_span: ContentSpan
    description: str | None = None
    chart: dict[str, JsonValue] | None = None
    mermaid: str | None = None
    warnings: list[str] = Field(default_factory=list)


class FigureArtifact(DocumentIdentity):
    schema_version: Literal[1] = 1
    provider: Literal["azure_content_understanding"] = "azure_content_understanding"
    analyzer_id: str
    api_version: str
    generated: Literal[True] = True
    figures: list[FigureRecord]


class ExtractionMetadata(Record):
    provider: Literal["azure_content_understanding"] = "azure_content_understanding"
    analyzer_id: str
    api_version: str
    analysis_blob: str
    analysis_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    figures: ArtifactReference | None = None


class DocumentManifest(DocumentIdentity):
    schema_version: Literal[2] = 2
    content_format: Literal["markdown"] = "markdown"
    string_index_type: Literal["unicodeCodePoint"] = "unicodeCodePoint"
    markdown_blob: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    content_chars: int = Field(ge=0)
    pages: list[PageReference]
    extraction: ExtractionMetadata | None = None


StoredDocument = Document | DocumentManifest
stored_document_adapter: TypeAdapter[StoredDocument] = TypeAdapter(
    Annotated[StoredDocument, Field(discriminator="schema_version")]
)


class Chunk(Record):
    id: str
    document_id: str
    revision: str
    blob_name: str
    source_url: str
    source_etag: str
    title: str
    page_number: int = Field(ge=1)
    page_count: int = Field(ge=1)
    content: str


class Evidence(Record):
    evidence_id: str
    document_id: str
    title: str
    page_number: int
    source_url: str
    source_etag: str
    text: str
    content_origin: Literal[
        "extracted_text", "mixed_extraction_and_generated_visuals"
    ] = "extracted_text"


class Assessment(Record):
    """The investigator assesses coverage; it must not write an answer."""

    sufficient: bool
    rationale: str
    gaps: list[str]
    evidence_ids: list[str]


class Citation(Record):
    evidence_id: str
    quote: str = Field(min_length=1)


class Answer(Record):
    answer: str = Field(min_length=1)
    citations: list[Citation] = Field(min_length=1)


class InvestigationResult(Record):
    status: Literal["answered", "insufficient_context", "budget_exhausted"]
    question: str
    assessment: Assessment | None = None
    answer: Answer | None = None
    retrieved_chunks: list[Chunk]
    evidence: list[Evidence]
    tool_calls: int
    searches: int
    stop_reason: str
