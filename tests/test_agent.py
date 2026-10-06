import asyncio
from collections import deque

import pytest
from agent_framework import (
    BaseChatClient,
    ChatResponse,
    Content,
    FunctionInvocationLayer,
    Message,
)

from hierarchical_rag.agent import investigate
from hierarchical_rag.investigation import Investigation
from hierarchical_rag.models import Answer, Assessment, Citation


class ScriptedClient(FunctionInvocationLayer, BaseChatClient):
    """Exercise real MAF tool invocation without a network/model dependency."""

    def __init__(self, responses, usage_details=None):
        super().__init__(
            function_invocation_configuration={
                "max_iterations": 20,
                "allow_concurrent_invocation": False,
                "max_consecutive_errors_per_request": 1,
            }
        )
        self.responses = deque(responses)
        self.requests = []
        self.usage_details = usage_details

    async def _inner_get_response(self, *, messages, stream, options, **kwargs):
        assert not stream
        self.requests.append((messages, options))
        if not self.responses:
            raise AssertionError("Unexpected extra model call")
        return ChatResponse(
            messages=[Message("assistant", [self.responses.popleft()])],
            usage_details=self.usage_details,
        )


def tool_call(name, **arguments):
    return Content.from_function_call(
        f"call-{name}-{len(str(arguments))}", name, arguments=arguments
    )


def assessment(ids, sufficient=True, gaps=None):
    return Content.from_text(
        Assessment(
            sufficient=sufficient,
            rationale="Coverage checked",
            gaps=gaps or [],
            evidence_ids=ids,
        ).model_dump_json()
    )


def answer(eid, quote="Warranty lasts two years."):
    return Content.from_text(
        Answer(
            answer=f"Warranty lasts two years [{eid}].",
            citations=[Citation(evidence_id=eid, quote=quote)],
        ).model_dump_json()
    )


async def test_real_maf_loop_expands_then_assesses_then_writes(settings, backend, document):
    docid = document.document_id
    p1 = f"{docid}:{document.revision}:p1"
    p2 = f"{docid}:{document.revision}:p2"
    client = ScriptedClient(
        [
            tool_call("open_pages", document_id=docid, start_page=1, end_page=1),
            assessment([p1], sufficient=False, gaps=["exceptions"]),
            tool_call("open_pages", document_id=docid, start_page=2, end_page=2),
            assessment([p1, p2]),
            answer(p1),
        ],
        usage_details={"input_token_count": 100, "output_token_count": 20},
    )
    result = await investigate(
        "What warranty and exceptions apply?", Investigation(settings, backend), client
    )
    assert result.status == "answered"
    assert [page.page_number for page in result.evidence] == [1, 2]
    assert result.tool_calls == 3 and result.searches == 1
    assert not client.responses
    assert not client.requests[-1][1].get("tools")
    assert client.requests[-1][1]["response_format"] is Answer
    tool_messages = [
        message for messages, _ in client.requests for message in messages if message.role == "tool"
    ]
    assert tool_messages
    assert [event.total_tokens for event in result.usage.events] == [240, 240, 120]
    assert result.usage.reported_tokens["total_tokens"] == 600


async def test_premature_synthesis_is_blocked(settings, backend, document):
    client = ScriptedClient([assessment([f"{document.document_id}:abc123:p1"])])
    result = await investigate("warranty", Investigation(settings, backend), client)
    assert result.status == "insufficient_context"
    assert result.answer is None and len(client.requests) == 1


async def test_budget_error_inside_maf_cannot_become_answer(settings, backend, document):
    settings.max_pages = 1
    client = ScriptedClient(
        [
            tool_call("open_pages", document_id=document.document_id, start_page=1, end_page=2),
            assessment([], sufficient=False, gaps=["missing pages"]),
        ]
    )
    result = await investigate("warranty", Investigation(settings, backend), client)
    assert result.status == "budget_exhausted"
    assert result.answer is None


async def test_unknown_document_tool_error_is_explicit(settings, backend):
    client = ScriptedClient(
        [
            tool_call("open_pages", document_id="invented", start_page=1, end_page=1),
            assessment([], sufficient=False, gaps=["missing"]),
        ]
    )
    with pytest.raises(RuntimeError, match="tool execution failed"):
        await investigate("warranty", Investigation(settings, backend), client)


async def test_no_hits_does_not_call_model(settings, backend):
    backend.hits = []
    client = ScriptedClient([])
    result = await investigate("unknown topic", Investigation(settings, backend), client)
    assert result.status == "insufficient_context"
    assert not client.requests


async def test_cross_document_expansion_requires_both_documents(settings, backend, document, hit):
    other_doc = document.model_copy(update={"document_id": "other-document", "revision": "def456"})
    other_hit = hit.model_copy(
        update={
            "id": "other-hit",
            "document_id": other_doc.document_id,
            "revision": "def456",
        }
    )
    backend.documents[other_doc.document_id] = other_doc
    original_retrieve = backend.retrieve

    async def retrieve(query):
        if query == "related policy":
            return [other_hit]
        return await original_retrieve(query)

    backend.retrieve = retrieve
    p1 = f"{document.document_id}:abc123:p1"
    p2 = "other-document:def456:p1"
    client = ScriptedClient(
        [
            tool_call("open_pages", document_id=document.document_id, start_page=1, end_page=1),
            tool_call("search_knowledge_base", query="related policy"),
            tool_call("open_pages", document_id="other-document", start_page=1, end_page=1),
            assessment([p1, p2]),
            answer(p1),
        ]
    )
    result = await investigate("compare policies", Investigation(settings, backend), client)
    assert result.status == "answered"
    assert len({e.document_id for e in result.evidence}) == 2
    assert result.searches == 2 and len(result.retrieved_chunks) == 2


async def test_fabricated_quote_is_rejected_after_real_tool_call(settings, backend, document):
    eid = f"{document.document_id}:abc123:p1"
    client = ScriptedClient(
        [
            tool_call("open_pages", document_id=document.document_id, start_page=1, end_page=1),
            assessment([eid]),
            answer(eid, "Fabricated warranty text"),
        ]
    )
    with pytest.raises(ValueError, match="substring"):
        await investigate("warranty", Investigation(settings, backend), client)


async def test_wall_clock_timeout_stops_without_answer(settings, backend):
    settings.query_timeout_seconds = 1

    async def slow_retrieve(query):
        await asyncio.sleep(10)
        return []

    backend.retrieve = slow_retrieve
    result = await investigate("warranty", Investigation(settings, backend), ScriptedClient([]))
    assert result.status == "budget_exhausted"
    assert "timeout" in result.stop_reason


async def test_service_timeout_not_misreported_as_query_budget(settings, backend):
    async def timeout_retrieve(query):
        raise TimeoutError("service timed out")

    backend.retrieve = timeout_retrieve
    with pytest.raises(TimeoutError, match="service"):
        await investigate("warranty", Investigation(settings, backend), ScriptedClient([]))
