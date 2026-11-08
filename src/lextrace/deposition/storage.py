"""Append-only M9 rows in the existing matter SQLite transaction."""

import sqlite3
from hashlib import sha256
from typing import TypeVar

from pydantic import BaseModel

from lextrace.deposition.contracts import PlanBasis, PlanError, PlanRevision

Model = TypeVar("Model", bound=BaseModel)
MAX_PAYLOAD_BYTES = 32 * 1024 * 1024


def encode(model: BaseModel) -> tuple[str, str]:
    payload = model.model_dump_json()
    if len(payload.encode()) > MAX_PAYLOAD_BYTES:
        raise PlanError("DEPOSITION_BUDGET_EXCEEDED")
    return payload, sha256(payload.encode()).hexdigest()


def decode(payload: str, digest: str, model: type[Model]) -> Model:
    if (
        len(payload.encode()) > MAX_PAYLOAD_BYTES
        or sha256(payload.encode()).hexdigest() != digest
    ):
        raise PlanError("DEPOSITION_STORAGE_CONFLICT")
    try:
        return model.model_validate_json(payload)
    except ValueError:
        raise PlanError("DEPOSITION_STORAGE_CONFLICT") from None


class PlanStorage:
    @staticmethod
    def basis(db: sqlite3.Connection, plan_id: str, thread_id: str) -> PlanBasis:
        row = db.execute(
            "SELECT payload,digest FROM deposition_plans WHERE "
            "plan_id=? AND thread_id=?",
            (plan_id, thread_id),
        ).fetchone()
        if row is None:
            raise PlanError("DEPOSITION_NOT_FOUND", 404)
        basis = decode(row[0], row[1], PlanBasis)
        if (basis.plan_id, basis.thread_id) != (plan_id, thread_id):
            raise PlanError("DEPOSITION_STORAGE_CONFLICT")
        return basis

    @staticmethod
    def revisions(db: sqlite3.Connection, plan_id: str) -> tuple[PlanRevision, ...]:
        rows = db.execute(
            "SELECT revision,payload,digest FROM deposition_revisions "
            "WHERE plan_id=? ORDER BY revision",
            (plan_id,),
        ).fetchall()
        result = tuple(
            decode(payload, digest, PlanRevision) for _, payload, digest in rows
        )
        if not result or any(
            (r.plan_id, r.revision) != (plan_id, i + 1) for i, r in enumerate(result)
        ):
            raise PlanError("DEPOSITION_STORAGE_CONFLICT")
        return result

    @staticmethod
    def append(db: sqlite3.Connection, revision: PlanRevision) -> None:
        payload, digest = encode(revision)
        db.execute(
            "INSERT INTO deposition_revisions VALUES (?,?,?,?)",
            (revision.plan_id, revision.revision, payload, digest),
        )
