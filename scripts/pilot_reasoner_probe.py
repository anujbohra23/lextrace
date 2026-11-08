"""One-call local Ollama M6 probe over isolated synthetic reviewed records.

This bypasses document extraction and workflow approvals. It never persists an
investigation finding or calls the independent verifier.
"""

import argparse
import json
import tempfile
from pathlib import Path

from lextrace.investigations.findings import InvestigationBudgets
from lextrace.investigations.repository import InvestigationRepository
from lextrace.matter.store import MatterStore
from lextrace.research.contracts import ResearchError, Usage
from lextrace.research.llm import Output
from lextrace.research.ollama import OllamaLLM
from lextrace.research.prompts import PromptDefinition
from tests.unit.test_inconsistency_investigation import Runtime


class SkipVerifier:
    provider = "probe-skip"
    model = "probe-skip"
    retries = 0
    usage = Usage()

    def generate(
        self,
        prompt: PromptDefinition,
        schema: type[Output],
        context: dict[str, object],
    ) -> Output:
        raise ResearchError("Verifier skipped in reasoner-only probe.")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="qwen3:8b")
    args = parser.parse_args()
    provider = OllamaLLM(
        args.model,
        timeout=180,
        max_retries=0,
        max_completion_tokens=8192,
        context_tokens=16384,
    )
    try:
        with tempfile.TemporaryDirectory(prefix="lextrace-reasoner-probe-") as raw:
            root = Path(raw)
            store = MatterStore(root / "matter.sqlite", root / "private")
            repository = InvestigationRepository(
                root / "matter.sqlite", source_store=store
            )
            runtime = Runtime(store, repository)
            runtime.service.reasoner = provider
            runtime.service.verifier = SkipVerifier()
            try:
                pair = runtime.pair()
                result = runtime.run(
                    pair,
                    budgets=InvestigationBudgets(
                        max_pairs=1,
                        max_searches=1,
                        searches_per_pair=1,
                        max_model_calls=2,
                        max_input_characters=100000,
                    ),
                )
                print(
                    json.dumps(
                        {
                            "provider": provider.provider,
                            "model": provider.model,
                            "reasoner_completed": bool(provider.last_response_metrics),
                            "reasoner_calls": provider.usage.calls,
                            "request": provider.last_request_metrics,
                            "duration_ms": provider.last_request_duration_ms,
                            "response": provider.last_response_metrics,
                            "reasoner_failures": [
                                d.reason
                                for d in result.diagnostics
                                if d.code == "MODEL_FAILED" and d.stage == "reasoner"
                            ],
                            "finding_persisted": False,
                            "verifier_called": False,
                        },
                        sort_keys=True,
                    )
                )
            finally:
                repository.close()
                store.close()
    except Exception as error:
        category = (
            error.category
            if isinstance(error, ResearchError)
            and error.category
            in {
                "provider_timeout",
                "provider_transport",
                "provider_http_error",
                "provider_error",
                "malformed_response",
                "empty_output",
                "truncated_output",
                "schema_or_json_validation",
            }
            else "internal_error"
        )
        print(
            json.dumps(
                {
                    "result": "FAILED",
                    "error_type": type(error).__name__,
                    "category": category,
                },
                sort_keys=True,
            )
        )
        raise SystemExit(2) from None
    finally:
        provider.close()


if __name__ == "__main__":
    main()
