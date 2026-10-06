from unittest.mock import AsyncMock, Mock

import httpx2
import pytest
from azure.core.exceptions import ResourceNotFoundError
from openai import AsyncAzureOpenAI
from openai.types import CreateEmbeddingResponse
from test_ingestion import ingestion_clients

from hierarchical_rag.config import Settings
from hierarchical_rag.embeddings import EMBEDDING_API_VERSION, embed_chunks
from hierarchical_rag.ingestion import ingest_pdf
from hierarchical_rag.provision import index_definition, provision, validate_embedding_index
from hierarchical_rag.usage import UsageTracker


def response(indices, vectors):
    return CreateEmbeddingResponse(
        model="text-embedding-3-large",
        object="list",
        usage={"prompt_tokens": 4, "total_tokens": 4},
        data=[
            {"object": "embedding", "index": index, "embedding": vector}
            for index, vector in zip(indices, vectors, strict=True)
        ],
    )


async def test_embeddings_preserve_response_alignment_and_batch_limit(settings, hit):
    settings.embedding_dimensions = 3
    calls = []

    async def create(**kwargs):
        calls.append(kwargs)
        indices = list(reversed(range(len(kwargs["input"]))))
        return response(indices, [[float(i + 1), 0.2, 0.3] for i in indices])

    client = Mock(embeddings=Mock(create=AsyncMock(side_effect=create)))
    usage = UsageTracker("ingest")
    vectors = await embed_chunks([hit] * 17, settings, client, usage=usage)
    assert len(vectors) == 17
    assert vectors[0] == vectors[16] == [1.0, 0.2, 0.3]
    assert vectors[15] == [16.0, 0.2, 0.3]
    assert [len(call["input"]) for call in calls] == [16, 1]
    assert calls[0]["input"] == [hit.content] * 16
    assert calls[0]["dimensions"] == 3
    assert calls[0]["encoding_format"] == "float"
    assert calls[0]["model"] == settings.embedding_deployment
    assert usage.report("completed").reported_tokens["input_tokens"] == 8
    assert usage.counters["chunks_embedded"] == 17
    assert len(usage.events) == 2


@pytest.mark.parametrize(
    "indices,vectors",
    [
        ([], []),
        ([1], [[1.0, 0.0, 0.0]]),
        ([0, 0], [[1.0, 0.0, 0.0]] * 2),
        ([0], [[1.0, 0.0]]),
        ([0], [[float("nan"), 0.0, 0.0]]),
        ([0], [[float("inf"), 0.0, 0.0]]),
        ([0], [[0.0, 0.0, 0.0]]),
    ],
)
async def test_invalid_vectors_fail_explicitly(settings, hit, indices, vectors):
    settings.embedding_dimensions = 3
    client = Mock(embeddings=Mock(create=AsyncMock(return_value=response(indices, vectors))))
    usage = UsageTracker("ingest")
    with pytest.raises(ValueError, match="Embedding response"):
        await embed_chunks([hit], settings, client, usage=usage)
    assert usage.report("failed").reported_tokens["input_tokens"] == 4


async def test_embedding_failure_preserves_old_index(settings):
    _, source, pages, cu, search = ingestion_clients()
    client = Mock(embeddings=Mock(create=AsyncMock(side_effect=RuntimeError("quota exceeded"))))
    with pytest.raises(RuntimeError, match="quota exceeded"):
        await ingest_pdf("test.pdf", settings, source, pages, cu, search, client)
    search.upload_documents.assert_not_called()
    search.search.assert_not_called()
    search.delete_documents.assert_not_called()


@pytest.mark.parametrize("existing", [False, True])
async def test_provision_new_or_existing_hybrid_index(settings, existing):
    client = Mock(
        get_index=AsyncMock(
            return_value=index_definition(settings),
            side_effect=None if existing else ResourceNotFoundError("new index"),
        ),
        create_or_update_index=AsyncMock(),
        create_or_update_knowledge_source=AsyncMock(),
        create_or_update_knowledge_base=AsyncMock(),
    )
    await provision(settings, client)
    client.create_or_update_index.assert_awaited_once()
    client.create_or_update_knowledge_source.assert_awaited_once()


async def test_provision_warns_text_only_migration(settings, caplog):
    index = index_definition(settings)
    index.fields = [field for field in index.fields if field.name != "content_vector"]
    client = Mock(
        get_index=AsyncMock(return_value=index),
        create_or_update_index=AsyncMock(),
        create_or_update_knowledge_source=AsyncMock(),
        create_or_update_knowledge_base=AsyncMock(),
    )
    await provision(settings, client)
    assert "existing chunks have no vectors until re-ingested" in caplog.text


@pytest.mark.parametrize("changed", ["dimensions", "deployment", "endpoint", "model"])
async def test_provision_refuses_mixing_embedding_spaces(settings, changed):
    index = index_definition(settings)
    if changed == "dimensions":
        settings.embedding_dimensions = 1536
    elif changed == "deployment":
        settings.embedding_deployment = "different"
    elif changed == "endpoint":
        settings.embedding_endpoint = "https://different.openai.azure.com"
    else:
        settings.embedding_model = "text-embedding-3-small"
    client = Mock(get_index=AsyncMock(return_value=index), create_or_update_index=AsyncMock())
    with pytest.raises(ValueError, match="incompatible embedding"):
        await provision(settings, client)
    client.create_or_update_index.assert_not_called()


def test_small_model_dimension_limit(settings):
    values = settings.model_dump()
    values["embedding_model"] = "text-embedding-3-small"
    with pytest.raises(ValueError, match="at most 1536"):
        Settings(**values, _env_file=None)


def test_ingestion_preflight_requires_vectorized_index(settings):
    index = index_definition(settings)
    index.fields = [field for field in index.fields if field.name != "content_vector"]
    with pytest.raises(ValueError, match="Run hrag provision"):
        validate_embedding_index(settings, index)


async def test_real_embedding_sdk_http_contract(settings, hit):
    import json

    settings.embedding_dimensions = 3
    requests = []

    async def handler(request):
        requests.append(request)
        return httpx2.Response(200, json=response([0], [[0.1, 0.2, 0.3]]).model_dump())

    async def token():
        return "offline-test-token"

    async with AsyncAzureOpenAI(
        azure_endpoint=settings.embedding_endpoint,
        api_version=EMBEDDING_API_VERSION,
        azure_ad_token_provider=token,
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)),
    ) as client:
        assert await embed_chunks([hit], settings, client) == [[0.1, 0.2, 0.3]]
    (request,) = requests
    assert request.url.path == f"/openai/deployments/{settings.embedding_deployment}/embeddings"
    assert request.url.params["api-version"] == EMBEDDING_API_VERSION
    assert request.headers["Authorization"] == "Bearer offline-test-token"
    assert json.loads(request.content)["input"] == [hit.content]
    assert json.loads(request.content)["dimensions"] == 3
