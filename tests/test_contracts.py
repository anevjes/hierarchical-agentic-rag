import json
from pathlib import Path
from unittest.mock import Mock

import pytest
from azure.core.credentials import AzureKeyCredential
from azure.core.exceptions import HttpResponseError
from azure.core.pipeline.transport import AsyncHttpResponse, AsyncHttpTransport
from azure.search.documents.knowledgebases.aio import KnowledgeBaseRetrievalClient
from azure.search.documents.knowledgebases.models import KnowledgeBaseRetrievalResponse

from hierarchical_rag.models import Chunk
from hierarchical_rag.provision import (
    SEARCH_API_VERSION,
    index_definition,
    knowledge_base_definition,
    knowledge_source_definition,
)
from hierarchical_rag.retrieval import AzureEvidenceBackend, parse_references, retrieval_request


def test_stable_sdk_serialization(settings):
    index = index_definition(settings).as_dict()
    assert {f["name"] for f in index["fields"]} == set(Chunk.model_fields) | {"content_vector"}
    assert all(f["retrievable"] for f in index["fields"] if f["name"] != "content_vector")
    vector = next(field for field in index["fields"] if field["name"] == "content_vector")
    assert vector["dimensions"] == settings.embedding_dimensions
    assert vector["type"] == "Collection(Edm.Single)"
    assert vector["searchable"] is True
    assert vector["retrievable"] is False and vector["stored"] is False
    profile = index["vectorSearch"]["profiles"][0]
    assert vector["vectorSearchProfile"] == profile["name"]
    assert profile["vectorizer"] == index["vectorSearch"]["vectorizers"][0]["name"]
    params = index["vectorSearch"]["vectorizers"][0]["azureOpenAIParameters"]
    assert params == {
        "resourceUri": settings.embedding_endpoint,
        "deploymentId": settings.embedding_deployment,
        "modelName": settings.embedding_model,
    }
    assert index["vectorSearch"]["algorithms"][0]["hnswParameters"]["metric"] == "cosine"
    assert index["semantic"]["defaultConfiguration"] == "page-content"
    source = knowledge_source_definition(settings).as_dict()
    assert source["kind"] == "searchIndex"
    params = source["searchIndexParameters"]
    assert params["semanticConfigurationName"] == "page-content"
    assert {f["name"] for f in params["sourceDataFields"]} == set(Chunk.model_fields)
    assert params["searchFields"] == [{"name": "content"}, {"name": "content_vector"}]
    kb = knowledge_base_definition(settings).as_dict()
    assert kb["knowledgeSources"] == [{"name": settings.knowledge_source_name}]
    assert "models" not in kb and "outputMode" not in kb
    request = retrieval_request("warranty", settings.knowledge_source_name).as_dict()
    assert request == {
        "includeActivity": True,
        "intents": [{"type": "semantic", "search": "warranty"}],
        "knowledgeSourceParams": [
            {
                "kind": "searchIndex",
                "knowledgeSourceName": settings.knowledge_source_name,
                "includeReferences": True,
                "includeReferenceSourceData": True,
            }
        ],
    }
    assert SEARCH_API_VERSION == "2026-04-01"


def response_for(hit):
    return KnowledgeBaseRetrievalResponse(
        {
            "references": [
                {
                    "type": "searchIndex",
                    "id": "0",
                    "activitySource": 0,
                    "docKey": hit.id,
                    "sourceData": hit.model_dump(),
                }
            ],
        }
    )


def test_reference_wire_deserialization(hit):
    response = response_for(hit)
    assert parse_references(response) == [hit]
    response.references.append(response.references[0])
    assert parse_references(response) == [hit]


def test_missing_source_data_is_not_empty_success(hit):
    response = response_for(hit)
    response.references[0].source_data = None
    with pytest.raises(ValueError, match="missing source data"):
        parse_references(response)


def test_wrong_reference_key(hit):
    response = response_for(hit)
    response.references[0].doc_key = "different"
    with pytest.raises(ValueError, match="key"):
        parse_references(response)


def test_partial_retrieval_error_is_fatal():
    response = KnowledgeBaseRetrievalResponse(
        {
            "activity": [
                {
                    "id": 0,
                    "type": "agenticReasoning",
                    "error": {"code": "Unavailable", "message": "source failed"},
                }
            ],
            "references": [],
        }
    )
    with pytest.raises(RuntimeError, match="activity failed"):
        parse_references(response)


def test_empty_results():
    assert parse_references(KnowledgeBaseRetrievalResponse({"references": []})) == []


class JsonResponse(AsyncHttpResponse):
    def __init__(self, request, payload, status):
        super().__init__(request, None)
        self.status_code = status
        self.headers = {"content-type": "application/json"}
        self.content_type = "application/json"
        self.payload = json.dumps(payload).encode()

    def body(self):
        return self.payload

    def json(self):
        return json.loads(self.payload)

    async def load_body(self):
        pass


class RecordingTransport(AsyncHttpTransport):
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status = status
        self.requests = []

    async def open(self):
        pass

    async def close(self):
        pass

    async def __aexit__(self, *args):
        await self.close()

    async def send(self, request, **kwargs):
        self.requests.append(request)
        return JsonResponse(request, self.payload, self.status)


async def test_sdk_retrieve_http_contract(settings, hit):
    transport = RecordingTransport(response_for(hit).as_dict())
    async with KnowledgeBaseRetrievalClient(
        endpoint=settings.search_endpoint,
        credential=AzureKeyCredential("offline-test-key-not-a-secret"),
        knowledge_base_name=settings.knowledge_base_name,
        api_version=SEARCH_API_VERSION,
        transport=transport,
    ) as client:
        backend = AzureEvidenceBackend(settings, client, Mock(), Mock())
        assert await backend.retrieve("warranty") == [hit]
    (request,) = transport.requests
    assert request.method == "POST"
    assert settings.knowledge_base_name in request.url
    assert "api-version=2026-04-01" in request.url
    assert "/retrieve" in request.url
    assert json.loads(request.body)["intents"] == [{"type": "semantic", "search": "warranty"}]


async def test_partial_http_success_is_not_grounding_success(settings, hit):
    transport = RecordingTransport(response_for(hit).as_dict(), status=206)
    async with KnowledgeBaseRetrievalClient(
        endpoint=settings.search_endpoint,
        credential=AzureKeyCredential("offline-test-key-not-a-secret"),
        knowledge_base_name=settings.knowledge_base_name,
        api_version=SEARCH_API_VERSION,
        transport=transport,
    ) as client:
        backend = AzureEvidenceBackend(settings, client, Mock(), Mock())
        with pytest.raises(HttpResponseError, match="partial retrieval"):
            await backend.retrieve("warranty")


def test_diagrams_are_valid_and_text_is_visible():
    diagrams = list((Path(__file__).parents[1] / "docs").glob("*.excalidraw"))
    assert len(diagrams) == 2
    for file in diagrams:
        diagram = json.loads(file.read_text())
        assert diagram["type"] == "excalidraw"
        elements = diagram["elements"]
        ids = {e["id"] for e in elements}
        assert len(ids) == len(elements)
        for element in elements:
            if element["type"] == "text":
                assert element["width"] > 0 and element["height"] > 0
                assert element["strokeColor"] == "#000000"
            for binding in ("startBinding", "endBinding"):
                if element.get(binding):
                    assert element[binding]["elementId"] in ids
