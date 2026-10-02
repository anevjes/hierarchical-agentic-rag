import json
import logging
import re
from typing import NoReturn

from .config import Settings
from .models import Answer, Assessment, Chunk, DocumentManifest, Evidence, Page, StoredDocument
from .retrieval import EvidenceBackend, validate_provenance

logger = logging.getLogger(__name__)


class BudgetExceeded(RuntimeError):
    pass


class Investigation:
    """Mutable evidence state, scoped to exactly one question and one caller."""

    def __init__(self, settings: Settings, backend: EvidenceBackend) -> None:
        self.settings = settings
        self.backend = backend
        self.tool_calls = 0
        self.searches = 0
        self.context_chars = 0
        self.hits: dict[str, Chunk] = {}
        self.documents: dict[str, StoredDocument] = {}
        self.page_cache: dict[tuple[str, int], Page] = {}
        self.evidence: dict[str, Evidence] = {}
        self.required_pages: set[tuple[str, int]] = set()
        self.queries: set[str] = set()
        self.budget_reason: str | None = None

    def _exhaust(self, reason: str) -> NoReturn:
        self.budget_reason = reason
        logger.warning("Investigation stopped: %s", reason)
        raise BudgetExceeded(reason)

    def _charge_call(self) -> None:
        if self.budget_reason:
            raise BudgetExceeded(self.budget_reason)
        if self.tool_calls >= self.settings.max_tool_calls:
            self._exhaust("Maximum tool calls reached")
        self.tool_calls += 1

    def _charge_text(self, text: str) -> None:
        if self.context_chars + len(text) > self.settings.max_context_chars:
            self._exhaust("Evidence character budget reached; no text was truncated")
        self.context_chars += len(text)

    async def search_knowledge_base(self, query: str) -> str:
        """Find IQ chunks. Open every returned hit's physical page before concluding."""
        self._charge_call()
        query = query.strip()
        if not query:
            raise ValueError("Search query must not be empty")
        if query in self.queries:
            return json.dumps({"notice": "Already searched; use existing hits or rephrase."})
        if self.searches >= self.settings.max_searches:
            self._exhaust("Maximum knowledge base searches reached")
        self.searches += 1
        hits = await self.backend.retrieve(query)
        selected = hits[: self.settings.max_hits]
        new_hits = [hit for hit in selected if hit.id not in self.hits]
        self._charge_text("".join(hit.content for hit in new_hits))
        self.queries.add(query)
        for hit in new_hits:
            previous = next(
                (item for item in self.hits.values() if item.document_id == hit.document_id),
                None,
            )
            if previous is not None and previous.revision != hit.revision:
                raise ValueError("Mixed document revisions retrieved; finish ingestion and retry")
            self.hits[hit.id] = hit
            self.required_pages.add((hit.document_id, hit.page_number))
        logger.info("IQ search %d: %d hits selected", self.searches, len(selected))
        return json.dumps(
            {
                "hits": [hit.model_dump() for hit in new_hits],
                "already_seen_ids": [hit.id for hit in selected if hit not in new_hits],
                "returned_references": len(hits),
                "selected_references": len(selected),
                "selection_limit": self.settings.max_hits,
            }
        )

    async def _document(self, doc_id: str) -> StoredDocument:
        hit = next((h for h in self.hits.values() if h.document_id == doc_id), None)
        if hit is None:
            raise ValueError("Document is not authorized by this investigation's IQ results")
        if doc_id not in self.documents:
            self.documents[doc_id] = await self.backend.load_document(hit)
        doc = self.documents[doc_id]
        self._validate_hits(doc)
        return doc

    def _validate_hits(self, doc: StoredDocument) -> None:
        for candidate in self.hits.values():
            if candidate.document_id == doc.document_id:
                validate_provenance(doc, candidate)
                page = self.page_cache.get((doc.document_id, candidate.page_number))
                if page is not None and candidate.content not in page.text:
                    raise ValueError("Retrieved chunk does not occur on its recorded physical page")

    async def _pages(
        self,
        doc: StoredDocument,
        numbers: list[int],
        *,
        full_document: bool = False,
    ) -> list[Page]:
        missing = [number for number in numbers if (doc.document_id, number) not in self.page_cache]
        if missing:
            if full_document:
                loaded = await self.backend.load_all_pages(doc)
                expected = list(range(1, len(doc.pages) + 1))
            else:
                loaded = await self.backend.load_pages(doc, missing)
                expected = missing
            if [page.number for page in loaded] != expected:
                raise ValueError("Page reader returned unexpected physical pages")
            for page in loaded:
                self.page_cache[doc.document_id, page.number] = page
        self._validate_hits(doc)
        return [self.page_cache[doc.document_id, number] for number in numbers]

    async def open_pages(self, document_id: str, start_page: int, end_page: int) -> str:
        """Read full physical PDF pages (inclusive, 1-based) from Markdown or legacy text."""
        self._charge_call()
        doc = await self._document(document_id)
        if start_page < 1 or end_page < start_page or end_page > len(doc.pages):
            raise ValueError(f"Page range must be within 1..{len(doc.pages)}")
        fresh_numbers = [
            number
            for number in range(start_page, end_page + 1)
            if self._evidence_id(doc, number) not in self.evidence
        ]
        if len(self.evidence) + len(fresh_numbers) > self.settings.max_pages:
            self._exhaust("Maximum unique opened pages reached")
        fresh = await self._pages(doc, fresh_numbers)
        self._charge_text("".join(page.text for page in fresh))
        for page in fresh:
            evidence_id = self._evidence_id(doc, page.number)
            self.evidence[evidence_id] = Evidence(
                evidence_id=evidence_id,
                document_id=doc.document_id,
                title=doc.title,
                page_number=page.number,
                source_url=f"{doc.source_url}#page={page.number}",
                source_etag=doc.source_etag,
                text=page.text,
                content_origin=(
                    "mixed_extraction_and_generated_visuals"
                    if isinstance(doc, DocumentManifest) and doc.extraction is not None
                    else "extracted_text"
                ),
            )
        return json.dumps(
            {
                "pages": [
                    self.evidence[self._evidence_id(doc, page.number)].model_dump()
                    for page in fresh
                ],
                "already_opened": [
                    self._evidence_id(doc, number)
                    for number in range(start_page, end_page + 1)
                    if number not in fresh_numbers
                ],
                "document_page_count": len(doc.pages),
            }
        )

    @staticmethod
    def _evidence_id(doc: StoredDocument, page: int) -> str:
        return f"{doc.document_id}:{doc.revision}:p{page}"

    async def search_document(self, document_id: str, query: str) -> str:
        """Locate non-adjacent pages by keywords in a PDF's persisted Markdown or legacy text."""
        self._charge_call()
        terms = set(re.findall(r"\w+", query.casefold()))
        if not terms:
            raise ValueError("Document search requires at least one word")
        doc = await self._document(document_id)
        pages = await self._pages(
            doc,
            list(range(1, len(doc.pages) + 1)),
            full_document=True,
        )
        matches = [
            (sum(term in page.text.casefold() for term in terms), page.number) for page in pages
        ]
        ranked = sorted(
            ((score, page) for score, page in matches if score),
            key=lambda item: (-item[0], item[1]),
        )
        return json.dumps(
            {
                "matching_pages": [
                    {"page_number": page, "matched_terms": score}
                    for score, page in ranked[: self.settings.max_hits]
                ],
                "total_matching_pages": len(ranked),
                "notice": "Keyword locator only. Call open_pages to obtain evidence.",
            }
        )

    def assessment_errors(self, assessment: Assessment) -> list[str]:
        errors = []
        opened = {(e.document_id, e.page_number) for e in self.evidence.values()}
        if not self.hits:
            errors.append("No knowledge-base hits were retrieved")
        if not self.required_pages.issubset(opened):
            errors.append("Not all selected hit pages have been opened")
        if not assessment.evidence_ids:
            errors.append("Assessment must identify opened evidence")
        if any(eid not in self.evidence for eid in assessment.evidence_ids):
            errors.append("Assessment cites unopened or unknown evidence")
        if assessment.sufficient and assessment.gaps:
            errors.append("Assessment declares sufficiency but still has gaps")
        return errors

    def validate_answer(self, answer: Answer, allowed_ids: list[str]) -> None:
        markers = set(re.findall(r"\[([^\[\]]+)\]", answer.answer))
        if markers != {citation.evidence_id for citation in answer.citations}:
            raise ValueError("Inline answer markers must match the citation evidence IDs")
        for citation in answer.citations:
            if citation.evidence_id not in allowed_ids or citation.evidence_id not in self.evidence:
                raise ValueError("Answer cites evidence not approved by the context assessment")
            if citation.quote not in self.evidence[citation.evidence_id].text:
                raise ValueError("Citation quote is not an exact substring of its opened page")
