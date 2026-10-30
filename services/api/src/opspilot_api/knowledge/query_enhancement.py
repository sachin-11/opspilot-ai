"""Experimental, disabled-by-default query rewrite and HyDE using the Module 2 provider router."""

from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout

from opspilot_api.config import Settings
from opspilot_api.investigations.providers import ProviderRouter
from opspilot_api.investigations.safety import redact_text
from opspilot_api.knowledge.schemas import HydePassage, QueryRewrite


class EnhancementError(Exception):
    """Experimental enhancement is disabled, over budget, or failed; never silently ignored."""


class QueryEnhancer:
    """One bounded model call per search; output is untrusted text used only as a query."""

    def __init__(self, settings: Settings, router: ProviderRouter | None = None) -> None:
        self.settings = settings
        self.router = router

    def _call(
        self, prompt_name: str, schema: type[QueryRewrite] | type[HydePassage], query: str
    ) -> str:
        if not self.settings.KNOWLEDGE_EXPERIMENTAL_QUERY_ENHANCEMENT:
            raise EnhancementError("experimental query enhancement is disabled")
        router = self.router or ProviderRouter(self.settings)
        # Redact before model use; the search query is the only context sent.
        context = {"query": redact_text(query)}
        executor = ThreadPoolExecutor(max_workers=1)
        future = executor.submit(router.generate, prompt_name, schema, context)
        try:
            result = future.result(timeout=self.settings.KNOWLEDGE_ENHANCEMENT_TIMEOUT_SECONDS)
        except FutureTimeout as exc:
            raise EnhancementError("query enhancement timed out") from exc
        except Exception as exc:
            raise EnhancementError(f"query enhancement failed: {type(exc).__name__}") from exc
        finally:
            executor.shutdown(wait=False, cancel_futures=True)
        if result.usage.output_tokens > self.settings.KNOWLEDGE_ENHANCEMENT_MAX_OUTPUT_TOKENS:
            raise EnhancementError("query enhancement exceeded the output token limit")
        if (
            result.usage.estimated_cost_usd or 0
        ) > self.settings.KNOWLEDGE_ENHANCEMENT_MAX_COST_USD:
            raise EnhancementError("query enhancement exceeded the cost limit")
        output = result.output
        if isinstance(output, QueryRewrite):
            return redact_text(output.query)
        if isinstance(output, HydePassage):
            return redact_text(output.passage)
        raise EnhancementError("query enhancement returned an unexpected shape")

    def rewrite(self, query: str) -> str:
        return self._call("knowledge_query_rewrite", QueryRewrite, query)

    def hyde(self, query: str) -> str:
        return self._call("knowledge_hyde", HydePassage, query)
