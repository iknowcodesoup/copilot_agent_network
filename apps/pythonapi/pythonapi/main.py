"""HTTP entrypoint for the pythonapi service.

Assembles the FastAPI app: builds every external client/resource once in
lifespan(), stores them on app.state, and wires up middleware and routers.
No business logic lives here - see dependencies.py, routes/, core/.
"""

import logging
from contextlib import asynccontextmanager

from cachetools import LRUCache
from fastapi import APIRouter, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from limits import parse
from limits.aio.strategies import MovingWindowRateLimiter
from limits.storage import storage_from_string
from openai import AsyncOpenAI

from pythonapi.config import settings
from pythonapi.core.document_parsing import (
    build_document_converter,
    build_hybrid_chunker,
)
from pythonapi.core.embeddings import EmbeddingClient
from pythonapi.core.generation import AnswerGenerator
from pythonapi.core.pii import PiiMasker
from pythonapi.core.reranking import CrossEncoderReranker, LexicalOverlapReranker
from pythonapi.core.voice_agent_tools import VoiceToolRegistry
from pythonapi.core.voice_events import VoiceEventStream
from pythonapi.core.voice_factory_gateway import VoiceFactoryGateway
from pythonapi.core.voice_pipeline_graph import build_voice_pipeline_graph
from pythonapi.core.voice_training_graph import build_voice_training_graph
from pythonapi.infrastructure.langfuse_client import (
    build_langfuse_client,
    close_langfuse_client,
)
from pythonapi.infrastructure.postgres_client import (
    build_postgres_engine,
    close_postgres_engine,
)
from pythonapi.infrastructure.qdrant_client import (
    build_qdrant_client,
    close_qdrant_client,
    ensure_chunk_collection,
)
from pythonapi.infrastructure.redis_client import (
    build_blocking_redis_client,
    build_redis_client,
    close_redis_client,
)
from pythonapi.infrastructure.voice_factory_client import (
    build_voice_factory_client,
    close_voice_factory_client,
)
from pythonapi.middleware.idempotency import IdempotencyMiddleware
from pythonapi.repositories.memory import InMemoryDocumentRepository
from pythonapi.repositories.orders import PostgresOrderRepository
from pythonapi.repositories.pii_vault import (
    InMemoryPiiVaultRepository,
    PostgresPiiVaultRepository,
)
from pythonapi.repositories.postgres import PostgresDocumentRepository
from pythonapi.repositories.qdrant import QdrantEmbeddingIndex
from pythonapi.repositories.voice_contributions import (
    InMemoryVoiceContributionRepository,
    PostgresVoiceContributionRepository,
)
from pythonapi.repositories.voice_runs import (
    InMemoryVoiceRunRepository,
    PostgresVoiceRunRepository,
)
from pythonapi.repositories.voices import (
    InMemoryVoiceRepository,
    PostgresVoiceRepository,
)
from pythonapi.routes import (
    agent,
    documents,
    health,
    openai_proxy,
    orders,
    search,
    voice,
    voice_factory_proxy,
    voices,
)
from pythonapi.workers.embedding_worker import EmbeddingWorkerPool
from pythonapi.workers.voice_run_reconciler import VoiceRunReconciler
from pythonapi.workers.voice_training_reconciler import VoiceTrainingReconciler

logger = logging.getLogger("uvicorn")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage runtime integrations and background resources."""
    app.state.redis = build_redis_client(settings)
    # A second pool, for the voice SSE tail read only. It parks for a whole
    # heartbeat window at a time, which the general client's socket timeout is
    # deliberately too short to allow.
    app.state.blocking_redis = build_blocking_redis_client(
        settings, block_seconds=settings.VOICE_EVENT_HEARTBEAT_SECONDS
    )
    app.state.langfuse = build_langfuse_client(settings)

    if app.state.redis is not None:
        try:
            await app.state.redis.ping()
            logger.info("Successfully connected to Redis at startup.")
        except Exception as exc:
            logger.error("Redis was unreachable at startup: %s", exc)
            # App still starts; /health reports the failure.

    # Postgres is the system of record for document/chunk metadata and
    # orders. It's optional like Redis/Langfuse: without it, documents fall
    # back to an in-memory repository and /orders returns 503.
    app.state.postgres_engine = await build_postgres_engine(settings)
    app.state.document_repository = (
        PostgresDocumentRepository(app.state.postgres_engine)
        if app.state.postgres_engine is not None
        else InMemoryDocumentRepository()
    )
    app.state.order_repository = (
        PostgresOrderRepository(app.state.postgres_engine)
        if app.state.postgres_engine is not None
        else None
    )

    # PII vault: unlike Redis/Langfuse/Postgres above, an unset key/salt does
    # not degrade to reduced functionality - it disables masking entirely,
    # so raw PII flows through unmasked. Loud warning given the higher
    # stakes versus "no idempotency"/"no tracing".
    if settings.PII_VAULT_ENCRYPTION_KEY and settings.PII_VAULT_SALT:
        app.state.pii_vault_repository = (
            PostgresPiiVaultRepository(
                app.state.postgres_engine, settings.PII_VAULT_ENCRYPTION_KEY
            )
            if app.state.postgres_engine is not None
            else InMemoryPiiVaultRepository()
        )
        app.state.pii_masker = PiiMasker(
            vault=app.state.pii_vault_repository,
            salt=settings.PII_VAULT_SALT,
            language=settings.PII_LANGUAGE,
        )
    else:
        logger.warning(
            "PII_VAULT_ENCRYPTION_KEY/PII_VAULT_SALT are unset - PII masking is "
            "DISABLED. Raw PII will be embedded, stored, and sent to the LLM."
        )
        app.state.pii_vault_repository = None
        app.state.pii_masker = None

    # Qdrant holds chunk embedding vectors only - never document/order
    # metadata - and is always available via its embedded ":memory:" mode.
    app.state.qdrant_client = build_qdrant_client(settings)
    await ensure_chunk_collection(
        app.state.qdrant_client, settings.QDRANT_COLLECTION, settings.EMBEDDING_DIM
    )
    app.state.embedding_index = QdrantEmbeddingIndex(
        app.state.qdrant_client, settings.QDRANT_COLLECTION
    )

    # Dense+sparse embedding backend. "mock" (default, tests) never touches
    # the network; "openai_compatible" talks to a real local server for
    # dense vectors and fastembed (in-process) for sparse vectors.
    app.state.openai_client = None
    sparse_model = None
    if settings.EMBEDDING_PROVIDER == "openai_compatible":
        if not settings.LLM_BASE_URL:
            raise RuntimeError(
                "LLM_BASE_URL must be set when EMBEDDING_PROVIDER=openai_compatible"
            )
        app.state.openai_client = AsyncOpenAI(
            base_url=settings.LLM_BASE_URL, api_key=settings.gateway_api_key
        )
        from fastembed import SparseTextEmbedding

        sparse_model = SparseTextEmbedding(model_name=settings.EMBEDDING_SPARSE_MODEL)

    app.state.embedding_client = EmbeddingClient(
        dim=settings.EMBEDDING_DIM,
        failure_rate=settings.EMBEDDING_FAILURE_RATE,
        max_retries=settings.EMBEDDING_MAX_RETRIES,
        base_delay=settings.EMBEDDING_RETRY_BASE_DELAY,
        max_delay=settings.EMBEDDING_RETRY_MAX_DELAY,
        provider=settings.EMBEDDING_PROVIDER,
        openai_client=app.state.openai_client,
        embedding_model=settings.EMBEDDING_MODEL,
        sparse_model=sparse_model,
    )

    # Reranking. "mock" (default, tests) is a deterministic token-overlap
    # scorer - CrossEncoderReranker is only constructed (and only downloads/
    # loads its HF model) when explicitly opted into.
    app.state.reranker = (
        CrossEncoderReranker(settings.RERANK_MODEL)
        if settings.RERANK_PROVIDER == "cross_encoder"
        else LexicalOverlapReranker()
    )
    app.state.answer_generator = AnswerGenerator(provider=settings.GENERATION_PROVIDER)

    # Docling converter/chunker are expensive (model weights) - build once,
    # reuse for every upload (see workers/embedding_worker.py).
    app.state.document_converter = build_document_converter()
    app.state.hybrid_chunker = build_hybrid_chunker()

    app.state.search_cache = LRUCache(maxsize=settings.SEARCH_CACHE_CAPACITY)
    rate_limit_storage = storage_from_string(settings.RATE_LIMIT_STORAGE_URI)
    app.state.rate_limiter = MovingWindowRateLimiter(rate_limit_storage)
    app.state.search_rate_limit = parse(settings.SEARCH_RATE_LIMIT)
    app.state.worker_pool = EmbeddingWorkerPool(
        repository=app.state.document_repository,
        embedding_client=app.state.embedding_client,
        embedding_index=app.state.embedding_index,
        document_converter=app.state.document_converter,
        hybrid_chunker=app.state.hybrid_chunker,
        pii_masker=app.state.pii_masker,
        num_workers=settings.EMBEDDING_WORKER_COUNT,
    )
    app.state.worker_pool.start()

    app.state.voice_factory_client = build_voice_factory_client(settings)
    app.state.voice_factory_gateway = (
        VoiceFactoryGateway(app.state.voice_factory_client)
        if app.state.voice_factory_client is not None
        else None
    )
    app.state.voice_run_repository = (
        PostgresVoiceRunRepository(app.state.postgres_engine)
        if app.state.postgres_engine is not None
        else InMemoryVoiceRunRepository()
    )
    # Wired unconditionally on postgres_engine, not on voice_factory_gateway:
    # creating a voice must work even without the factory configured.
    app.state.voice_repository = (
        PostgresVoiceRepository(app.state.postgres_engine)
        if app.state.postgres_engine is not None
        else InMemoryVoiceRepository()
    )
    # Wired unconditionally on postgres_engine too - the audit trail must
    # record a contribution regardless of whether the factory is configured.
    app.state.voice_contribution_repository = (
        PostgresVoiceContributionRepository(app.state.postgres_engine)
        if app.state.postgres_engine is not None
        else InMemoryVoiceContributionRepository()
    )
    # Fan-out for voice run changes. Redis is optional here as everywhere: with
    # it, a change made by any API instance reaches every browser; without it,
    # the pipeline still runs and the page falls back to a reload.
    app.state.voice_event_stream = VoiceEventStream(
        redis=app.state.redis,
        blocking_redis=app.state.blocking_redis,
        stream_key=settings.VOICE_EVENT_STREAM_KEY,
        max_length=settings.VOICE_EVENT_STREAM_MAX_LENGTH,
    )
    # What the chat agent may call. None without a voice factory, which leaves
    # the agent a plain chat agent rather than breaking it.
    app.state.voice_tool_registry = (
        VoiceToolRegistry(
            gateway=app.state.voice_factory_gateway,
            repository=app.state.voice_run_repository,
        )
        if app.state.voice_factory_gateway is not None
        else None
    )
    app.state.voice_run_reconciler = None
    app.state.voice_training_reconciler = None
    if app.state.voice_factory_gateway is not None:
        app.state.voice_run_reconciler = VoiceRunReconciler(
            repository=app.state.voice_run_repository,
            graph=build_voice_pipeline_graph(app.state.voice_factory_gateway),
            interval_seconds=settings.VOICE_RECONCILE_INTERVAL_SECONDS,
            event_stream=app.state.voice_event_stream,
            lease_seconds=settings.VOICE_LEASE_SECONDS,
            max_consecutive_errors=settings.VOICE_MAX_CONSECUTIVE_ERRORS,
            gateway=app.state.voice_factory_gateway,
        )
        app.state.voice_run_reconciler.start()
        # Independent of VoiceRunReconciler (FR21): its own graph, its own
        # lease, no shared node code. assign_run and POST /voices/{id}/train
        # both wake it directly (Story 3.3).
        app.state.voice_training_reconciler = VoiceTrainingReconciler(
            repository=app.state.voice_repository,
            graph=build_voice_training_graph(app.state.voice_factory_gateway),
            interval_seconds=settings.VOICE_TRAINING_RECONCILE_INTERVAL_SECONDS,
            lease_seconds=settings.VOICE_TRAINING_LEASE_SECONDS,
            gateway=app.state.voice_factory_gateway,
        )
        app.state.voice_training_reconciler.start()

    try:
        yield
    finally:
        if app.state.voice_run_reconciler is not None:
            await app.state.voice_run_reconciler.shutdown()
        if app.state.voice_training_reconciler is not None:
            await app.state.voice_training_reconciler.shutdown()
        if app.state.voice_factory_client is not None:
            await close_voice_factory_client(app.state.voice_factory_client)
        await app.state.worker_pool.shutdown()
        if app.state.openai_client is not None:
            await app.state.openai_client.close()
        await close_qdrant_client(app.state.qdrant_client)
        if app.state.postgres_engine is not None:
            await close_postgres_engine(app.state.postgres_engine)
        if app.state.redis is not None:
            await close_redis_client(app.state.redis)
        if app.state.blocking_redis is not None:
            await close_redis_client(app.state.blocking_redis)
        if app.state.langfuse is not None:
            close_langfuse_client(app.state.langfuse)


app = FastAPI(
    title="pythonapi",
    lifespan=lifespan,
    openapi_url="/api/openapi.json",
    docs_url="/api/docs",
    redoc_url="/api/redoc",
)
app.add_middleware(IdempotencyMiddleware, ttl_seconds=settings.IDEMPOTENCY_TTL_SECONDS)
# Added last so it sits outermost: Starlette runs the most recently added
# middleware first, and CORS has to answer preflights before anything else
# gets a chance to reject them.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_allow_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

api_router = APIRouter(prefix="/api")
api_router.include_router(health.router)
api_router.include_router(orders.router)
api_router.include_router(documents.router)
api_router.include_router(search.router)
api_router.include_router(openai_proxy.router)
api_router.include_router(agent.router)
api_router.include_router(voice.router)
api_router.include_router(voice_factory_proxy.router)
api_router.include_router(voices.router)

app.include_router(api_router)
