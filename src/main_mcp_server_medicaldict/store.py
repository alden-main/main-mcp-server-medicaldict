"""원문을 저장하지 않는 분석 및 승인 이력 SQLite 저장소다."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from .models import (
    AnalysisResult,
    AnalysisStatus,
    CandidateType,
    Confidence,
    DecisionStatus,
    DictionaryTerm,
    DocumentTermCandidate,
    IssueType,
    ReviewStatus,
    Suggestion,
)


class StoreError(RuntimeError):
    """분석 또는 승인 이력의 무결성 위반을 나타낸다."""


class AnalysisStore:
    """분석 요약과 제안을 사전 DB와 별도의 SQLite에 저장한다."""

    def __init__(self, database_path: Path) -> None:
        """운영 이력 DB와 필요한 테이블을 생성한다."""
        self._database_path = database_path
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS analyses (
                    analysis_id TEXT PRIMARY KEY, dictionary_version TEXT NOT NULL,
                    status TEXT NOT NULL, host_reviewed INTEGER NOT NULL, error_code TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS suggestions (
                    suggestion_id TEXT PRIMARY KEY, analysis_id TEXT NOT NULL,
                    start_offset INTEGER NOT NULL, end_offset INTEGER NOT NULL,
                    review_status TEXT NOT NULL, recommended_term_json TEXT,
                    reason TEXT NOT NULL, confidence TEXT NOT NULL, decision TEXT NOT NULL,
                    decided_at TEXT, decided_by TEXT, selected_term_code TEXT,
                    issue_type TEXT, candidate_id TEXT, search_result_id TEXT,
                    FOREIGN KEY(analysis_id) REFERENCES analyses(analysis_id)
                );
                CREATE TABLE IF NOT EXISTS candidates (
                    candidate_id TEXT PRIMARY KEY, analysis_id TEXT NOT NULL,
                    start_offset INTEGER NOT NULL, end_offset INTEGER NOT NULL,
                    candidate_type TEXT NOT NULL, terms_json TEXT NOT NULL,
                    FOREIGN KEY(analysis_id) REFERENCES analyses(analysis_id)
                );
            """)
            columns = {row[1] for row in connection.execute("PRAGMA table_info(analyses)")}
            if "llm_reviewed" in columns and "host_reviewed" not in columns:
                connection.execute("ALTER TABLE analyses RENAME COLUMN llm_reviewed TO host_reviewed")
            suggestion_columns = {row[1] for row in connection.execute("PRAGMA table_info(suggestions)")}
            for name in ("issue_type", "candidate_id", "search_result_id"):
                if name not in suggestion_columns:
                    connection.execute(f"ALTER TABLE suggestions ADD COLUMN {name} TEXT")

    def save(self, result: AnalysisResult) -> None:
        """원문 전체 없이 분석 메타데이터와 제안을 하나의 트랜잭션으로 저장한다.

        Args:
            result: 저장할 분석 결과이며 원문 문서는 포함하지 않는다.

        Returns:
            반환값은 없으며 동일 analysis_id가 이미 있으면 무결성 오류를 발생시킨다.
        """
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO analyses VALUES (?, ?, ?, ?, ?, ?)",
                (result.analysis_id, result.dictionary_version, result.status.value, int(result.host_reviewed), result.error_code, _now()),
            )
            for item in result.suggestions:
                self._insert_suggestion(connection, result.analysis_id, item)
            for candidate in result.candidates:
                connection.execute(
                    "INSERT INTO candidates VALUES (?, ?, ?, ?, ?, ?)",
                    (candidate.candidate_id, result.analysis_id, candidate.start_offset, candidate.end_offset,
                     candidate.candidate_type.value, json.dumps([term.to_dict() for term in candidate.terms])),
                )

    def check_ready(self) -> None:
        """이력 SQLite가 기본 질의를 수행할 수 있는지 확인한다.

        Raises:
            StoreError: 이력 SQLite에 연결하거나 질의할 수 없을 때 발생한다.
        """
        try:
            with self._connect() as connection:
                connection.execute("SELECT 1").fetchone()
        except sqlite3.Error as error:
            raise StoreError("이력 저장소를 준비할 수 없습니다.") from error

    def get(self, analysis_id: str) -> AnalysisResult | None:
        """분석 ID로 저장된 분석과 제안을 다시 조립해 반환한다."""
        with self._connect() as connection:
            analysis = connection.execute("SELECT * FROM analyses WHERE analysis_id = ?", (analysis_id,)).fetchone()
            if analysis is None:
                return None
            rows = connection.execute(_SUGGESTION_SELECT + " WHERE analysis_id = ? ORDER BY rowid", (analysis_id,)).fetchall()
        suggestions = [_suggestion_from_row(row) for row in rows]
        return AnalysisResult(analysis[0], analysis[1], AnalysisStatus(analysis[2]), suggestions, bool(analysis[3]), analysis[4])

    def append_suggestions(self, analysis_id: str, suggestions: list[Suggestion]) -> None:
        """호스트 LLM이 제출했고 서버 검증을 통과한 제안을 분석 이력에 추가한다.

        Args:
            analysis_id: 새 제안이 속할 기존 분석 식별자다.
            suggestions: 원문 전체를 포함하지 않는 검증 완료 제안 목록이다.

        Returns:
            반환값은 없으며 존재하지 않는 분석 ID는 무결성 오류로 처리한다.
        """
        with self._connect() as connection:
            if connection.execute("SELECT 1 FROM analyses WHERE analysis_id = ?", (analysis_id,)).fetchone() is None:
                raise StoreError("분석을 찾을 수 없습니다.")
            for item in suggestions:
                self._insert_suggestion(connection, analysis_id, item)
            connection.execute("UPDATE analyses SET host_reviewed = 1 WHERE analysis_id = ?", (analysis_id,))

    def decide(self, analysis_id: str, suggestion_id: str, decision: DecisionStatus, selected_term_code: str | None, decided_by: str | None) -> Suggestion:
        """대상 분석에 속한 pending 제안만 결정하고 동일 요청은 멱등적으로 반환한다.

        Args:
            analysis_id: 제안 소유권을 확인할 분석 식별자다.
            suggestion_id: 승인 또는 거절할 제안 식별자다.
            decision: approved 또는 rejected 사용자 결정이다.
            selected_term_code: 사용자가 선택한 대체 사전 코드다.
            decided_by: 선택적으로 저장할 결정자 식별자다.

        Returns:
            갱신되었거나 같은 요청으로 재시도된 제안이다.
        """
        if decision not in (DecisionStatus.APPROVED, DecisionStatus.REJECTED):
            raise StoreError("pending 상태는 결정으로 기록할 수 없습니다.")
        with self._connect() as connection:
            row = connection.execute(_SUGGESTION_SELECT + " WHERE suggestion_id = ? AND analysis_id = ?", (suggestion_id, analysis_id)).fetchone()
            if row is None:
                raise StoreError("제안이 해당 분석에 속하지 않습니다.")
            current = _suggestion_from_row(row)
            if decision is DecisionStatus.APPROVED:
                if current.recommended_term is None or selected_term_code != current.recommended_term.term_code:
                    raise StoreError("승인 시 제안된 사전 코드와 같은 selected_term_code가 필요합니다.")
            if current.decision is not DecisionStatus.PENDING:
                if current.decision is decision and current.selected_term_code == selected_term_code:
                    return current
                raise StoreError("이미 다른 결정이 기록된 제안입니다.")
            timestamp = _now()
            connection.execute("UPDATE suggestions SET decision=?, decided_at=?, decided_by=?, selected_term_code=? WHERE suggestion_id=?", (decision.value, timestamp, decided_by, selected_term_code, suggestion_id))
            current.decision, current.decided_at, current.decided_by, current.selected_term_code = decision, timestamp, decided_by, selected_term_code
            return current

    def get_candidate(self, analysis_id: str, candidate_id: str) -> DocumentTermCandidate | None:
        """분석에 속한 원문 비포함 후보 근거를 반환한다.

        Args:
            analysis_id: 후보가 속해야 하는 분석 식별자다.
            candidate_id: 분석 도구가 발급한 후보 식별자다.

        Returns:
            후보의 범위·유형·사전 항목 또는 소유하지 않거나 없는 경우 None이다.
        """
        with self._connect() as connection:
            row = connection.execute(
                "SELECT candidate_id, start_offset, end_offset, candidate_type, terms_json FROM candidates WHERE analysis_id = ? AND candidate_id = ?",
                (analysis_id, candidate_id),
            ).fetchone()
        if row is None:
            return None
        return DocumentTermCandidate(row[0], row[1], row[2], CandidateType(row[3]), [DictionaryTerm(**term) for term in json.loads(row[4])])

    @staticmethod
    def _insert_suggestion(connection: sqlite3.Connection, analysis_id: str, item: Suggestion) -> None:
        """확장 감사 필드를 포함해 원문 없는 제안 행을 기록한다."""
        connection.execute(
            """INSERT INTO suggestions (
                suggestion_id, analysis_id, start_offset, end_offset, review_status,
                recommended_term_json, reason, confidence, decision, decided_at,
                decided_by, selected_term_code, issue_type, candidate_id, search_result_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (item.suggestion_id, analysis_id, item.start_offset, item.end_offset,
             item.review_status.value, json.dumps(item.recommended_term.to_dict()) if item.recommended_term else None,
             item.reason, item.confidence.value, item.decision.value, item.decided_at, item.decided_by,
             item.selected_term_code, item.issue_type.value if item.issue_type else None,
             item.candidate_id, item.search_result_id),
        )

    def _connect(self) -> sqlite3.Connection:
        """외래 키를 활성화한 이력 DB 연결을 반환한다."""
        connection = sqlite3.connect(self._database_path)
        connection.execute("PRAGMA foreign_keys = ON")
        return connection


_SUGGESTION_SELECT = """SELECT suggestion_id, analysis_id, start_offset, end_offset, review_status,
    recommended_term_json, reason, confidence, decision, decided_at, decided_by,
    selected_term_code, issue_type, candidate_id, search_result_id FROM suggestions"""


def _suggestion_from_row(row: tuple) -> Suggestion:
    """SQLite 행을 공개 제안 DTO로 복원한다."""
    term = DictionaryTerm(**json.loads(row[5])) if row[5] else None
    return Suggestion(
        row[0], None, row[2], row[3], ReviewStatus(row[4]), term, row[6], Confidence(row[7]),
        DecisionStatus(row[8]), row[9], row[10], row[11], IssueType(row[12]) if row[12] else None,
        row[13], row[14],
    )


def _now() -> str:
    """감사 이력에 사용할 UTC ISO-8601 시각을 생성한다."""
    return datetime.now(UTC).isoformat()
