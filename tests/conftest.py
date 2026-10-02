import pytest

from hierarchical_rag.config import Settings
from hierarchical_rag.ingestion import chunks_for, document_id, source_url
from hierarchical_rag.models import Document, Page


@pytest.fixture
def settings():
    return Settings(
        storage_account_url="https://example.blob.core.windows.net",
        search_endpoint="https://example.search.windows.net",
        document_intelligence_endpoint="https://example.cognitiveservices.azure.com",
        foundry_project_endpoint="https://example.services.ai.azure.com/api/projects/demo",
        model_deployment="test-model",
        _env_file=None,
    )


@pytest.fixture
def document(settings):
    url = source_url(settings, "manuals/warranty.pdf")
    return Document(
        document_id=document_id(url),
        revision="abc123",
        blob_name="manuals/warranty.pdf",
        source_url=url,
        source_etag='"etag-1"',
        title="warranty.pdf",
        pages=[
            Page(number=1, text="Warranty lasts two years. See exceptions on the next page."),
            Page(number=2, text="Exception: flood damage is not covered."),
            Page(number=3, text=""),
            Page(number=4, text="Appeals must be filed within thirty days."),
        ],
    )


@pytest.fixture
def hit(document):
    return next(chunks_for(document, 200, 20))


class MemoryBackend:
    def __init__(self, document, hits):
        self.documents = {document.document_id: document}
        self.hits = hits
        self.queries = []
        self.loads = 0

    async def retrieve(self, query):
        self.queries.append(query)
        return self.hits

    async def load_document(self, hit):
        self.loads += 1
        return self.documents[hit.document_id]

    async def load_pages(self, document, numbers):
        return [document.pages[number - 1] for number in numbers]

    async def load_all_pages(self, document):
        return document.pages


@pytest.fixture
def backend(document, hit):
    return MemoryBackend(document, [hit])
