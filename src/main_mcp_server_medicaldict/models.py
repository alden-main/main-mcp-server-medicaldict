"""MCP 도구가 주고받는 결정적 데이터 계약을 정의한다.

원문 텍스트는 이 모듈의 분석 이력 객체에 포함하지 않는다.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class AnalysisStatus(StrEnum):
    """분석 실행의 완료 상태."""

    COMPLETED = "completed"
    PARTIALLY_COMPLETED = "partially_completed"
    FAILED = "failed"


class ReviewStatus(StrEnum):
    """사전 또는 LLM 검토 결과의 상태."""

    NO_CHANGE = "no_change"
    REVIEW_RECOMMENDED = "review_recommended"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


class Confidence(StrEnum):
    """사용자 정렬용 신뢰도이며 자동 치환 근거는 아니다."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class DecisionStatus(StrEnum):
    """제안에 대해 기록된 사용자 결정 상태."""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class CandidateType(StrEnum):
    """문서 표현이 사전과 맺는 후보 관계이며 자동 수정 지시가 아니다."""

    EXACT_MATCH = "exact_match"
    VARIANT_MATCH = "variant_match"
    UNKNOWN_OR_POSSIBLE_TYPO = "unknown_or_possible_typo"


class MatchType(StrEnum):
    """검색 결과의 결정적 일치 근거다."""

    EXACT = "exact"
    NORMALIZED = "normalized"
    SYNONYM_OR_ABBREVIATION = "synonym_or_abbreviation"
    SIMILAR = "similar"


class IssueType(StrEnum):
    """호스트가 검토 대상으로 표시한 문제의 종류다."""

    POSSIBLE_TYPO = "possible_typo"
    VARIANT = "variant"
    OTHER = "other"


@dataclass(frozen=True, slots=True)
class DictionaryTerm:
    """활성 SQLite 사전에서 조회한 단일 용어 항목."""

    source_id: str
    term_code: str
    concept_code: str
    eng_name: str
    kor_name: str
    description: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """MCP 응답에 쓸 공개 사전 항목 사전을 반환한다."""
        return asdict(self)


@dataclass(slots=True)
class Suggestion:
    """문서의 한 문자 범위에 대한 검토 결과와 승인 상태."""

    suggestion_id: str
    source_text: str | None
    start_offset: int
    end_offset: int
    review_status: ReviewStatus
    recommended_term: DictionaryTerm | None
    reason: str
    confidence: Confidence
    decision: DecisionStatus = DecisionStatus.PENDING
    decided_at: str | None = None
    decided_by: str | None = None
    selected_term_code: str | None = None
    issue_type: IssueType | None = None
    candidate_id: str | None = None
    search_result_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """열거형을 문자열로 변환해 MCP 직렬화 가능한 사전을 반환한다."""
        value = asdict(self)
        value["review_status"] = self.review_status.value
        value["confidence"] = self.confidence.value
        value["decision"] = self.decision.value
        if self.issue_type is not None:
            value["issue_type"] = self.issue_type.value
        return value


@dataclass(frozen=True, slots=True)
class DocumentTermCandidate:
    """원문 범위와 사전 근거를 가진 검토 후보다.

    후보는 사람 또는 호스트의 검토 입력일 뿐 자동 수정 권한을 부여하지 않는다.
    """

    candidate_id: str
    start_offset: int
    end_offset: int
    candidate_type: CandidateType
    terms: list[DictionaryTerm] = field(default_factory=list)

    def to_dict(self, source_text: str | None) -> dict[str, Any]:
        """분석 직후에만 원문 표현을 포함한 MCP 응답을 만든다."""
        return {
            "candidate_id": self.candidate_id,
            "source_text": source_text,
            "start_offset": self.start_offset,
            "end_offset": self.end_offset,
            "candidate_type": self.candidate_type.value,
            "terms": [term.to_dict() for term in self.terms],
            "automatic_correction": False,
        }


@dataclass(frozen=True, slots=True)
class SearchResult:
    """제한된 사전 검색이 반환한 재검증 가능한 결과 묶음이다."""

    search_result_id: str
    dictionary_version: str
    results: list["SearchHit"]


@dataclass(frozen=True, slots=True)
class SearchHit:
    """하나의 사전 검색 결과와 정렬 근거다."""

    term: DictionaryTerm
    match_type: MatchType
    score: float


@dataclass(slots=True)
class AnalysisResult:
    """원문을 제외한 하나의 분석 실행 결과."""

    analysis_id: str
    dictionary_version: str
    status: AnalysisStatus
    suggestions: list[Suggestion] = field(default_factory=list)
    host_reviewed: bool = False
    error_code: str | None = None
    candidates: list[DocumentTermCandidate] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """분석 결과를 MCP 응답 형식으로 반환한다."""
        return {
            "analysis_id": self.analysis_id,
            "dictionary_version": self.dictionary_version,
            "status": self.status.value,
            "suggestions": [item.to_dict() for item in self.suggestions],
            "host_reviewed": self.host_reviewed,
            "error_code": self.error_code,
        }


class SuggestedTermInput(BaseModel):
    """호스트 LLM이 추천 사전 항목을 식별하기 위해 제출하는 코드 묶음."""

    model_config = ConfigDict(extra="forbid")

    source_id: str = Field(description="활성 사전의 source_id 값", examples=["medical-term-mysql"])
    term_code: str = Field(description="활성 사전의 term_code 값", examples=["H00000025"])
    concept_code: str = Field(description="활성 사전의 concept_code 값", examples=["H00000025"])


class HostSuggestionInput(BaseModel):
    """접속한 LLM 호스트가 검증을 위해 제출하는 단일 추천이다."""

    model_config = ConfigDict(extra="forbid")

    start_offset: int = Field(ge=0, description="입력 text 안 추천 대상 표현의 시작 문자 오프셋")
    end_offset: int = Field(gt=0, description="입력 text 안 추천 대상 표현 직후의 문자 오프셋")
    review_status: Literal[ReviewStatus.REVIEW_RECOMMENDED, ReviewStatus.INSUFFICIENT_EVIDENCE] = Field(description="호스트가 제출할 수 있는 검토 상태. no_change는 제출하지 않는다.")
    recommended_term: SuggestedTermInput = Field(description="새로 만들지 않은 활성 사전 항목 코드")
    reason: str = Field(min_length=1, max_length=1_000, description="추천의 짧은 근거. 원문 전체를 복사하지 않는다.")
    confidence: Confidence = Field(description="자동 수정 근거가 아닌 사용자 검토 우선순위")


class ReviewSuggestionInput(HostSuggestionInput):
    """후보 또는 검색 근거에 연결해 제출하는 확장 검토 제안이다."""

    issue_type: IssueType
    candidate_id: str | None = None
    search_result_id: str | None = None

    def model_post_init(self, __context: Any) -> None:
        """두 근거 중 정확히 하나가 있어야 함을 결정적으로 확인한다."""
        if bool(self.candidate_id) == bool(self.search_result_id):
            raise ValueError("candidate_id 또는 search_result_id 중 하나를 제공해야 합니다.")


class DecisionInput(BaseModel):
    """사용자가 pending 제안에 내린 승인 또는 거절 입력이다."""

    model_config = ConfigDict(extra="forbid")

    suggestion_id: str = Field(description="대상 분석에 속하는 pending suggestion_id")
    decision: Literal[DecisionStatus.APPROVED, DecisionStatus.REJECTED] = Field(description="approved 또는 rejected. pending은 허용하지 않는다.")
    selected_term_code: str | None = Field(default=None, description="승인 시 추천 사전 코드와 같은 값")
    decided_by: str | None = Field(default=None, max_length=200, description="선택적 결정자 식별자")


class DictionaryTermOutput(BaseModel):
    """MCP 응답에 공개하는 사전 항목이다."""

    source_id: str
    term_code: str
    concept_code: str
    eng_name: str
    kor_name: str
    description: str | None = None


class SuggestionOutput(BaseModel):
    """분석 또는 승인 조회가 반환하는 제안이다."""

    suggestion_id: str
    source_text: str | None = Field(description="분석 직후에만 제공되며 재조회에서는 원문 비영속화로 null")
    start_offset: int
    end_offset: int
    review_status: ReviewStatus
    recommended_term: DictionaryTermOutput | None
    reason: str
    confidence: Confidence
    decision: DecisionStatus
    decided_at: str | None = None
    decided_by: str | None = None
    selected_term_code: str | None = None
    issue_type: IssueType | None = None
    candidate_id: str | None = None
    search_result_id: str | None = None


class DocumentTermCandidateOutput(BaseModel):
    """문서 분석 직후에만 원문 표현을 포함하는 후보 응답이다."""

    candidate_id: str
    source_text: str
    start_offset: int
    end_offset: int
    candidate_type: CandidateType
    terms: list[DictionaryTermOutput]
    automatic_correction: Literal[False] = False


class SearchHitOutput(BaseModel):
    """사전 검색의 단일 결과다."""

    term: DictionaryTermOutput
    match_type: MatchType
    score: float = Field(ge=0, le=1)


class SearchOutput(BaseModel):
    """검색 결과 식별자와 제한된 사전 후보를 반환한다."""

    search_result_id: str
    dictionary_version: str
    results: list[SearchHitOutput]


class AnalysisOutput(BaseModel):
    """사전 기반 분석과 저장된 제안을 반환하는 성공 응답이다."""

    analysis_id: str
    dictionary_version: str
    status: AnalysisStatus
    suggestions: list[SuggestionOutput]
    host_reviewed: bool
    error_code: str | None = None


class DocumentAnalysisOutput(AnalysisOutput):
    """확장 문서 분석의 후보 목록과 전체 문서 기준 범위를 반환한다."""

    candidates: list[DocumentTermCandidateOutput]


class SuggestionSubmissionOutput(BaseModel):
    """서버 검증을 통과해 저장된 호스트 제안 응답이다."""

    analysis_id: str
    suggestions: list[SuggestionOutput]


class ErrorDetail(BaseModel):
    """모든 MCP 도구가 공통으로 반환하는 오류 세부 정보다."""

    code: str = Field(description="호출자가 분기할 수 있는 안정된 오류 코드")
    message: str = Field(description="원문을 포함하지 않는 사용자용 오류 설명")
    retryable: bool = Field(description="같은 요청을 재시도할 수 있는지 여부")


class ErrorOutput(BaseModel):
    """MCP 도구 실패 시의 표준 응답이다."""

    error: ErrorDetail
