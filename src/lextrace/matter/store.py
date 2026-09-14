"""Single-host SQLite matter records and private document files."""

import hashlib
import re
import shutil
import sqlite3
import threading
import uuid
from datetime import UTC, date, datetime
from pathlib import Path

from lextrace.graph.intelligence_contracts import DoctrineAnalysis, TreatmentAnnotation
from lextrace.matter.contracts import (
    ArgumentFinding,
    DocumentSection,
    LegalClaim,
    LegalIssue,
    Matter,
    MatterAnalysis,
    MatterDocument,
    MatterError,
    MatterFact,
)
from lextrace.matter.documents import (
    MAX_UPLOAD_BYTES,
    extract_document,
    safe_filename,
    segment_document,
)
from lextrace.matter.research_contracts import (
    AttackFinding,
    DeepResearchPlan,
    DeepResearchRun,
    ResearchCoverage,
)

IDENTIFIER = re.compile(r"^[0-9a-f]{32}$")


def _id(value: str) -> str:
    if not IDENTIFIER.fullmatch(value):
        raise MatterError("Matter identifier is invalid.")
    return value


def _now() -> datetime:
    return datetime.now(UTC)


class MatterStore:
    """Owns matter DB and private files; callers supply IDs, never file paths."""

    def __init__(self, database: Path, private_root: Path) -> None:
        database.parent.mkdir(parents=True, exist_ok=True)
        private_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        private_root.chmod(0o700)
        self.private_root = private_root
        self._lock = threading.RLock()
        self._db = sqlite3.connect(database, check_same_thread=False)
        database.chmod(0o600)
        self._db.row_factory = sqlite3.Row
        with self._db:
            self._db.executescript(
                """
                PRAGMA foreign_keys=ON;
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS matters (
                  id TEXT PRIMARY KEY, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS matter_documents (
                  id TEXT PRIMARY KEY, matter_id TEXT NOT NULL, payload TEXT NOT NULL,
                  FOREIGN KEY(matter_id) REFERENCES matters(id) ON DELETE CASCADE);
                CREATE TABLE IF NOT EXISTS document_sections (
                  id TEXT PRIMARY KEY, document_id TEXT NOT NULL, payload TEXT NOT NULL,
                  FOREIGN KEY(document_id) REFERENCES matter_documents(id)
                  ON DELETE CASCADE);
                CREATE TABLE IF NOT EXISTS matter_issues (
                  id TEXT PRIMARY KEY, matter_id TEXT NOT NULL,
                  document_id TEXT NOT NULL,
                  payload TEXT NOT NULL,
                  FOREIGN KEY(matter_id) REFERENCES matters(id) ON DELETE CASCADE);
                CREATE TABLE IF NOT EXISTS matter_facts (
                  id TEXT PRIMARY KEY, matter_id TEXT NOT NULL,
                  document_id TEXT NOT NULL, payload TEXT NOT NULL,
                  FOREIGN KEY(matter_id) REFERENCES matters(id) ON DELETE CASCADE);
                CREATE TABLE IF NOT EXISTS matter_claims (
                  id TEXT PRIMARY KEY, matter_id TEXT NOT NULL,
                  document_id TEXT NOT NULL,
                  payload TEXT NOT NULL,
                  FOREIGN KEY(matter_id) REFERENCES matters(id) ON DELETE CASCADE);
                CREATE TABLE IF NOT EXISTS document_citations (
                  id TEXT PRIMARY KEY, matter_id TEXT NOT NULL,
                  document_id TEXT NOT NULL,
                  payload TEXT NOT NULL,
                  FOREIGN KEY(matter_id) REFERENCES matters(id)
                  ON DELETE CASCADE);
                CREATE TABLE IF NOT EXISTS matter_evidence (
                  id TEXT PRIMARY KEY, claim_id TEXT NOT NULL, payload TEXT NOT NULL,
                  FOREIGN KEY(claim_id) REFERENCES matter_claims(id) ON DELETE CASCADE);
                CREATE TABLE IF NOT EXISTS claim_authority_links (
                  claim_id TEXT NOT NULL, case_id TEXT NOT NULL, relation TEXT NOT NULL,
                  payload TEXT NOT NULL, PRIMARY KEY(claim_id,case_id,relation),
                  FOREIGN KEY(claim_id) REFERENCES matter_claims(id) ON DELETE CASCADE);
                CREATE TABLE IF NOT EXISTS argument_findings (
                  claim_id TEXT PRIMARY KEY, payload TEXT NOT NULL,
                  FOREIGN KEY(claim_id) REFERENCES matter_claims(id) ON DELETE CASCADE);
                CREATE TABLE IF NOT EXISTS matter_jobs (
                  id TEXT PRIMARY KEY, matter_id TEXT NOT NULL,
                  document_id TEXT NOT NULL,
                  claim_id TEXT, status TEXT NOT NULL, error_code TEXT,
                  created_at TEXT NOT NULL, completed_at TEXT,
                  FOREIGN KEY(matter_id) REFERENCES matters(id) ON DELETE CASCADE);
                CREATE TABLE IF NOT EXISTS treatment_annotations_v1 (
                  matter_id TEXT NOT NULL, claim_id TEXT NOT NULL,
                  annotation_id TEXT NOT NULL, payload TEXT NOT NULL,
                  PRIMARY KEY(matter_id,claim_id,annotation_id),
                  FOREIGN KEY(matter_id) REFERENCES matters(id) ON DELETE CASCADE);
                CREATE TABLE IF NOT EXISTS doctrine_cache_v1 (
                  matter_id TEXT NOT NULL, claim_id TEXT NOT NULL,
                  identity TEXT NOT NULL, payload TEXT NOT NULL,
                  PRIMARY KEY(matter_id,claim_id),
                  FOREIGN KEY(matter_id) REFERENCES matters(id) ON DELETE CASCADE);
                CREATE TABLE IF NOT EXISTS research_plans_v2 (
                  matter_id TEXT NOT NULL, claim_id TEXT PRIMARY KEY,
                  payload TEXT NOT NULL,
                  FOREIGN KEY(claim_id) REFERENCES matter_claims(id) ON DELETE CASCADE);
                CREATE TABLE IF NOT EXISTS research_runs_v2 (
                  id TEXT PRIMARY KEY, matter_id TEXT NOT NULL, claim_id TEXT NOT NULL,
                  payload TEXT NOT NULL,
                  FOREIGN KEY(claim_id) REFERENCES matter_claims(id) ON DELETE CASCADE);
                CREATE TABLE IF NOT EXISTS research_coverage_v2 (
                  claim_id TEXT PRIMARY KEY, payload TEXT NOT NULL,
                  FOREIGN KEY(claim_id) REFERENCES matter_claims(id) ON DELETE CASCADE);
                CREATE TABLE IF NOT EXISTS attack_findings_v2 (
                  id TEXT PRIMARY KEY, matter_id TEXT NOT NULL, claim_id TEXT NOT NULL,
                  payload TEXT NOT NULL,
                  FOREIGN KEY(claim_id) REFERENCES matter_claims(id) ON DELETE CASCADE);
                """
            )

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def create_matter(
        self,
        name: str,
        *,
        court: str | None = None,
        jurisdiction: str | None = None,
        state: str | None = None,
        as_of_date: date | None = None,
        reference: str | None = None,
    ) -> Matter:
        now = _now()
        matter = Matter(
            matter_id=uuid.uuid4().hex,
            name=name,
            court=court,
            jurisdiction=jurisdiction,
            state=state,
            as_of_date=as_of_date,
            reference=reference,
            created_at=now,
            updated_at=now,
        )
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO matters VALUES (?,?)",
                (matter.matter_id, matter.model_dump_json()),
            )
        return matter

    def get_matter(self, matter_id: str) -> Matter | None:
        with self._lock:
            row = self._db.execute(
                "SELECT payload FROM matters WHERE id=?", (_id(matter_id),)
            ).fetchone()
        return Matter.model_validate_json(row[0]) if row else None

    def list_matters(self) -> list[Matter]:
        with self._lock:
            rows = self._db.execute(
                "SELECT payload FROM matters ORDER BY id"
            ).fetchall()
        return [Matter.model_validate_json(row[0]) for row in rows]

    def delete_matter(self, matter_id: str) -> bool:
        _id(matter_id)
        with self._lock, self._db:
            exists = self._db.execute(
                "SELECT 1 FROM matters WHERE id=?", (matter_id,)
            ).fetchone()
            if exists is None:
                return False
            active = self._db.execute(
                "SELECT COUNT(*) FROM matter_jobs WHERE matter_id=? "
                "AND status IN ('queued','running')",
                (matter_id,),
            ).fetchone()[0]
            if active:
                raise MatterError("Wait for matter analysis to finish before deletion.")
            private_directory = self.private_root / matter_id
            if private_directory.exists():
                try:
                    shutil.rmtree(private_directory)
                except OSError as exc:
                    raise MatterError(
                        "Private matter files could not be removed."
                    ) from exc
            removed = self._db.execute(
                "DELETE FROM matters WHERE id=?", (matter_id,)
            ).rowcount
        return bool(removed)

    def add_document(
        self, matter_id: str, filename: str, content: bytes
    ) -> MatterDocument:
        if self.get_matter(matter_id) is None:
            raise MatterError("Matter was not found.")
        filename, kind = safe_filename(filename)
        if not content or len(content) > MAX_UPLOAD_BYTES:
            raise MatterError("Document is empty or exceeds the 10 MB limit.")
        digest = hashlib.sha256(content).hexdigest()
        for existing in self.list_documents(matter_id):
            if existing.content_hash == digest and existing.document_type == kind:
                return existing
        extracted = extract_document(filename, content)
        document_id = uuid.uuid4().hex
        document = MatterDocument(
            document_id=document_id,
            matter_id=matter_id,
            filename=filename,
            document_type=kind,
            content_hash=digest,
            ingestion_status="OCR_REQUIRED" if extracted.ocr_required else "READY",
            page_count=extracted.page_count,
            created_at=_now(),
            text_length=len(extracted.text),
        )
        sections = segment_document(document_id, digest, extracted)
        directory = self.private_root / matter_id / document_id
        directory.mkdir(parents=True, exist_ok=False, mode=0o700)
        try:
            source_file = directory / f"source.{kind}"
            text_file = directory / "extracted.txt"
            source_file.write_bytes(content)
            text_file.write_text(extracted.text, encoding="utf-8")
            source_file.chmod(0o600)
            text_file.chmod(0o600)
            with self._lock, self._db:
                self._db.execute(
                    "INSERT INTO matter_documents VALUES (?,?,?)",
                    (document_id, matter_id, document.model_dump_json()),
                )
                self._db.executemany(
                    "INSERT INTO document_sections VALUES (?,?,?)",
                    [
                        (s.section_id, document_id, s.model_dump_json())
                        for s in sections
                    ],
                )
        except Exception:
            shutil.rmtree(directory, ignore_errors=True)
            raise
        return document

    def get_document(self, matter_id: str, document_id: str) -> MatterDocument | None:
        with self._lock:
            row = self._db.execute(
                "SELECT payload FROM matter_documents WHERE matter_id=? AND id=?",
                (_id(matter_id), _id(document_id)),
            ).fetchone()
        return MatterDocument.model_validate_json(row[0]) if row else None

    def list_documents(self, matter_id: str) -> list[MatterDocument]:
        _id(matter_id)
        with self._lock:
            rows = self._db.execute(
                "SELECT payload FROM matter_documents WHERE matter_id=? ORDER BY rowid",
                (matter_id,),
            ).fetchall()
        return [MatterDocument.model_validate_json(row[0]) for row in rows]

    def document_text(self, matter_id: str, document_id: str) -> str:
        if self.get_document(matter_id, document_id) is None:
            raise MatterError("Document was not found.")
        return (
            self.private_root / matter_id / document_id / "extracted.txt"
        ).read_text(encoding="utf-8")

    def sections(self, matter_id: str, document_id: str) -> list[DocumentSection]:
        if self.get_document(matter_id, document_id) is None:
            raise MatterError("Document was not found.")
        with self._lock:
            rows = self._db.execute(
                "SELECT payload FROM document_sections "
                "WHERE document_id=? ORDER BY rowid",
                (document_id,),
            ).fetchall()
        return [DocumentSection.model_validate_json(row[0]) for row in rows]

    def save_analysis(self, analysis: MatterAnalysis) -> None:
        document = self.get_document(analysis.matter_id, analysis.document_id)
        if document is None:
            raise MatterError("Document was not found.")
        document.analysis_warnings = analysis.warnings
        with self._lock, self._db:
            self._db.execute(
                "UPDATE matter_documents SET payload=? WHERE id=?",
                (document.model_dump_json(), document.document_id),
            )
            old_claims = self._db.execute(
                "SELECT id FROM matter_claims WHERE document_id=?",
                (analysis.document_id,),
            ).fetchall()
            for row in old_claims:
                self.invalidate_doctrine(analysis.matter_id, row[0], clear_reviews=True)
                self._db.execute(
                    "DELETE FROM matter_evidence WHERE claim_id=?", (row[0],)
                )
                self._db.execute(
                    "DELETE FROM claim_authority_links WHERE claim_id=?", (row[0],)
                )
                self._db.execute(
                    "DELETE FROM argument_findings WHERE claim_id=?", (row[0],)
                )
            for table in ("matter_issues", "matter_claims", "document_citations"):
                self._db.execute(
                    f"DELETE FROM {table} WHERE document_id=?", (analysis.document_id,)
                )
            for issue in analysis.issues:
                self._db.execute(
                    "INSERT INTO matter_issues VALUES (?,?,?,?)",
                    (
                        issue.issue_id,
                        analysis.matter_id,
                        analysis.document_id,
                        issue.model_dump_json(),
                    ),
                )
            for claim in analysis.claims:
                self._db.execute(
                    "INSERT INTO matter_claims VALUES (?,?,?,?)",
                    (
                        claim.claim_id,
                        analysis.matter_id,
                        analysis.document_id,
                        claim.model_dump_json(),
                    ),
                )
            for citation in analysis.citations:
                self._db.execute(
                    "INSERT INTO document_citations VALUES (?,?,?,?)",
                    (
                        citation.citation_id,
                        analysis.matter_id,
                        analysis.document_id,
                        citation.model_dump_json(),
                    ),
                )
            for finding in analysis.findings:
                self._save_finding(finding)

    def _save_finding(self, finding: ArgumentFinding) -> None:
        self._db.execute(
            "INSERT INTO argument_findings VALUES (?,?)",
            (finding.claim_id, finding.model_dump_json()),
        )
        for evidence in finding.evidence:
            self._db.execute(
                "INSERT INTO matter_evidence VALUES (?,?,?)",
                (evidence.evidence_id, finding.claim_id, evidence.model_dump_json()),
            )
        for link in finding.cited_authorities:
            self._db.execute(
                "INSERT OR REPLACE INTO claim_authority_links VALUES (?,?,?,?)",
                (link.claim_id, link.case_id, link.relation, link.model_dump_json()),
            )

    def analysis(self, matter_id: str, document_id: str) -> MatterAnalysis:
        document = self.get_document(matter_id, document_id)
        if document is None:
            raise MatterError("Document was not found.")
        with self._lock:
            issues = self._db.execute(
                "SELECT payload FROM matter_issues WHERE document_id=? ORDER BY rowid",
                (document_id,),
            ).fetchall()
            claims = self._db.execute(
                "SELECT payload FROM matter_claims WHERE document_id=? ORDER BY rowid",
                (document_id,),
            ).fetchall()
            citations = self._db.execute(
                "SELECT payload FROM document_citations "
                "WHERE document_id=? ORDER BY rowid",
                (document_id,),
            ).fetchall()
            findings = self._db.execute(
                """SELECT f.payload FROM argument_findings f JOIN matter_claims c
                ON c.id=f.claim_id WHERE c.document_id=? ORDER BY c.rowid""",
                (document_id,),
            ).fetchall()
        from lextrace.matter.contracts import DocumentCitation

        return MatterAnalysis(
            matter_id=matter_id,
            document_id=document_id,
            issues=[LegalIssue.model_validate_json(r[0]) for r in issues],
            claims=[LegalClaim.model_validate_json(r[0]) for r in claims],
            citations=[DocumentCitation.model_validate_json(r[0]) for r in citations],
            findings=[ArgumentFinding.model_validate_json(r[0]) for r in findings],
            warnings=document.analysis_warnings,
        )

    def all_claims(self, matter_id: str) -> list[LegalClaim]:
        _id(matter_id)
        with self._lock:
            rows = self._db.execute(
                "SELECT payload FROM matter_claims WHERE matter_id=? ORDER BY rowid",
                (matter_id,),
            ).fetchall()
        return [LegalClaim.model_validate_json(row[0]) for row in rows]

    def all_issues(self, matter_id: str) -> list[LegalIssue]:
        _id(matter_id)
        with self._lock:
            rows = self._db.execute(
                "SELECT payload FROM matter_issues WHERE matter_id=? ORDER BY rowid",
                (matter_id,),
            ).fetchall()
        return [LegalIssue.model_validate_json(row[0]) for row in rows]

    def save_facts(self, facts: list[MatterFact]) -> None:
        with self._lock, self._db:
            for fact in facts:
                self._db.execute(
                    "INSERT OR REPLACE INTO matter_facts VALUES (?,?,?,?)",
                    (
                        fact.fact_id,
                        fact.matter_id,
                        fact.document_id,
                        fact.model_dump_json(),
                    ),
                )

    def facts(self, matter_id: str, document_id: str) -> list[MatterFact]:
        if self.get_document(matter_id, document_id) is None:
            raise MatterError("Document was not found.")
        with self._lock:
            rows = self._db.execute(
                "SELECT payload FROM matter_facts WHERE matter_id=? "
                "AND document_id=? ORDER BY rowid",
                (matter_id, document_id),
            ).fetchall()
        return [MatterFact.model_validate_json(row[0]) for row in rows]

    def claim(self, matter_id: str, claim_id: str) -> LegalClaim | None:
        with self._lock:
            row = self._db.execute(
                "SELECT payload FROM matter_claims WHERE matter_id=? AND id=?",
                (_id(matter_id), _id(claim_id)),
            ).fetchone()
        return LegalClaim.model_validate_json(row[0]) if row else None

    def update_claim(self, claim: LegalClaim) -> None:
        with self._lock, self._db:
            previous = self.claim(claim.matter_id, claim.claim_id)
            self._db.execute(
                "UPDATE matter_claims SET payload=? WHERE id=? AND matter_id=?",
                (claim.model_dump_json(), claim.claim_id, claim.matter_id),
            )
            self._db.execute(
                "DELETE FROM argument_findings WHERE claim_id=?", (claim.claim_id,)
            )
            self.invalidate_doctrine(
                claim.matter_id,
                claim.claim_id,
                clear_reviews=(
                    previous is None
                    or previous.normalized_proposition != claim.normalized_proposition
                ),
            )
            self.invalidate_research(claim.claim_id)

    def set_claim_lock(self, matter_id: str, claim_id: str, locked: bool) -> LegalClaim:
        claim = self.claim(matter_id, claim_id)
        if claim is None:
            raise MatterError("Claim was not found.")
        claim.wording_locked = locked
        with self._lock, self._db:
            self._db.execute(
                "UPDATE matter_claims SET payload=? WHERE matter_id=? AND id=?",
                (claim.model_dump_json(), matter_id, claim_id),
            )
        return claim

    def finding(self, matter_id: str, claim_id: str) -> ArgumentFinding | None:
        if self.claim(matter_id, claim_id) is None:
            return None
        with self._lock:
            row = self._db.execute(
                "SELECT payload FROM argument_findings WHERE claim_id=?",
                (claim_id,),
            ).fetchone()
        return ArgumentFinding.model_validate_json(row[0]) if row else None

    def update_finding(self, finding: ArgumentFinding) -> None:
        with self._lock, self._db:
            self._db.execute(
                "DELETE FROM matter_evidence WHERE claim_id=?", (finding.claim_id,)
            )
            self._db.execute(
                "DELETE FROM claim_authority_links WHERE claim_id=?",
                (finding.claim_id,),
            )
            self._db.execute(
                "DELETE FROM argument_findings WHERE claim_id=?", (finding.claim_id,)
            )
            self._save_finding(finding)
            claim = self._db.execute(
                "SELECT matter_id FROM matter_claims WHERE id=?", (finding.claim_id,)
            ).fetchone()
            if claim is not None:
                self.invalidate_doctrine(claim[0], finding.claim_id)
                self.invalidate_research(finding.claim_id)

    def invalidate_research(self, claim_id: str) -> None:
        """Invalidate only derived state for this claim, retaining run history."""
        with self._lock, self._db:
            for table in (
                "research_plans_v2",
                "research_coverage_v2",
                "attack_findings_v2",
            ):
                self._db.execute(f"DELETE FROM {table} WHERE claim_id=?", (claim_id,))

    def research_plan(self, matter_id: str, claim_id: str) -> DeepResearchPlan | None:
        if self.claim(matter_id, claim_id) is None:
            return None
        with self._lock:
            row = self._db.execute(
                "SELECT payload FROM research_plans_v2 "
                "WHERE matter_id=? AND claim_id=?",
                (matter_id, claim_id),
            ).fetchone()
        return DeepResearchPlan.model_validate_json(row[0]) if row else None

    def save_research_plan(self, plan: DeepResearchPlan) -> None:
        if self.claim(plan.matter_id, plan.claim_id) is None:
            raise MatterError("Claim was not found.")
        with self._lock, self._db:
            self._db.execute(
                "INSERT OR REPLACE INTO research_plans_v2 VALUES (?,?,?)",
                (plan.matter_id, plan.claim_id, plan.model_dump_json()),
            )

    def research_run(self, matter_id: str, run_id: str) -> DeepResearchRun | None:
        with self._lock:
            row = self._db.execute(
                "SELECT payload FROM research_runs_v2 WHERE matter_id=? AND id=?",
                (_id(matter_id), _id(run_id)),
            ).fetchone()
        return DeepResearchRun.model_validate_json(row[0]) if row else None

    def latest_research_run(
        self, matter_id: str, claim_id: str
    ) -> DeepResearchRun | None:
        if self.claim(matter_id, claim_id) is None:
            return None
        with self._lock:
            row = self._db.execute(
                "SELECT payload FROM research_runs_v2 WHERE matter_id=? AND claim_id=? "
                "AND json_extract(payload, '$.plan_id') != 'red-team' "
                "ORDER BY rowid DESC LIMIT 1",
                (matter_id, claim_id),
            ).fetchone()
        return DeepResearchRun.model_validate_json(row[0]) if row else None

    def save_research_run(self, run: DeepResearchRun) -> None:
        if self.claim(run.matter_id, run.claim_id) is None:
            raise MatterError("Claim was not found.")
        with self._lock, self._db:
            self._db.execute(
                "INSERT OR REPLACE INTO research_runs_v2 VALUES (?,?,?,?)",
                (run.run_id, run.matter_id, run.claim_id, run.model_dump_json()),
            )

    def research_coverage(self, claim_id: str) -> ResearchCoverage | None:
        with self._lock:
            row = self._db.execute(
                "SELECT payload FROM research_coverage_v2 WHERE claim_id=?", (claim_id,)
            ).fetchone()
        return ResearchCoverage.model_validate_json(row[0]) if row else None

    def save_research_coverage(self, coverage: ResearchCoverage) -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT OR REPLACE INTO research_coverage_v2 VALUES (?,?)",
                (coverage.claim_id, coverage.model_dump_json()),
            )

    def attacks(
        self, matter_id: str, claim_id: str | None = None
    ) -> list[AttackFinding]:
        _id(matter_id)
        with self._lock:
            if claim_id is None:
                rows = self._db.execute(
                    "SELECT payload FROM attack_findings_v2 "
                    "WHERE matter_id=? ORDER BY id",
                    (matter_id,),
                ).fetchall()
            else:
                rows = self._db.execute(
                    "SELECT payload FROM attack_findings_v2 "
                    "WHERE matter_id=? AND claim_id=? ORDER BY id",
                    (matter_id, _id(claim_id)),
                ).fetchall()
        return [AttackFinding.model_validate_json(row[0]) for row in rows]

    def save_attacks(
        self, matter_id: str, claim_id: str, attacks: list[AttackFinding]
    ) -> None:
        if self.claim(matter_id, claim_id) is None:
            raise MatterError("Claim was not found.")
        with self._lock, self._db:
            self._db.execute(
                "DELETE FROM attack_findings_v2 WHERE claim_id=?", (claim_id,)
            )
            self._db.executemany(
                "INSERT INTO attack_findings_v2 VALUES (?,?,?,?)",
                [
                    (a.attack_id, matter_id, claim_id, a.model_dump_json())
                    for a in attacks
                ],
            )

    def invalidate_doctrine(
        self, matter_id: str, claim_id: str, *, clear_reviews: bool = False
    ) -> None:
        with self._lock, self._db:
            self._db.execute(
                "DELETE FROM doctrine_cache_v1 WHERE matter_id=? AND claim_id=?",
                (_id(matter_id), _id(claim_id)),
            )
            if clear_reviews:
                self._db.execute(
                    "DELETE FROM treatment_annotations_v1 "
                    "WHERE matter_id=? AND claim_id=?",
                    (matter_id, claim_id),
                )

    def cached_doctrine(
        self, matter_id: str, claim_id: str, identity: str
    ) -> DoctrineAnalysis | None:
        with self._lock:
            row = self._db.execute(
                "SELECT payload FROM doctrine_cache_v1 "
                "WHERE matter_id=? AND claim_id=? AND identity=?",
                (_id(matter_id), _id(claim_id), identity),
            ).fetchone()
        return DoctrineAnalysis.model_validate_json(row[0]) if row else None

    def latest_doctrine(self, matter_id: str, claim_id: str) -> DoctrineAnalysis | None:
        if self.claim(matter_id, claim_id) is None:
            return None
        with self._lock:
            row = self._db.execute(
                "SELECT payload FROM doctrine_cache_v1 "
                "WHERE matter_id=? AND claim_id=?",
                (matter_id, claim_id),
            ).fetchone()
        return DoctrineAnalysis.model_validate_json(row[0]) if row else None

    def save_doctrine(
        self,
        matter_id: str,
        claim_id: str,
        identity: str,
        analysis: DoctrineAnalysis,
    ) -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT OR REPLACE INTO doctrine_cache_v1 VALUES (?,?,?,?)",
                (matter_id, claim_id, identity, analysis.model_dump_json()),
            )
            for edge in analysis.trace.edges:
                annotation = edge.treatment
                old = self.treatment_review(
                    matter_id, claim_id, annotation.annotation_id
                )
                if old is None:
                    self._db.execute(
                        "INSERT INTO treatment_annotations_v1 VALUES (?,?,?,?)",
                        (
                            matter_id,
                            claim_id,
                            annotation.annotation_id,
                            annotation.model_dump_json(),
                        ),
                    )

    def treatment_review(
        self, matter_id: str, claim_id: str, annotation_id: str
    ) -> TreatmentAnnotation | None:
        with self._lock:
            row = self._db.execute(
                "SELECT payload FROM treatment_annotations_v1 "
                "WHERE matter_id=? AND claim_id=? AND annotation_id=?",
                (_id(matter_id), _id(claim_id), annotation_id),
            ).fetchone()
        return TreatmentAnnotation.model_validate_json(row[0]) if row else None

    def review_treatment(
        self,
        matter_id: str,
        claim_id: str,
        annotation_id: str,
        state: str,
    ) -> TreatmentAnnotation:
        if state not in {"CONFIRMED", "REJECTED", "UNCERTAIN"}:
            raise MatterError("Treatment review state is invalid.")
        old = self.treatment_review(matter_id, claim_id, annotation_id)
        if old is None or self.claim(matter_id, claim_id) is None:
            raise MatterError("Treatment annotation was not found.")
        reviewed = old.model_copy(update={"review_state": state, "reviewed_at": _now()})
        with self._lock, self._db:
            self._db.execute(
                "UPDATE treatment_annotations_v1 SET payload=? "
                "WHERE matter_id=? AND claim_id=? AND annotation_id=?",
                (reviewed.model_dump_json(), matter_id, claim_id, annotation_id),
            )
            self.invalidate_doctrine(matter_id, claim_id)
        return reviewed

    def create_job(
        self, matter_id: str, document_id: str, claim_id: str | None = None
    ) -> str:
        if self.get_document(matter_id, document_id) is None:
            raise MatterError("Document was not found.")
        job_id = uuid.uuid4().hex
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO matter_jobs VALUES (?,?,?,?,?,?,?,?)",
                (
                    job_id,
                    matter_id,
                    document_id,
                    claim_id,
                    "queued",
                    None,
                    _now().isoformat(),
                    None,
                ),
            )
        return job_id

    def set_job(self, job_id: str, status: str, error_code: str | None = None) -> None:
        with self._lock, self._db:
            self._db.execute(
                "UPDATE matter_jobs SET status=?,error_code=?,completed_at=? "
                "WHERE id=?",
                (
                    status,
                    error_code,
                    _now().isoformat() if status in {"completed", "failed"} else None,
                    _id(job_id),
                ),
            )

    def job(self, matter_id: str, job_id: str) -> dict[str, str | None] | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM matter_jobs WHERE matter_id=? AND id=?",
                (_id(matter_id), _id(job_id)),
            ).fetchone()
        return dict(row) if row else None
