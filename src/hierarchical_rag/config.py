from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .usage import TokenRates


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="HRAG_", extra="ignore")

    storage_account_url: str
    source_container: str = "documents"
    pages_container: str = "document-pages"
    search_endpoint: str
    index_name: str = "page-chunks"
    knowledge_source_name: str = "page-chunks-source"
    knowledge_base_name: str = "document-investigation"
    content_understanding_endpoint: str
    content_understanding_analyzer: str = "prebuilt-documentSearch"
    content_understanding_model_deployments: dict[str, str] = Field(default_factory=dict)
    content_understanding_processing_location: Literal["geography", "dataZone", "global"] = (
        "geography"
    )
    foundry_project_endpoint: str
    model_deployment: str
    embedding_endpoint: str
    embedding_deployment: str = "text-embedding-3-large"
    embedding_model: Literal["text-embedding-3-large", "text-embedding-3-small"] = (
        "text-embedding-3-large"
    )
    embedding_dimensions: int = Field(default=3072, ge=1, le=3072)
    chunk_chars: int = Field(default=2400, ge=200, le=12000)
    chunk_overlap: int = Field(default=200, ge=0)
    max_pdf_bytes: int = Field(default=50_000_000, ge=1)
    max_document_pages: int = Field(default=500, ge=1, le=2000)
    max_tool_calls: int = Field(default=20, ge=2, le=100)
    max_searches: int = Field(default=4, ge=1, le=20)
    max_hits: int = Field(default=8, ge=1, le=50)
    max_pages: int = Field(default=24, ge=1, le=200)
    max_context_chars: int = Field(default=100_000, ge=1000)
    query_timeout_seconds: int = Field(default=180, ge=1)
    token_rates_usd_per_million: dict[str, TokenRates] = Field(default_factory=dict)
    catalog_index_name: str = "document-catalog"
    broad_max_documents: int = Field(default=6, ge=2, le=20)
    broad_concurrency: int = Field(default=3, ge=1, le=8)
    broad_max_facets: int = Field(default=4, ge=1, le=8)
    broad_candidates_per_query: int = Field(default=20, ge=2, le=50)
    broad_max_tool_calls: int = Field(default=60, ge=4, le=200)
    broad_max_searches: int = Field(default=24, ge=4, le=100)
    broad_max_pages: int = Field(default=48, ge=2, le=200)
    broad_max_context_chars: int = Field(default=150_000, ge=2000)
    broad_timeout_seconds: int = Field(default=300, ge=1)
    broad_max_evidence_records: int = Field(default=8, ge=1, le=20)
    broad_quote_chars: int = Field(default=1200, ge=100, le=4000)

    @field_validator(
        "storage_account_url",
        "search_endpoint",
        "content_understanding_endpoint",
        "foundry_project_endpoint",
        "embedding_endpoint",
    )
    @classmethod
    def https_endpoint(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme != "https" or not parsed.hostname or parsed.query or parsed.fragment:
            raise ValueError("Endpoints must be HTTPS URLs without query strings or fragments")
        if parsed.username or parsed.password:
            raise ValueError("Endpoint credentials are not supported")
        return value.rstrip("/")

    @field_validator(
        "model_deployment", "content_understanding_analyzer", "embedding_deployment"
    )
    @classmethod
    def nonempty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("A model deployment name is required")
        return value

    @field_validator("content_understanding_model_deployments")
    @classmethod
    def valid_model_mapping(cls, value: dict[str, str]) -> dict[str, str]:
        if any(not key.strip() or not deployment.strip() for key, deployment in value.items()):
            raise ValueError("Content Understanding model mappings must not contain empty names")
        return value

    @model_validator(mode="after")
    def validate_relationships(self) -> "Settings":
        if self.catalog_index_name == self.index_name:
            raise ValueError("Document catalog and chunk index names must differ")
        if self.broad_max_searches < 2 * self.broad_max_facets + self.broad_max_documents:
            raise ValueError("Broad search budget must cover discovery and one search per document")
        if (
            self.broad_max_tool_calls < 2 * self.broad_max_documents
            or self.broad_max_pages < self.broad_max_documents
            or self.broad_max_context_chars < 1000 * self.broad_max_documents
        ):
            raise ValueError("Broad budgets must permit each selected document to be investigated")
        if self.source_container == self.pages_container:
            raise ValueError("Source and derived-page containers must differ")
        if self.chunk_overlap >= self.chunk_chars:
            raise ValueError("chunk_overlap must be smaller than chunk_chars")
        if self.embedding_model == "text-embedding-3-small" and self.embedding_dimensions > 1536:
            raise ValueError("text-embedding-3-small supports at most 1536 embedding dimensions")
        return self
