"""문서 원문에서 사전 정확 일치 후보를 추출하고 LLM 응답을 검증한다."""

from __future__ import annotations

import re
from uuid import uuid4

from .dictionary import DictionaryRepository
from .models import (
    AnalysisResult,
    AnalysisStatus,
    CandidateType,
    Confidence,
    DictionaryTerm,
    DocumentTermCandidate,
    IssueType,
    ReviewStatus,
    Suggestion,
)


DEFAULT_MAX_ANALYSIS_SUGGESTIONS = 1_000


class ValidationError(ValueError):
    """호출자 입력 또는 LLM 구조화 응답의 계약 위반을 나타낸다."""


class SuggestionValidator:
    """호스트 LLM이 제출한 제안을 원문 범위·상태·사전 코드 기준으로 검증한다."""

    def __init__(self, dictionary: DictionaryRepository) -> None:
        """추천 코드 검증에 사용할 활성 사전 저장소를 설정한다."""
        self._dictionary = dictionary

    def validate(self, item: dict, text: str) -> Suggestion | None:
        """유효한 LLM 제안만 반환하고 계약 위반은 None으로 차단한다.

        Args:
            item: 게이트웨이가 반환한 하나의 JSON 객체다.
            text: 범위와 원문 표현을 확인할 현재 호출의 문서 텍스트다.

        Returns:
            활성 사전 코드와 일치하는 제안 또는 검증 실패 시 None이다.
        """
        try:
            start, end = int(item["start_offset"]), int(item["end_offset"])
            status = ReviewStatus(item["review_status"])
            confidence = Confidence(item["confidence"])
            candidate = item["recommended_term"]
            if not 0 <= start < end <= len(text) or status is ReviewStatus.NO_CHANGE:
                return None
            term = self._dictionary.find_by_code(candidate["source_id"], candidate["term_code"], candidate["concept_code"])
            if term is None:
                return None
            return Suggestion(str(uuid4()), text[start:end], start, end, status, term, str(item["reason"]), confidence)
        except (KeyError, TypeError, ValueError):
            return None

    def validate_review(
        self,
        item: dict,
        text: str,
        evidence_terms: list[DictionaryTerm],
        candidate: DocumentTermCandidate | None = None,
    ) -> Suggestion | None:
        """후보 또는 검색 결과에 추천 코드가 연결된 확장 제안만 반환한다.

        Args:
            item: 확장 MCP 도구가 파싱한 하나의 제안이다.
            text: 저장하지 않는 원문 전체이며 Unicode code point 범위를 검증한다.
            evidence_terms: 연결된 후보 또는 검색 결과가 허용한 사전 항목이다.
            candidate: candidate_id 근거일 때의 분석 후보다.

        Returns:
            근거·범위·활성 코드가 모두 일치한 제안 또는 실패 시 None이다.
        """
        validated = self.validate(item, text)
        if validated is None:
            return None
        try:
            issue_type = IssueType(item["issue_type"])
            if candidate is not None and (validated.start_offset != candidate.start_offset or validated.end_offset != candidate.end_offset):
                return None
            if not any(_same_term(validated.recommended_term, term) for term in evidence_terms):
                return None
            validated.issue_type = issue_type
            validated.candidate_id = item.get("candidate_id")
            validated.search_result_id = item.get("search_result_id")
            return validated
        except (KeyError, TypeError, ValueError):
            return None


class DocumentAnalyzer:
    """LLM 호출 없이 문서의 정확 일치 사전 후보를 결정적으로 추출한다."""

    def __init__(self, dictionary: DictionaryRepository, max_suggestions: int = DEFAULT_MAX_ANALYSIS_SUGGESTIONS) -> None:
        """활성 사전 저장소를 주입한다."""
        if max_suggestions < 1:
            raise ValueError("max_suggestions는 1 이상이어야 합니다.")
        self._dictionary = dictionary
        self._max_suggestions = max_suggestions

    def analyze(self, text: str) -> AnalysisResult:
        """문서에서 정확 일치 용어를 찾아 사전 근거와 문자 범위를 반환한다.

        Args:
            text: 영속화하지 않는 호출 단위 문서 원문이다.

        Returns:
            접속한 LLM 호스트가 판단에 사용할 정확 일치 분석 결과다.
        """
        if not isinstance(text, str) or not text.strip():
            raise ValidationError("text는 비어 있지 않은 문자열이어야 합니다.")
        if len(text) > 100_000:
            raise ValidationError("text는 100,000자 이하여야 합니다.")
        suggestions, truncated = self._exact_suggestions(text)
        result = AnalysisResult(
            str(uuid4()),
            self._dictionary.version,
            AnalysisStatus.PARTIALLY_COMPLETED if truncated else AnalysisStatus.COMPLETED,
            suggestions,
            error_code="result_limit_exceeded" if truncated else None,
        )
        return result

    def analyze_document(self, text: str, document_offset: int = 0) -> AnalysisResult:
        """정확·표기 변형·미확인 후보를 전체 문서 기준 범위로 분석한다.

        Args:
            text: 영속화하지 않는 전체 문서 또는 최대 100,000자 청크 원문이다.
            document_offset: 청크 시작의 전체 문서 Unicode code point 오프셋이다.

        Returns:
            후보 메타데이터만 이력에 저장 가능한 분석 결과다. 후보는 자동 수정 지시가 아니다.
        """
        self._validate_text(text)
        if not isinstance(document_offset, int) or document_offset < 0:
            raise ValidationError("document_offset은 0 이상의 Unicode code point 오프셋이어야 합니다.")
        exact, truncated = self._exact_suggestions(text)
        candidates: list[DocumentTermCandidate] = []
        seen_ranges: set[tuple[int, int]] = set()
        by_range: dict[tuple[int, int], list[DictionaryTerm]] = {}
        for suggestion in exact:
            by_range.setdefault((suggestion.start_offset, suggestion.end_offset), []).append(suggestion.recommended_term)
        for (start, end), terms in by_range.items():
            candidates.append(DocumentTermCandidate(str(uuid4()), start + document_offset, end + document_offset, CandidateType.EXACT_MATCH, terms))
            seen_ranges.add((start, end))
        for start, end, source, base in self._variant_expressions(text):
            terms = self._dictionary.find_exact(base)
            if terms:
                candidates.append(DocumentTermCandidate(str(uuid4()), start + document_offset, end + document_offset, CandidateType.VARIANT_MATCH, terms))
                seen_ranges.add((start, end))
        for start, end, source in self._token_expressions(text):
            if (start, end) in seen_ranges:
                continue
            similar = [hit.term for hit in self._dictionary.search(source, limit=3).results if hit.match_type.value == "similar"]
            if similar:
                candidates.append(DocumentTermCandidate(str(uuid4()), start + document_offset, end + document_offset, CandidateType.UNKNOWN_OR_POSSIBLE_TYPO, similar))
        return AnalysisResult(
            str(uuid4()),
            self._dictionary.version,
            AnalysisStatus.PARTIALLY_COMPLETED if truncated else AnalysisStatus.COMPLETED,
            error_code="result_limit_exceeded" if truncated else None,
            candidates=candidates,
        )

    def _exact_suggestions(self, text: str) -> tuple[list[Suggestion], bool]:
        """정확 일치 결과를 상한까지 만들고, 초과 여부를 함께 반환한다."""
        suggestions: list[Suggestion] = []
        seen: set[tuple[int, int, str, str, str]] = set()
        truncated = False

        def append(start: int, end: int, term: DictionaryTerm) -> None:
            nonlocal truncated
            key = (start, end, term.source_id, term.term_code, term.concept_code)
            if key in seen:
                return
            if len(suggestions) >= self._max_suggestions:
                truncated = True
                return
            seen.add(key)
            suggestions.append(Suggestion(str(uuid4()), text[start:end], start, end, ReviewStatus.NO_CHANGE, term, "활성 사전에 정확히 일치하는 용어입니다.", Confidence.HIGH))

        for match in re.finditer(r"[A-Za-z가-힣][A-Za-z가-힣0-9()/.+\- ]*", text):
            words = list(re.finditer(r"\S+", match.group()))
            for index in range(len(words)):
                for size in range(1, min(6, len(words) - index) + 1):
                    first, last = words[index], words[index + size - 1]
                    start, end = match.start() + first.start(), match.start() + last.end()
                    source = text[start:end]
                    for term in self._dictionary.find_exact(source):
                        append(start, end, term)
                        if truncated:
                            return suggestions, True
        for match in re.finditer(r"[가-힣]{2,}", text):
            block = match.group()
            for index in range(len(block) - 1):
                for term in self._dictionary.find_korean_prefix(block[index:index + 2]):
                    name = term.kor_name
                    if block.startswith(name, index):
                        append(match.start() + index, match.start() + index + len(name), term)
                        if truncated:
                            return suggestions, True
        return suggestions, False

    @staticmethod
    def _validate_text(text: str) -> None:
        """원문 저장 없이 문서 입력의 공통 한계를 확인한다."""
        if not isinstance(text, str) or not text.strip():
            raise ValidationError("text는 비어 있지 않은 문자열이어야 합니다.")
        if len(text) > 100_000:
            raise ValidationError("text는 100,000자 이하여야 합니다.")

    @staticmethod
    def _token_expressions(text: str) -> list[tuple[int, int, str]]:
        """한글 및 공백을 포함한 영문 용어형 표현의 원문 범위를 보존해 분리한다."""
        pattern = r"[가-힣]{2,}|[A-Za-z][A-Za-z0-9()/.+\-]*(?:\s+[A-Za-z][A-Za-z0-9()/.+\-]*)*(?:은|는|을|를|이|가|의|와|과|도|에|에서|으로|로)?"
        return [(match.start(), match.end(), match.group()) for match in re.finditer(pattern, text)]

    def _variant_expressions(self, text: str) -> list[tuple[int, int, str, str]]:
        """조사·어미가 결합했으나 기본 표기가 사전에 있는 후보를 만든다."""
        suffixes = ("에서", "으로", "은", "는", "을", "를", "이", "가", "의", "와", "과", "도", "에", "로")
        variants: list[tuple[int, int, str, str]] = []
        for start, end, source in self._token_expressions(text):
            for suffix in suffixes:
                if source.endswith(suffix) and len(source) > len(suffix) + 1:
                    base = source[:-len(suffix)]
                    if self._dictionary.find_exact(base):
                        variants.append((start, end, source, base))
                    break
        return variants


def _same_term(left: DictionaryTerm | None, right: DictionaryTerm) -> bool:
    """추천 코드가 근거 사전 항목과 완전히 같은지 확인한다."""
    return left is not None and (left.source_id, left.term_code, left.concept_code) == (right.source_id, right.term_code, right.concept_code)
