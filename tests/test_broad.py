import asyncio
import json
from unittest.mock import AsyncMock, Mock

import pytest
from agent_framework import BaseChatClient, ChatResponse, Content, FunctionInvocationLayer, Message
from conftest import MemoryBackend
from test_agent import tool_call

from hierarchical_rag.broad import investigate_broad, select_documents
from hierarchical_rag.catalog import catalog_document
from hierarchical_rag.ingestion import chunks_for
from hierarchical_rag.models import (
    Answer,
    Citation,
    CoverageAssessment,
    DocumentBrief,
    EvidenceNote,
    FacetCoverage,
    ResearchPlan,
    SearchFacet,
)
from hierarchical_rag.usage import UsageTracker


class BroadMemoryBackend(MemoryBackend):
    def __init__(self, documents):
        self.documents = {doc.document_id: doc for doc in documents}
        self.catalog = [catalog_document(doc) for doc in documents]
        self.hits = [next(chunks_for(doc, 200, 20)) for doc in documents]
        self.queries = []
        self.loads = 0
        self.active = 0
        self.max_active = 0
        self.fail = False
        self.full_downloads = 0

    async def discover_documents(self, query, limit):
        return self.catalog[:limit]

    async def retrieve_document(self, document, query, limit):
        return [hit for hit in self.hits if hit.document_id == document.document_id][:limit]

    async def load_pages(self, document, numbers):
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(0.01)
            if self.fail:
                raise RuntimeError("source unavailable")
            return await super().load_pages(document, numbers)
        finally:
            self.active -= 1

    async def load_all_pages(self, document):
        self.full_downloads += 1
        return await super().load_all_pages(document)


class BroadClient(FunctionInvocationLayer, BaseChatClient):
    """Real MAF native loops, with deterministic independent document conversations."""

    def __init__(self, documents):
        super().__init__(
            function_invocation_configuration={
                "max_iterations": 10,
                "allow_concurrent_invocation": False,
                "max_consecutive_errors_per_request": 1,
            }
        )
        self.documents = {doc.document_id: doc for doc in documents}
        self.worker_calls = {}
        self.requests = []
        self.plan = ResearchPlan(
            minimum_documents=2,
            facets=[
                SearchFacet(
                    facet_id="comparison",
                    description="Compare warranty terms",
                    query="warranty terms",
                    minimum_documents=2,
                )
            ],
        )
        self.forged_quote = False
        self.unopened = False
        self.partial = False
        self.bad_coverage = False
        self.one_citation = False
        self.writer_extra = False
        self.slow = False
        self.needed_page = 1
        self.expand_two_pages = False
        self.search_first = False

    async def _inner_get_response(self, *, messages, stream, options, **kwargs):
        assert not stream
        response_format = options["response_format"]
        self.requests.append((response_format, messages))
        if response_format is ResearchPlan:
            content = Content.from_text(self.plan.model_dump_json())
        elif response_format is DocumentBrief:
            prompt = next(
                json.loads(message.text)
                for message in messages
                if message.role == "user" and '"document_navigation"' in message.text
            )
            docid = prompt["document_navigation"]["document_id"]
            count = self.worker_calls.get(docid, 0)
            self.worker_calls[docid] = count + 1
            if self.slow:
                await asyncio.sleep(10)
            if count == 0 and self.search_first:
                content = tool_call("search_document_pages", query="exceptions and limitations")
            elif count == int(self.search_first) and not self.unopened:
                content = tool_call(
                    "open_pages",
                    document_id=docid,
                    start_page=self.needed_page,
                    end_page=2 if self.expand_two_pages else self.needed_page,
                )
            else:
                note = self.note(docid)
                if self.forged_quote:
                    note.citation.quote = "Invented evidence"
                content = Content.from_text(
                    DocumentBrief(finished=True, notes=[note], gaps=[]).model_dump_json()
                )
        elif response_format is CoverageAssessment:
            content = Content.from_text(
                CoverageAssessment(
                    facets=[
                        FacetCoverage(
                            facet_id="comparison",
                            status="partial" if self.partial else "covered",
                            evidence_ids=(
                                ["invented"]
                                if self.bad_coverage
                                else [
                                    self.note(key).citation.evidence_id for key in self.worker_calls
                                ]
                            ),
                            explanation="Comparison is limited to these two reports.",
                        )
                    ]
                ).model_dump_json()
            )
        elif response_format is Answer:
            citations = [self.note(key).citation for key in self.worker_calls]
            if self.one_citation:
                citations = citations[:1]
            if self.writer_extra:
                citations[0].quote += " See exceptions on the next page."
            content = Content.from_text(
                Answer(
                    answer="Selected reports, not exhaustive: "
                    + " ".join(f"Terms [{citation.evidence_id}]." for citation in citations),
                    citations=citations,
                ).model_dump_json()
            )
        else:
            raise AssertionError(response_format)
        return ChatResponse(
            messages=[Message("assistant", [content])],
            usage_details={"input_token_count": 100, "output_token_count": 20},
        )

    def note(self, docid):
        document = self.documents[docid]
        return EvidenceNote(
            facet_ids=["comparison"],
            claim="Warranty terms",
            citation=Citation(
                evidence_id=f"{docid}:{document.revision}:p1",
                quote=document.pages[0].text.split(".")[0] + ".",
            ),
            method="",
            limitations="Only extracted text was inspected",
        )


@pytest.fixture
def broad_fixture(document):
    second = document.model_copy(deep=True)
    second.document_id = "another-document"
    second.blob_name = "manuals/other.pdf"
    second.source_url = "https://example.blob.core.windows.net/documents/manuals/other.pdf"
    second.pages[0].text = "Warranty lasts one year. Additional conditions apply."
    documents = [document, second]
    return BroadMemoryBackend(documents), BroadClient(documents)


async def test_broad_real_loops_parallel_evidence_coverage_and_usage(settings, broad_fixture):
    backend, client = broad_fixture
    result = await investigate_broad("Compare warranty terms", settings, backend, client)
    assert result.status == "answered"
    assert result.broad.documents_cited == 2
    assert len(result.evidence) == 2
    assert backend.max_active == 2
    assert backend.full_downloads == 0
    assert result.usage.reported_tokens["total_tokens"] == 840
    assert result.usage.counters["documents_discovered"] == 2
    assert result.usage.counters["broad_searches"] == 4
    assert result.tool_calls == 4
    writer = next(messages for format_, messages in client.requests if format_ is Answer)
    payload = json.loads(next(message.text for message in writer if message.role == "user"))
    assert "evidence_metadata" in payload
    assert "See exceptions on the next page" not in json.dumps(payload)
    assert all(document.status == "investigated" for document in result.broad.documents)


async def test_concurrency_one_serializes_workers(settings, broad_fixture):
    backend, client = broad_fixture
    settings.broad_concurrency = 1
    await investigate_broad("Compare", settings, backend, client)
    assert backend.max_active == 1


async def test_workers_can_search_again_without_loading_full_documents(settings, broad_fixture):
    backend, client = broad_fixture
    client.search_first = True
    result = await investigate_broad("Compare", settings, backend, client)
    assert result.status == "answered"
    assert result.searches == 6
    assert result.tool_calls == 6
    assert backend.full_downloads == 0
    assert result.usage.reported_tokens["total_tokens"] == 1080


async def test_unopened_discovery_pages_are_not_required(settings, broad_fixture):
    backend, client = broad_fixture
    backend.hits += [list(chunks_for(doc, 200, 20))[1] for doc in backend.documents.values()]
    result = await investigate_broad("Compare", settings, backend, client)
    assert result.status == "answered"
    assert len(result.retrieved_chunks) == 4
    assert len(result.evidence) == 2


@pytest.mark.parametrize("attribute", ["forged_quote", "unopened"])
async def test_invalid_worker_evidence_fails_closed(settings, broad_fixture, attribute):
    backend, client = broad_fixture
    setattr(client, attribute, True)
    with pytest.raises(ExceptionGroup) as error:
        await investigate_broad("Compare", settings, backend, client)
    assert "exact quote" in str(error.value.exceptions[0])
    assert not any(format_ is Answer for format_, _ in client.requests)


@pytest.mark.parametrize(
    "attribute,pattern",
    [
        ("bad_coverage", "not verified"),
        ("one_citation", "fewer documents"),
        ("writer_extra", "outside its verified"),
    ],
)
async def test_invalid_coordinator_evidence_fails(settings, broad_fixture, attribute, pattern):
    backend, client = broad_fixture
    setattr(client, attribute, True)
    with pytest.raises(ValueError, match=pattern):
        await investigate_broad("Compare", settings, backend, client)


async def test_partial_coverage_prevents_synthesis(settings, broad_fixture):
    backend, client = broad_fixture
    client.partial = True
    result = await investigate_broad("Compare", settings, backend, client)
    assert result.status == "insufficient_context"
    assert result.answer is None
    assert result.assessment.gaps
    assert not any(format_ is Answer for format_, _ in client.requests)


async def test_document_limit_cannot_relax_question(settings, broad_fixture):
    backend, client = broad_fixture
    client.plan.minimum_documents = 10
    result = await investigate_broad("Compare ten reports", settings, backend, client)
    assert result.status == "budget_exhausted"
    assert not backend.queries


async def test_too_few_documents_no_worker_calls(settings, broad_fixture):
    backend, client = broad_fixture
    backend.hits = backend.hits[:1]
    backend.catalog = backend.catalog[:1]
    result = await investigate_broad("Compare", settings, backend, client)
    assert result.status == "insufficient_context"
    assert not client.worker_calls


async def test_mixed_catalog_revision_fails(settings, broad_fixture):
    backend, client = broad_fixture
    backend.catalog[0].revision = "stale"
    with pytest.raises(ValueError, match="mixed revisions"):
        await investigate_broad("Compare", settings, backend, client)


async def test_global_timeout_cancels_workers_retains_usage(settings, broad_fixture):
    backend, client = broad_fixture
    client.slow = True
    settings.broad_timeout_seconds = 1
    result = await investigate_broad("Compare", settings, backend, client)
    assert result.status == "budget_exhausted"
    assert result.usage.reported_tokens["total_tokens"] == 120
    assert [event.status for event in result.usage.events] == ["completed", "failed", "failed"]
    assert all(doc.status == "budget_exhausted" for doc in result.broad.documents)
    assert backend.active == 0


async def test_failed_worker_does_not_silently_return_partial_answer(settings, broad_fixture):
    backend, client = broad_fixture
    backend.fail = True
    usage = UsageTracker("ask")
    with pytest.raises(ExceptionGroup):
        await investigate_broad("Compare", settings, backend, client, usage=usage)
    assert not any(format_ is Answer for format_, _ in client.requests)
    assert usage.report("failed").reported_tokens["total_tokens"] >= 120
    assert backend.active == 0


async def test_global_page_budget_partitioned(settings, broad_fixture):
    backend, client = broad_fixture
    settings.broad_max_pages = 2
    result = await investigate_broad("Compare", settings, backend, client)
    assert len(result.evidence) <= 2
    assert result.usage.counters["pages_opened"] <= 2


async def test_worker_cannot_overspend_its_reserved_pages(settings, broad_fixture):
    backend, client = broad_fixture
    settings.broad_max_pages = 2
    client.expand_two_pages = True
    result = await investigate_broad("Compare", settings, backend, client)
    assert result.status == "budget_exhausted"
    assert result.usage.counters["pages_opened"] == 0
    assert result.tool_calls <= settings.broad_max_tool_calls
    assert all(doc.status == "budget_exhausted" for doc in result.broad.documents)
    assert not any(format_ is Answer for format_, _ in client.requests)


async def test_facet_minimum_is_enforced_even_when_model_claims_coverage(settings, broad_fixture):
    backend, client = broad_fixture
    client.plan.facets[0].minimum_documents = 3
    result = await investigate_broad("Compare", settings, backend, client)
    assert result.status == "insufficient_context"
    assert result.broad.coverage[0].status == "partial"
    assert result.usage.counters["facets_covered"] == 0
    assert not any(format_ is Answer for format_, _ in client.requests)


async def test_service_timeout_is_not_a_global_budget_result(settings, broad_fixture):
    backend, client = broad_fixture
    backend.discover_documents = AsyncMock(side_effect=TimeoutError("service timeout"))
    with pytest.raises(TimeoutError, match="service timeout"):
        await investigate_broad("Compare", settings, backend, client)


def test_document_selection_covers_different_facets():
    candidates = dict.fromkeys(["dominant-a", "dominant-b", "minority"])
    matches = {"dominant-a": {"a"}, "dominant-b": {"a"}, "minority": {"b"}}
    selected = select_documents(
        candidates, matches, {"dominant-a": 1, "dominant-b": 0.9, "minority": 0.1}, 2
    )
    assert selected == ["dominant-a", "minority"]


async def test_unselected_documents_reported(settings, broad_fixture):
    backend, client = broad_fixture
    third = backend.catalog[0].model_copy(update={"document_id": "not-investigated"})
    backend.catalog.append(third)
    settings.broad_max_documents = 2
    result = await investigate_broad("Compare", settings, backend, client)
    assert result.status == "answered"
    omitted = [doc for doc in result.broad.documents if not doc.selected]
    assert len(omitted) == 1
    assert omitted[0].status == "not_selected"


async def test_stale_catalog_without_chunks_fails(settings, broad_fixture):
    backend, client = broad_fixture
    backend.retrieve_document = AsyncMock(return_value=[])
    with pytest.raises(ExceptionGroup) as error:
        await investigate_broad("Compare", settings, backend, client)
    assert "no searchable chunks" in str(error.value.exceptions[0])


async def test_no_live_services_used_for_invalid_plan(settings, broad_fixture):
    backend, client = broad_fixture
    client.plan.facets.append(client.plan.facets[0])
    backend.discover_documents = Mock()
    with pytest.raises(ValueError, match="duplicate"):
        await investigate_broad("Compare", settings, backend, client)
    backend.discover_documents.assert_not_called()
