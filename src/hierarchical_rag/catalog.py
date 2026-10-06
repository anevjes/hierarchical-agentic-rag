import json
import re

from azure.search.documents.aio import SearchClient
from azure.search.documents.indexes.models import (
    SearchableField,
    SearchIndex,
    SemanticConfiguration,
    SemanticField,
    SemanticPrioritizedFields,
    SemanticSearch,
    SimpleField,
)
from pydantic import Field

from .models import Document, DocumentIdentity, Record
from .usage import UsageTracker

CATALOG_SEMANTIC = "document-overview"


class Section(Record):
    heading: str
    start_page: int = Field(ge=1)
    end_page: int = Field(ge=1)


class CatalogDocument(DocumentIdentity):
    page_count: int = Field(ge=1)
    overview: str
    overview_kind: str = "extractive_navigation"
    sections: list[Section] = Field(default_factory=list)


def catalog_document(document: Document) -> CatalogDocument:
    """Extract bounded navigation metadata, without another model invocation."""
    sections: list[Section] = []
    excerpts: list[str] = []
    for page in document.pages:
        in_fence = False
        prose: list[str] = []
        for line in page.text.splitlines():
            if line.lstrip().startswith(("```", "~~~")):
                in_fence = not in_fence
                continue
            if in_fence:
                continue
            heading = re.match(r"^#{1,6}\s+(.+?)\s*#*$", line)
            if heading and len(sections) < 200:
                sections.append(
                    Section(heading=heading[1][:250], start_page=page.number, end_page=page.number)
                )
            elif line.strip() and not line.lstrip().startswith(("<", "|", "![")):
                prose.append(line.strip())
        if prose:
            excerpts.append(" ".join(prose)[:500])
    for index, section in enumerate(sections):
        section.end_page = (
            max(section.start_page, sections[index + 1].start_page)
            if index + 1 < len(sections)
            else len(document.pages)
        )
    # Sample across the report rather than allowing its front matter to consume the overview.
    selected = (
        [excerpts[index * (len(excerpts) - 1) // 5] for index in range(6)]
        if len(excerpts) > 6
        else excerpts
    )
    return CatalogDocument(
        **document.model_dump(exclude={"schema_version", "pages"}),
        page_count=len(document.pages),
        overview="\n".join(selected),
        sections=sections,
    )


def catalog_index(name: str) -> SearchIndex:
    return SearchIndex(
        name=name,
        fields=[
            SimpleField(name="document_id", type="Edm.String", key=True, filterable=True),
            SimpleField(name="revision", type="Edm.String", filterable=True),
            SimpleField(name="blob_name", type="Edm.String"),
            SimpleField(name="source_url", type="Edm.String"),
            SimpleField(name="source_etag", type="Edm.String"),
            SimpleField(name="page_count", type="Edm.Int32"),
            SearchableField(name="title", type="Edm.String"),
            SearchableField(name="overview", type="Edm.String"),
            SimpleField(name="overview_kind", type="Edm.String"),
            SearchableField(name="headings", type="Edm.String"),
            SimpleField(name="sections_json", type="Edm.String"),
        ],
        semantic_search=SemanticSearch(
            default_configuration_name=CATALOG_SEMANTIC,
            configurations=[
                SemanticConfiguration(
                    name=CATALOG_SEMANTIC,
                    prioritized_fields=SemanticPrioritizedFields(
                        title_field=SemanticField(field_name="title"),
                        content_fields=[
                            SemanticField(field_name="overview"),
                            SemanticField(field_name="headings"),
                        ],
                    ),
                )
            ],
        ),
    )


def validate_catalog_index(index: SearchIndex) -> None:
    expected = catalog_index(index.name)
    actual = {field.name: field for field in index.fields}
    for field in expected.fields:
        current = actual.get(field.name)
        if (
            current is None
            or current.type != field.type
            or bool(current.key) != bool(field.key)
            or bool(current.searchable) != bool(field.searchable)
            or bool(current.filterable) != bool(field.filterable)
        ):
            raise ValueError("Incompatible document catalog index; run hrag provision --catalog")
    if not index.semantic_search or not any(
        config.name == CATALOG_SEMANTIC for config in index.semantic_search.configurations or []
    ):
        raise ValueError("Missing catalog semantic configuration; run hrag provision --catalog")


async def publish_catalog(document: Document, client: SearchClient, usage: UsageTracker) -> None:
    entry = catalog_document(document)
    with usage.operation("catalog_upload", document=document.blob_name):
        results = await client.upload_documents(
            documents=[
                {
                    **entry.model_dump(exclude={"sections"}),
                    "headings": "\n".join(section.heading for section in entry.sections),
                    "sections_json": json.dumps(
                        [section.model_dump() for section in entry.sections]
                    ),
                }
            ]
        )
        if len(results) != 1 or not results[0].succeeded:
            raise RuntimeError(
                "Chunks exist but catalog publication failed; "
                "rerun hrag catalog or ingest --catalog"
            )
    usage.increment("catalog_documents_uploaded")
