"""활성 의학용어 SQLite 스냅샷의 읽기 전용 조회를 제공한다."""

from __future__ import annotations

import json
import re
import sqlite3
from threading import RLock
from collections import OrderedDict
from difflib import SequenceMatcher
from pathlib import Path
from uuid import uuid4

from .models import DictionaryTerm, MatchType, SearchHit, SearchResult


KOREAN_PREFIX_CACHE_SIZE = 1_024


class DictionaryConfigurationError(RuntimeError):
    """활성 스냅샷 메타데이터 또는 파일이 올바르지 않을 때 발생한다."""


def normalize_term(value: str) -> str:
    """한글·영문 정확 일치 조회용 문자열을 결정적으로 정규화한다."""
    return re.sub(r"\s+", "", value).casefold()


def comparison_key(value: str) -> str:
    """공백·대소문자·일반 구분 기호 차이를 비교할 보조 키를 만든다."""
    return re.sub(r"[^\w가-힣]", "", normalize_term(value))


class DictionaryRepository:
    """active.json이 가리키는 SQLite 사전만 읽기 전용으로 조회한다."""

    def __init__(self, snapshot_root: Path) -> None:
        """사전 스냅샷 루트에서 활성 버전과 SQLite 경로를 확인한다."""
        self._root = snapshot_root
        try:
            metadata = json.loads((snapshot_root / "active.json").read_text(encoding="utf-8"))
            self.version = metadata["version"]
        except (OSError, ValueError, KeyError) as error:
            raise DictionaryConfigurationError("활성 사전 메타데이터가 올바르지 않습니다.") from error
        self._database_path = snapshot_root / "snapshots" / f"{self.version}.sqlite"
        if not self._database_path.is_file():
            raise DictionaryConfigurationError("활성 사전 SQLite 파일을 찾을 수 없습니다.")
        self._search_results: OrderedDict[str, SearchResult] = OrderedDict()
        # 분석기는 한글 연속 구간의 모든 문자 위치에서 두 글자 접두어를
        # 확인한다. 활성 스냅샷은 프로세스 수명 중 바뀌지 않으므로 결과를
        # 제한된 LRU로 재사용해 반복 SQLite 연결·질의를 피한다.
        self._korean_prefix_cache: OrderedDict[str, tuple[DictionaryTerm, ...]] = OrderedDict()
        self._korean_prefix_cache_lock = RLock()

    def find_exact(self, expression: str) -> list[DictionaryTerm]:
        """표현의 한글·영문 정규화 컬럼을 모두 조회한다.

        Args:
            expression: 문서에서 찾은 원문 표현이다.

        Returns:
            코드와 명칭을 포함한 일치 사전 항목 목록이다.
        """
        normalized = normalize_term(expression)
        compact = comparison_key(expression)
        if not normalized:
            return []
        query = """
            SELECT source_id, term_code, concept_code, eng_name, kor_name, description
            FROM snapshot_term
            WHERE normalized_eng_name IN (?, ?) OR normalized_kor_name IN (?, ?)
        """
        with self._connect() as connection:
            rows = connection.execute(query, (normalized, compact, normalized, compact)).fetchall()
        return [DictionaryTerm(*row) for row in rows]

    def check_ready(self) -> None:
        """활성 사전 파일과 필수 테이블을 읽을 수 있는지 확인한다.

        Raises:
            DictionaryConfigurationError: SQLite 파일 또는 필수 테이블을 읽을 수 없을 때 발생한다.
        """
        try:
            with self._connect() as connection:
                connection.execute("SELECT 1 FROM snapshot_term LIMIT 1").fetchone()
        except sqlite3.Error as error:
            raise DictionaryConfigurationError("활성 사전을 읽을 수 없습니다.") from error

    def find_by_code(self, source_id: str, term_code: str, concept_code: str) -> DictionaryTerm | None:
        """LLM 응답 검증에 사용할 사전 코드의 실제 존재 여부를 확인한다."""
        query = """
            SELECT source_id, term_code, concept_code, eng_name, kor_name, description
            FROM snapshot_term WHERE source_id = ? AND term_code = ? AND concept_code = ?
        """
        with self._connect() as connection:
            row = connection.execute(query, (source_id, term_code, concept_code)).fetchone()
        return DictionaryTerm(*row) if row else None

    def find_korean_prefix(self, prefix: str) -> list[DictionaryTerm]:
        """공백 없는 한글 문장 안의 용어를 찾기 위해 접두어 후보를 조회한다."""
        normalized = normalize_term(prefix)
        if len(normalized) < 2:
            return []
        with self._korean_prefix_cache_lock:
            cached = self._korean_prefix_cache.get(normalized)
            if cached is not None:
                self._korean_prefix_cache.move_to_end(normalized)
                return list(cached)
        query = """
            SELECT source_id, term_code, concept_code, eng_name, kor_name, description
            FROM snapshot_term WHERE normalized_kor_name GLOB ?
        """
        with self._connect() as connection:
            rows = connection.execute(query, (f"{normalized}*",)).fetchall()
        terms = tuple(DictionaryTerm(*row) for row in rows)
        with self._korean_prefix_cache_lock:
            # 다른 동시 요청이 같은 prefix를 먼저 채웠어도 동등한 snapshot
            # 결과이므로 새 조회 결과를 안전하게 사용한다.
            self._korean_prefix_cache[normalized] = terms
            self._korean_prefix_cache.move_to_end(normalized)
            while len(self._korean_prefix_cache) > KOREAN_PREFIX_CACHE_SIZE:
                self._korean_prefix_cache.popitem(last=False)
        return list(terms)

    def search(self, query: str, limit: int = 10, match_modes: set[MatchType] | None = None) -> SearchResult:
        """색인된 정규화 키의 제한된 접두 후보만 점수화해 검색한다.

        Args:
            query: 저장하지 않는 사용자 검색 표현이다.
            limit: 반환할 최대 결과 수이며 1에서 50 사이여야 한다.

        Returns:
            활성 사전 버전과 재검증 가능한 검색 결과 식별자를 포함한 결과다.

        Raises:
            ValueError: 비어 있는 검색어 또는 허용 범위를 벗어난 limit일 때 발생한다.
        """
        if not isinstance(query, str) or not query.strip():
            raise ValueError("query는 비어 있지 않은 문자열이어야 합니다.")
        if not 1 <= limit <= 50:
            raise ValueError("limit은 1에서 50 사이여야 합니다.")
        normalized = comparison_key(query)
        exact = self.find_exact(query)
        hits: dict[tuple[str, str, str], SearchHit] = {}
        for term in exact:
            raw_match = query == term.eng_name or query == term.kor_name
            hits[_term_key(term)] = SearchHit(term, MatchType.EXACT if raw_match else MatchType.NORMALIZED, 1.0)
            # The snapshot has no separate synonym table.  Equivalent aliases
            # published under the same normalized Korean name (for example DM
            # and Diabetes mellitus) are retrieved through its exact-name index.
            for alias in self.find_exact(term.kor_name):
                key = _term_key(alias)
                if key not in hits:
                    hits[key] = SearchHit(alias, MatchType.SYNONYM_OR_ABBREVIATION, 0.95)
        # The snapshot's normalized-name indexes are the dedicated search keys.
        # At most 200 indexed prefix candidates are read; the 339k-term table is
        # never walked for an individual similar search.
        for term in self._prefix_candidates(normalized, candidate_limit=100):
            key = _term_key(term)
            if key in hits:
                continue
            score = max(_similarity(normalized, comparison_key(term.eng_name)), _similarity(normalized, comparison_key(term.kor_name)))
            if score >= 0.45:
                hits[key] = SearchHit(term, MatchType.SIMILAR, score)
        ordered = sorted(hits.values(), key=lambda hit: (-hit.score, hit.term.kor_name, hit.term.eng_name, hit.term.term_code))
        if match_modes is not None:
            ordered = [hit for hit in ordered if hit.match_type in match_modes]
        ordered = ordered[:limit]
        result = SearchResult(str(uuid4()), self.version, ordered)
        self._search_results[result.search_result_id] = result
        self._search_results.move_to_end(result.search_result_id)
        while len(self._search_results) > 1_000:
            self._search_results.popitem(last=False)
        return result

    def get_search_result(self, search_result_id: str) -> SearchResult | None:
        """현재 활성 사전에서 발급한 제한적 검색 결과를 반환한다.

        검색 결과는 서버 프로세스 메모리에만 보관하며 원문 검색어는 이력 DB에 저장하지 않는다.
        """
        result = self._search_results.get(search_result_id)
        if result is not None:
            self._search_results.move_to_end(search_result_id)
        return result

    def _prefix_candidates(self, normalized: str, candidate_limit: int) -> list[DictionaryTerm]:
        """안전한 영문 3자 또는 한글 2자 색인 접두어로 후보 수를 제한한다."""
        korean = re.match(r"[가-힣]{2,}", normalized)
        english = re.match(r"[a-z0-9]{3,}", normalized)
        prefix = korean.group()[:2] if korean else english.group()[:3] if english else ""
        if not prefix:
            return []
        query = """
            SELECT source_id, term_code, concept_code, eng_name, kor_name, description
            FROM snapshot_term WHERE normalized_eng_name GLOB ? LIMIT ?
        """
        korean_query = """
            SELECT source_id, term_code, concept_code, eng_name, kor_name, description
            FROM snapshot_term WHERE normalized_kor_name GLOB ? LIMIT ?
        """
        with self._connect() as connection:
            rows = connection.execute(query, (f"{prefix}*", candidate_limit)).fetchall()
            rows.extend(connection.execute(korean_query, (f"{prefix}*", candidate_limit)).fetchall())
        unique: dict[tuple[str, str, str], DictionaryTerm] = {}
        for row in rows:
            term = DictionaryTerm(*row)
            unique[_term_key(term)] = term
        return list(unique.values())

    def _connect(self) -> sqlite3.Connection:
        """사전 변경을 차단하는 SQLite 읽기 전용 연결을 생성한다."""
        return sqlite3.connect(f"file:{self._database_path}?mode=ro", uri=True)


def _term_key(term: DictionaryTerm) -> tuple[str, str, str]:
    """사전 항목의 불변 식별 키를 만든다."""
    return term.source_id, term.term_code, term.concept_code


def _similarity(query: str, candidate: str) -> float:
    """제한된 색인 후보의 재현 가능한 문자열 유사도만 계산한다."""
    return round(SequenceMatcher(a=query, b=candidate).ratio(), 4)
