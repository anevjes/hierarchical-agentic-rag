from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


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

    @field_validator(
        "storage_account_url",
        "search_endpoint",
        "content_understanding_endpoint",
        "foundry_project_endpoint",
    )
    @classmethod
    def https_endpoint(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme != "https" or not parsed.hostname or parsed.query or parsed.fragment:
            raise ValueError("Endpoints must be HTTPS URLs without query strings or fragments")
        if parsed.username or parsed.password:
            raise ValueError("Endpoint credentials are not supported")
        return value.rstrip("/")

    @field_validator("model_deployment", "content_understanding_analyzer")
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
        if self.source_container == self.pages_container:
            raise ValueError("Source and derived-page containers must differ")
        if self.chunk_overlap >= self.chunk_chars:
            raise ValueError("chunk_overlap must be smaller than chunk_chars")
        return self
