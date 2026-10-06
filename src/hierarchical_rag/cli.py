import argparse
import asyncio
import logging
from contextlib import AsyncExitStack
from time import perf_counter

from agent_framework.foundry import FoundryChatClient
from azure.ai.contentunderstanding.aio import ContentUnderstandingClient
from azure.core.exceptions import ResourceExistsError
from azure.identity.aio import DefaultAzureCredential, get_bearer_token_provider
from azure.search.documents.aio import SearchClient
from azure.search.documents.indexes.aio import SearchIndexClient
from azure.search.documents.knowledgebases.aio import KnowledgeBaseRetrievalClient
from azure.storage.blob.aio import BlobServiceClient
from openai import AsyncAzureOpenAI

from .agent import investigate
from .config import Settings
from .embeddings import EMBEDDING_API_VERSION
from .ingestion import CONTENT_UNDERSTANDING_API_VERSION, ingest_pdf
from .investigation import Investigation
from .provision import SEARCH_API_VERSION, provision, validate_embedding_index
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
            started = perf_counter()
            index_client = await stack.enter_async_context(
                SearchIndexClient(
                    settings.search_endpoint, credential, api_version=SEARCH_API_VERSION
                )
            )
            validate_embedding_index(settings, await index_client.get_index(settings.index_name))
            logger.info("Validated index embedding configuration before PDF analysis")
            search = await stack.enter_async_context(
                SearchClient(
                    settings.search_endpoint,
                    settings.index_name,
                    credential,
                    api_version=SEARCH_API_VERSION,
                )
            )
            intelligence = await stack.enter_async_context(
                ContentUnderstandingClient(
                    settings.content_understanding_endpoint,
                    credential,
                    api_version=CONTENT_UNDERSTANDING_API_VERSION,
                )
            )
            count = 0
            total_chunks = 0
            embeddings = await stack.enter_async_context(
                AsyncAzureOpenAI(
                    azure_endpoint=settings.embedding_endpoint,
                    api_version=EMBEDDING_API_VERSION,
                    azure_ad_token_provider=get_bearer_token_provider(
                        credential, "https://cognitiveservices.azure.com/.default"
                    ),
                )
            )
            if args.blob:
                total_chunks = await ingest_pdf(
                    args.blob, settings, source, pages, intelligence, search, embeddings
                )
                count = 1
            else:
                logger.info(
                    "Scanning source container %s for PDFs with prefix=%r, top=%s",
                    settings.source_container,
                    args.prefix,
                    args.top if args.top is not None else "unlimited",
                )
                async for blob in source.list_blobs(name_starts_with=args.prefix):
                    if not blob.name.lower().endswith(".pdf"):
                        logger.warning("Skipping non-PDF blob: %s", blob.name)
                        continue
                    logger.info("Processing PDF %d: %s", count + 1, blob.name)
                    total_chunks += await ingest_pdf(
                        blob.name, settings, source, pages, intelligence, search, embeddings
                    )
                    count += 1
                    if args.top is not None and count >= args.top:
                        logger.info("Reached --top %d document limit; stopping scan", args.top)
                        break
            if not count:
                raise ValueError("No PDF blobs matched the ingestion request")
            logger.info(
                "Ingested %d PDF(s), %d chunks in %.1fs",
                count,
                total_chunks,
                perf_counter() - started,
            )
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
    ingest.add_argument(
        "--top",
        type=int,
        metavar="N",
        help="Ingest at most N PDFs in Blob listing order (with optional --prefix, not --blob)",
    )
    ask = commands.add_parser("ask", help="Investigate, open pages, then answer with citations")
    ask.add_argument("question")
    args = parser.parse_args()
    if args.command == "ingest":
        if args.top is not None and args.top <= 0:
            parser.error("--top must be a positive integer")
        if args.top is not None and args.blob:
            parser.error("--top cannot be combined with --blob; use --prefix for multiple PDFs")
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    logging.getLogger("azure").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
