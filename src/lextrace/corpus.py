"""Small local corpus files: canonical Case JSONL and ingestion metadata."""

import json
import os
import tempfile
from datetime import date
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from lextrace.domain.case import Case


class CorpusError(Exception):
    """A safe local-corpus error without file contents or credentials."""


class CorpusQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    court: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$")
    filed_after: date | None = None
    filed_before: date | None = None
    max_cases: int = Field(gt=0)

    def api_filters(self) -> dict[str, str]:
        filters = {"docket__court": self.court, "order_by": "id"}
        if self.filed_after:
            filters["date_filed__gte"] = self.filed_after.isoformat()
        if self.filed_before:
            filters["date_filed__lte"] = self.filed_before.isoformat()
        return filters


class Rejection(BaseModel):
    source_id: str | None
    reason: Literal["invalid_cluster", "invalid_case", "filter_mismatch"]


class IngestionRun(BaseModel):
    status: Literal["running", "complete", "exhausted", "failed", "interrupted"] = (
        "running"
    )
    request_interval: float
    initial_case_count: int = 0
    source_records_encountered: int = 0
    successfully_normalized_cases: int = 0
    skipped_duplicate_records: int = 0
    rejections: list[Rejection] = Field(default_factory=list)
    # Only locally assigned error categories, never arbitrary exception messages.
    failure_reason: (
        Literal["request_failure", "local_failure", "unexpected_failure"] | None
    ) = None

    def quality(self) -> dict[str, object]:
        counts: dict[str, int] = {}
        for rejection in self.rejections:
            counts[rejection.reason] = counts.get(rejection.reason, 0) + 1
        return {
            "source_records_encountered": self.source_records_encountered,
            "successfully_normalized_cases": self.successfully_normalized_cases,
            "rejected_records": len(self.rejections),
            "rejection_reason_counts": counts,
            "skipped_duplicate_records": self.skipped_duplicate_records,
        }


class Manifest(BaseModel):
    format_version: Literal[1] = 1
    normalizer_version: Literal["milestone-1"] = "milestone-1"
    source: Literal["courtlistener"] = "courtlistener"
    order_by: Literal["id"] = "id"
    query: CorpusQuery
    runs: list[IngestionRun] = Field(default_factory=list)


def manifest_path(output: Path) -> Path:
    return output.with_suffix(".manifest.json")


def serialize_cases(cases: list[Case]) -> str:
    return "".join(
        json.dumps(
            case.model_dump(mode="json"),
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
        for case in sorted(cases, key=lambda case: int(case.source_id))
    )


def atomic_write(path: Path, content: str) -> None:
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="\n", dir=path.parent, delete=False
        ) as stream:
            temporary = stream.name
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except OSError:
        raise CorpusError("Could not write corpus files.") from None
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)


def read_cases(path: Path) -> list[Case]:
    cases: list[Case] = []
    try:
        with path.open(encoding="utf-8") as stream:
            for number, line in enumerate(stream, 1):
                try:
                    case = Case.model_validate_json(line)
                    if (
                        not case.source_id.isascii()
                        or not case.source_id.isdecimal()
                        or int(case.source_id) <= 0
                    ):
                        raise ValueError
                    cases.append(case)
                except (ValidationError, ValueError):
                    raise CorpusError(f"Invalid Case JSONL at line {number}.") from None
    except (OSError, UnicodeError):
        raise CorpusError("Could not read corpus JSONL.") from None
    return cases


def prepare(
    output: Path, query: CorpusQuery, resume: bool
) -> tuple[list[Case], Manifest]:
    # Restrict generated corpora and sidecars to the ignored data directory.
    if output.suffix != ".jsonl" or not output.resolve().is_relative_to(
        Path("data").resolve()
    ):
        raise CorpusError("Corpus output must be a .jsonl file under data/.")
    sidecar = manifest_path(output)
    if resume:
        try:
            manifest = Manifest.model_validate_json(sidecar.read_bytes())
        except (OSError, ValidationError):
            raise CorpusError("Resume requires a valid corpus manifest.") from None
        if manifest.query != query:
            raise CorpusError(
                "Resume filters and maximum case count must match the manifest."
            )
        cases = read_cases(output)
        ids = [(case.source, case.source_id) for case in cases]
        if len(set(ids)) != len(ids):
            raise CorpusError("Cannot resume a corpus with duplicate source IDs.")
        if len(cases) > query.max_cases or any(
            case.court_id != query.court
            or (
                query.filed_after is not None
                and (case.date_filed is None or case.date_filed < query.filed_after)
            )
            or (
                query.filed_before is not None
                and (case.date_filed is None or case.date_filed > query.filed_before)
            )
            for case in cases
        ):
            raise CorpusError("Existing cases do not match the corpus query.")
        for run in manifest.runs:
            if run.status == "running":
                run.status = "interrupted"
                # JSONL is authoritative if interruption occurred between replacements.
                run.successfully_normalized_cases = len(cases) - run.initial_case_count
        return cases, manifest
    if output.exists() or sidecar.exists():
        raise CorpusError("Corpus output already exists; use --resume or a new path.")
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        raise CorpusError("Could not create corpus output directory.") from None
    manifest = Manifest(query=query)
    # Metadata first: if interrupted before creating JSONL, no corpus was lost.
    save_manifest(output, manifest)
    atomic_write(output, "")
    return [], manifest


def save_manifest(output: Path, manifest: Manifest) -> None:
    data = manifest.model_dump(mode="json")
    # Derived counters stay beside each run's explicit rejection records.
    for record, run in zip(data["runs"], manifest.runs, strict=True):
        record["ingestion_quality"] = run.quality()
    atomic_write(
        manifest_path(output), json.dumps(data, sort_keys=True, indent=2) + "\n"
    )
