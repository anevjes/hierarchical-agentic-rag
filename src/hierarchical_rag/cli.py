import argparse
import asyncio
import logging
from contextlib import AsyncExitStack

from agent_framework.foundry import FoundryChatClient
from azure.ai.documentintelligence.aio import DocumentIntelligenceClient
from azure.core.exceptions import ResourceExistsError
from azure.identity.aio import DefaultAzureCredential
from azure.search.documents.aio import SearchClient
from azure.search.documents.indexes.aio import SearchIndexClient
from azure.search.documents.knowledgebases.aio import KnowledgeBaseRetrievalClient
from azure.storage.blob.aio import BlobServiceClient

from .agent import investigate
from .config import Settings
from .ingestion import ingest_pdf
from .investigation import Investigation
from .provision import SEARCH_API_VERSION, provision
from .retrieval import AzureEvidenceBackend

logger = logging.getLogger(__name__)


async def run(args: argparse.Namespace) -> None:
    settings = Settings()  # type: ignore[call-arg]  # Values are loaded from the environment.
    async with AsyncExitStack() as stack:
        credential = await stack.enter_async_context(DefaultAzureCredential())
        storage = await stack.enter_async_context(
            BlobServiceClient(settings.storage_account_url, credential=credential),
        )
        source = storage.get_container_client(settings.source_container)
        pages = storage.get_container_client(settings.pages_container)
        if args.command == "provision":
            await source.get_container_properties()
            try:
                await pages.create_container()
            except ResourceExistsError:
                logger.info("Derived-page container already exists")
            index_client = await stack.enter_async_context(
                SearchIndexClient(
                    settings.search_endpoint,
                    credential,
                    api_version=SEARCH_API_VERSION,
                )
            )
            await provision(settings, index_client)
            logger.info("Index, knowledge source, and knowledge base are ready")
        elif args.command == "ingest":
            search = await stack.enter_async_context(
                SearchClient(
                    settings.search_endpoint,
                    settings.index_name,
                    credential,
                    api_version=SEARCH_API_VERSION,
                )
            )
            intelligence = await stack.enter_async_context(
                DocumentIntelligenceClient(
                    settings.document_intelligence_endpoint,
                    credential,
                    api_version="2024-11-30",
                )
            )
            count = 0
            if args.blob:
                await ingest_pdf(args.blob, settings, source, pages, intelligence, search)
                count = 1
            else:
                async for blob in source.list_blobs(name_starts_with=args.prefix):
                    if not blob.name.lower().endswith(".pdf"):
                        logger.warning("Skipping non-PDF blob: %s", blob.name)
                        continue
                    await ingest_pdf(blob.name, settings, source, pages, intelligence, search)
                    count += 1
            if not count:
                raise ValueError("No PDF blobs matched the ingestion request")
            logger.info("Ingested %d PDF(s)", count)
        elif args.command == "ask":
            kb = await stack.enter_async_context(
                KnowledgeBaseRetrievalClient(
                    endpoint=settings.search_endpoint,
                    knowledge_base_name=settings.knowledge_base_name,
                    credential=credential,
                    api_version=SEARCH_API_VERSION,
                )
            )
            client = FoundryChatClient(
                project_endpoint=settings.foundry_project_endpoint,
                model=settings.model_deployment,
                credential=credential,
                function_invocation_configuration={
                    "max_iterations": settings.max_tool_calls,
                    "max_function_calls": settings.max_tool_calls,
                    "max_duration_seconds": float(settings.query_timeout_seconds),
                    "allow_concurrent_invocation": False,
                    "include_detailed_errors": False,
                    "max_consecutive_errors_per_request": 1,
                },
            )
            stack.push_async_callback(client.project_client.close)
            stack.push_async_callback(client.client.close)
            backend = AzureEvidenceBackend(settings, kb, source, pages)
            result = await investigate(args.question, Investigation(settings, backend), client)
            print(result.model_dump_json(indent=2))
        else:
            raise ValueError(f"Unknown command: {args.command}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Page-expanding Foundry IQ accelerator")
    parser.add_argument("--verbose", action="store_true")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("provision", help="Create/update Search data-plane objects")
    ingest = commands.add_parser("ingest", help="Extract and index PDFs already in Blob")
    source = ingest.add_mutually_exclusive_group()
    source.add_argument("--blob", help="Exact PDF blob name")
    source.add_argument("--prefix", default="", help="Only scan this Blob prefix")
    ask = commands.add_parser("ask", help="Investigate, open pages, then answer with citations")
    ask.add_argument("question")
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("azure").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
