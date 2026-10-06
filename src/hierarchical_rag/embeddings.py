import logging
import math

from openai import AsyncAzureOpenAI

from .config import Settings
from .models import Chunk
from .usage import UsageTracker

logger = logging.getLogger(__name__)

EMBEDDING_API_VERSION = "2024-10-21"
VECTOR_FIELD = "content_vector"
EMBEDDING_BATCH_SIZE = 16


async def embed_chunks(
    chunks: list[Chunk], settings: Settings, client: AsyncAzureOpenAI,
    *, usage: UsageTracker | None = None,
) -> list[list[float]]:
    usage = usage or UsageTracker("ingest", settings.token_rates_usd_per_million)
    vectors = []
    for start in range(0, len(chunks), EMBEDDING_BATCH_SIZE):
        batch = chunks[start : start + EMBEDDING_BATCH_SIZE]
        if any(not chunk.content.strip() for chunk in batch):
            raise ValueError("Cannot embed an empty chunk")
        with usage.operation(
            "embedding", settings.embedding_deployment, batch[0].blob_name
        ) as event:
            response = await client.embeddings.create(
                model=settings.embedding_deployment,
                input=[chunk.content for chunk in batch],
                dimensions=settings.embedding_dimensions,
                encoding_format="float",
            )
            if response.usage is not None:
                event.record({
                    "input_token_count": response.usage.prompt_tokens,
                    "output_token_count": 0,
                    "total_token_count": response.usage.total_tokens,
                    "cache_read_input_token_count": 0,
                    "reasoning_output_token_count": 0,
                })
        usage.increment("chunks_embedded", len(batch))
        ordered = sorted(response.data, key=lambda item: item.index)
        if [item.index for item in ordered] != list(range(len(batch))):
            raise ValueError("Embedding response indices do not match the requested chunk batch")
        for item in ordered:
            vector = item.embedding
            if (
                len(vector) != settings.embedding_dimensions
                or not all(math.isfinite(value) for value in vector)
                or not any(vector)
            ):
                raise ValueError("Embedding response has invalid dimensions or vector values")
            vectors.append(vector)
        logger.debug(
            "Embedded %d/%d chunks in current index batch", start + len(batch), len(chunks)
        )
    return vectors
