import json

import pytest

from hierarchical_rag.investigation import BudgetExceeded, Investigation
from hierarchical_rag.models import Answer, Assessment, Citation


async def seeded(settings, backend):
    state = Investigation(settings, backend)
    await state.search_knowledge_base("warranty")
    return state


async def test_requires_full_page_not_just_chunk(settings, backend, document):
    state = await seeded(settings, backend)
    assessment = Assessment(sufficient=True, rationale="Covered", gaps=[], evidence_ids=[])
    assert state.assessment_errors(assessment)
    result = json.loads(await state.open_pages(document.document_id, 1, 2))
    assert [p["page_number"] for p in result["pages"]] == [1, 2]
    assert result["pages"][1]["text"] == document.pages[1].text
    assert result["pages"][0]["source_url"].endswith("#page=1")
    assessment.evidence_ids = list(state.evidence)
    assert state.assessment_errors(assessment) == []


async def test_locates_nonadjacent_pages(settings, backend, document):
    state = await seeded(settings, backend)
    found = json.loads(await state.search_document(document.document_id, "appeals"))
    assert found["matching_pages"] == [{"page_number": 4, "matched_terms": 1}]
    assert not state.evidence
    await state.open_pages(document.document_id, 4, 4)
    assert next(iter(state.evidence.values())).text == document.pages[3].text


async def test_duplicate_pages_and_queries_do_not_repeat_text(settings, backend, document):
    state = await seeded(settings, backend)
    await state.open_pages(document.document_id, 1, 1)
    chars = state.context_chars
    repeated = json.loads(await state.open_pages(document.document_id, 1, 1))
    assert repeated["pages"] == []
    assert len(repeated["already_opened"]) == 1
    await state.search_knowledge_base("warranty")
    assert state.context_chars == chars
    assert state.searches == 1 and backend.loads == 1


async def test_unknown_document_blocked(settings, backend):
    state = await seeded(settings, backend)
    with pytest.raises(ValueError, match="not authorized"):
        await state.open_pages("https://attacker.example/secret", 1, 1)
    assert backend.loads == 0


@pytest.mark.parametrize("start,end", [(0, 1), (2, 1), (1, 5)])
async def test_invalid_range(settings, backend, document, start, end):
    state = await seeded(settings, backend)
    with pytest.raises(ValueError, match="range"):
        await state.open_pages(document.document_id, start, end)


async def test_page_budget_atomic_and_terminal(settings, backend, document):
    settings.max_pages = 1
    state = await seeded(settings, backend)
    chars = state.context_chars
    with pytest.raises(BudgetExceeded, match="pages"):
        await state.open_pages(document.document_id, 1, 2)
    assert not state.evidence and state.context_chars == chars
    with pytest.raises(BudgetExceeded):
        await state.open_pages(document.document_id, 1, 1)


async def test_character_budget_no_silent_truncation(settings, backend, document):
    state = await seeded(settings, backend)
    state.context_chars = settings.max_context_chars - len(document.pages[0].text)
    await state.open_pages(document.document_id, 1, 1)
    assert state.context_chars == settings.max_context_chars
    with pytest.raises(BudgetExceeded, match="character"):
        await state.open_pages(document.document_id, 2, 2)
    assert len(state.evidence) == 1


async def test_call_budget_exact(settings, backend, document):
    settings.max_tool_calls = 2
    state = await seeded(settings, backend)
    await state.open_pages(document.document_id, 1, 1)
    with pytest.raises(BudgetExceeded, match="tool calls"):
        await state.open_pages(document.document_id, 2, 2)
    assert state.tool_calls == 2


async def test_search_budget_exact(settings, backend):
    settings.max_searches = 1
    state = await seeded(settings, backend)
    with pytest.raises(BudgetExceeded, match="searches"):
        await state.search_knowledge_base("exceptions")
    assert state.searches == 1


async def test_selection_limit_is_explicit(settings, backend, hit):
    settings.max_hits = 1
    backend.hits = [hit, hit.model_copy(update={"id": "second"})]
    state = Investigation(settings, backend)
    result = json.loads(await state.search_knowledge_base("q"))
    assert result["returned_references"] == 2 and result["selected_references"] == 1
    assert len(state.hits) == 1


async def test_sufficiency_with_gaps_is_rejected(settings, backend, document):
    state = await seeded(settings, backend)
    await state.open_pages(document.document_id, 1, 1)
    assessment = Assessment(
        sufficient=True,
        rationale="Maybe",
        gaps=["exceptions"],
        evidence_ids=list(state.evidence),
    )
    assert "gaps" in " ".join(state.assessment_errors(assessment))


async def test_answer_quotes_and_inline_ids_are_validated(settings, backend, document):
    state = await seeded(settings, backend)
    await state.open_pages(document.document_id, 1, 1)
    eid = next(iter(state.evidence))
    answer = Answer(
        answer=f"Two years [{eid}].",
        citations=[Citation(evidence_id=eid, quote="Warranty lasts two years.")],
    )
    state.validate_answer(answer, [eid])
    answer.citations[0].quote = "It lasts forever."
    with pytest.raises(ValueError, match="substring"):
        state.validate_answer(answer, [eid])
    answer.answer = "Two years [invented]."
    with pytest.raises(ValueError, match="markers"):
        state.validate_answer(answer, [eid])
