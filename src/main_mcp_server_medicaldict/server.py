"""의학 용어 검토 MCP 도구를 조립하고 입력·오류 계약을 제공한다."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

from mcp.server.fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

from .analysis import DocumentAnalyzer, SuggestionValidator, ValidationError
from .dictionary import DictionaryConfigurationError, DictionaryRepository
from .models import (
    AnalysisOutput,
    DocumentAnalysisOutput,
    DecisionInput,
    DecisionStatus,
    ErrorDetail,
    ErrorOutput,
    HostSuggestionInput,
    MatchType,
    ReviewSuggestionInput,
    SearchOutput,
    SuggestionOutput,
    SuggestionSubmissionOutput,
)
from .store import AnalysisStore, StoreError


MCPTransport = Literal["stdio", "streamable-http"]


def create_server(
    snapshot_root: Path | None = None,
    store_path: Path | None = None,
    *,
    host: str = "127.0.0.1",
    port: int = 8000,
) -> Any:
    """stdio 또는 Streamable HTTP 전송에 쓸 의학용어 MCP 서버를 생성한다.

    Args:
        snapshot_root: active.json과 snapshots가 있는 경로다.
        store_path: 원문을 제외한 분석·승인 이력 SQLite 파일 경로다.
        host: Streamable HTTP로 실행할 때 바인딩할 호스트다.
        port: Streamable HTTP로 실행할 때 바인딩할 포트다.

    Returns:
        등록이 완료된 FastMCP 서버 객체다.
    """
    root = snapshot_root or Path(os.environ.get("MEDICALDICT_SNAPSHOT_ROOT", "data/dictionary-snapshots"))
    history = store_path or Path(os.environ.get("MEDICALDICT_STORE_PATH", "data/analysis-history.sqlite"))
    dictionary = DictionaryRepository(root)
    analyzer = DocumentAnalyzer(dictionary)
    validator = SuggestionValidator(dictionary)
    store = AnalysisStore(history)
    server = FastMCP("medical-term-review", host=host, port=port, streamable_http_path="/mcp")

    @server.custom_route("/healthz", methods=["GET"], include_in_schema=False)
    async def healthz(_: Request) -> JSONResponse:
        """Gateway와 컨테이너가 서버 준비 상태만 확인하도록 응답한다.

        Returns:
            사전과 이력 저장소 초기화가 완료된 서버의 비민감 상태 응답이다.
        """
        return JSONResponse({"status": "ok", "service_id": "medical-term-review"})

    @server.tool(title="사전 기반 의학 용어 분석", description="먼저 호출하세요. 원문을 저장하지 않고 활성 사전의 정확 일치 용어와 문자 범위를 반환합니다. 호스트 LLM은 이 결과를 근거로 추천을 만든 뒤 submit_suggestions를 호출해야 합니다.")
    def analyze_medical_terms(text: str, document_id: str | None = None, language_hint: str | None = None) -> AnalysisOutput | ErrorOutput:
        """원문을 저장하지 않고 활성 사전 기준 용어 후보를 분석한다.

        Args:
            text: 이력 DB에 저장하지 않는 분석 대상 원문이다.
            document_id: 호출자가 관리하는 선택적 문서 식별자다.
            language_hint: 현재 분석에는 사용하지 않는 선택적 언어 힌트다.

        Returns:
            호스트 LLM의 판단 근거가 되는 분석 결과 또는 오류 본문이다.
        """
        del document_id, language_hint
        try:
            result = analyzer.analyze(text)
            store.save(result)
            return AnalysisOutput.model_validate(result.to_dict())
        except ValidationError as error:
            return _error("invalid_input", str(error), False)
        except DictionaryConfigurationError:
            return _error("dictionary_unavailable", "활성 사전을 읽을 수 없습니다.", False)

    @server.tool(title="호스트 LLM 추천 제출", description="analyze_medical_terms 뒤에만 호출하세요. 호스트가 판단한 추천을 서버가 원문 범위·허용 상태·활성 사전 코드로 검증해 저장합니다. 검증되지 않은 추천은 절대 저장하지 않습니다.")
    def submit_suggestions(analysis_id: str, text: str, suggestions: list[HostSuggestionInput]) -> SuggestionSubmissionOutput | ErrorOutput:
        """호스트 LLM의 제안을 검증해 승인 대상 이력으로 저장한다.

        Args:
            analysis_id: `analyze_medical_terms`가 반환한 분석 식별자다.
            text: 범위를 재검증할 원문이며 저장하지 않는다.
            suggestions: 호스트 LLM이 만든 구조화 제안 목록이다.

        Returns:
            저장된 제안 목록 또는 유효하지 않은 제출에 대한 오류 본문이다.
        """
        if store.get(analysis_id) is None:
            return _error("analysis_not_found", "분석을 찾을 수 없습니다.", False)
        validated = [item for item in (validator.validate(item.model_dump(mode="json"), text) for item in suggestions) if item]
        if len(validated) != len(suggestions):
            return _error("invalid_suggestion", "제안의 범위, 상태 또는 사전 코드를 검증하지 못했습니다.", False)
        try:
            store.append_suggestions(analysis_id, validated)
            return SuggestionSubmissionOutput(analysis_id=analysis_id, suggestions=[SuggestionOutput.model_validate(item.to_dict()) for item in validated])
        except StoreError as error:
            return _error("invalid_suggestion", str(error), False)

    @server.tool(title="문서 단위 의학 용어 후보 분석", description="문서 전체 또는 청크에서 정확 일치, 표기 변형 및 미확인 검토 후보를 Unicode code point 범위로 반환합니다. 후보와 신뢰도는 자동 수정 지시가 아니며, 원문은 이력에 저장하지 않습니다.")
    def analyze_document_terms(text: str, document_id: str | None = None, language_hint: str | None = None, document_offset: int = 0) -> DocumentAnalysisOutput | ErrorOutput:
        """확장 분석 후보를 저장 가능한 메타데이터와 함께 반환한다.

        Args:
            text: 저장하지 않는 전체 문서 또는 최대 100,000자 청크 원문이다.
            document_id: 호출자가 관리하는 선택적 문서 식별자이며 저장하지 않는다.
            language_hint: 선택적 언어 힌트이며 현재 후보 추출은 결정적으로 처리한다.
            document_offset: 청크 시작의 전체 문서 Unicode code point 오프셋이다.

        Returns:
            자동 수정이 아닌 검토 후보 또는 구분 가능한 오류 본문이다.
        """
        del document_id, language_hint
        try:
            result = analyzer.analyze_document(text, document_offset)
            store.save(result)
            payload = result.to_dict()
            payload["candidates"] = [
                candidate.to_dict(text[candidate.start_offset - document_offset:candidate.end_offset - document_offset])
                for candidate in result.candidates
            ]
            return DocumentAnalysisOutput.model_validate(payload)
        except ValidationError as error:
            return _error("invalid_input", str(error), False)
        except DictionaryConfigurationError:
            return _error("dictionary_unavailable", "활성 사전을 읽을 수 없습니다.", False)

    @server.tool(title="의학 용어 사전 검색", description="정확·정규화·유사 사전 검색 결과를 제한된 수로 반환합니다. 유사도와 결과는 자동 수정 또는 의학적 판단의 근거가 아니며 검토용입니다.")
    def search_medical_terms(query: str, language_hint: str | None = None, match_modes: list[MatchType] | None = None, limit: int = 10) -> SearchOutput | ErrorOutput:
        """활성 사전의 색인된 정규화 키로 제한된 검색 결과를 반환한다.

        Args:
            query: 저장하지 않는 검색 표현이다.
            language_hint: 선택적 언어 힌트이며 검색 계약에는 영향을 주지 않는다.
            match_modes: 허용할 일치 근거의 선택적 목록이다.
            limit: 반환할 결과 수로 1에서 50 사이다.

        Returns:
            결과 연결 검증에 쓸 search_result_id 또는 입력 오류 본문이다.
        """
        del language_hint
        try:
            result = dictionary.search(query, limit, set(match_modes) if match_modes else None)
            return SearchOutput.model_validate({
                "search_result_id": result.search_result_id,
                "dictionary_version": result.dictionary_version,
                "results": [
                    {"term": hit.term.to_dict(), "match_type": hit.match_type.value, "score": hit.score}
                    for hit in result.results
                ],
            })
        except ValueError as error:
            return _error("invalid_input", str(error), False)
        except DictionaryConfigurationError:
            return _error("dictionary_unavailable", "활성 사전을 읽을 수 없습니다.", False)

    @server.tool(title="검토 근거 연결 추천 제출", description="analyze_document_terms 또는 search_medical_terms 뒤에 호출하세요. 후보 또는 검색 결과에 포함된 코드만 저장합니다. 검토 후보는 자동 수정 지시가 아닙니다.")
    def submit_review_suggestions(analysis_id: str, text: str, suggestions: list[ReviewSuggestionInput]) -> SuggestionSubmissionOutput | ErrorOutput:
        """범위·후보 또는 검색 근거·활성 사전 코드를 모두 검증해 제안을 저장한다.

        Args:
            analysis_id: 확장 분석 도구가 반환한 식별자다.
            text: 분석에 사용한 동일한 전체 원문이며 저장하지 않는다.
            suggestions: 후보 또는 검색 결과에 정확히 하나로 연결된 호스트 검토 제안이다.

        Returns:
            원문 없이 저장된 제안 또는 invalid_suggestion 오류 본문이다.
        """
        if store.get(analysis_id) is None:
            return _error("analysis_not_found", "분석을 찾을 수 없습니다.", False)
        validated = []
        for item in suggestions:
            payload = item.model_dump(mode="json")
            candidate = store.get_candidate(analysis_id, item.candidate_id) if item.candidate_id else None
            if item.candidate_id and candidate is None:
                return _error("invalid_suggestion", "분석에 속한 후보 근거를 찾을 수 없습니다.", False)
            if candidate:
                evidence_terms = candidate.terms
            else:
                search_result = dictionary.get_search_result(item.search_result_id or "")
                if search_result is None or search_result.dictionary_version != dictionary.version:
                    return _error("invalid_suggestion", "현재 활성 사전의 검색 근거를 찾을 수 없습니다.", False)
                evidence_terms = [hit.term for hit in search_result.results]
            suggestion = validator.validate_review(payload, text, evidence_terms, candidate)
            if suggestion is None:
                return _error("invalid_suggestion", "제안의 범위, 상태, 근거 또는 사전 코드를 검증하지 못했습니다.", False)
            validated.append(suggestion)
        try:
            store.append_suggestions(analysis_id, validated)
            return SuggestionSubmissionOutput(analysis_id=analysis_id, suggestions=[SuggestionOutput.model_validate(item.to_dict()) for item in validated])
        except StoreError as error:
            return _error("invalid_suggestion", str(error), False)

    @server.tool(title="분석·승인 이력 조회", description="analysis_id로 저장된 제안과 사용자 결정을 조회합니다. 개인정보 보호를 위해 재조회 응답의 source_text는 null입니다.")
    def get_analysis(analysis_id: str) -> AnalysisOutput | ErrorOutput:
        """저장된 분석과 승인 이력을 반환하며 원문은 반환하지 않는다."""
        result = store.get(analysis_id)
        return AnalysisOutput.model_validate(result.to_dict()) if result else _error("analysis_not_found", "분석을 찾을 수 없습니다.", False)

    @server.tool(title="제안 승인 또는 거절", description="사람 또는 승인 권한을 가진 호출자가 마지막에 사용합니다. 같은 approved/rejected 요청의 재시도는 멱등적이며, pending 상태만 최초 결정할 수 있습니다.")
    def decide_suggestions(analysis_id: str, decisions: list[DecisionInput]) -> SuggestionSubmissionOutput | ErrorOutput:
        """분석 소유권과 pending 상태를 검증해 승인 또는 거절을 기록한다.

        Args:
            analysis_id: 제안이 속한 분석 식별자다.
            decisions: suggestion_id별 사용자 승인 또는 거절 목록이다.

        Returns:
            갱신된 제안 목록 또는 상태 전이 오류다.
        """
        try:
            updated = []
            for item in decisions:
                updated.append(store.decide(analysis_id, item.suggestion_id, item.decision, item.selected_term_code, item.decided_by))
            return SuggestionSubmissionOutput(analysis_id=analysis_id, suggestions=[SuggestionOutput.model_validate(item.to_dict()) for item in updated])
        except (KeyError, ValueError, StoreError) as error:
            return _error("invalid_decision", str(error), False)

    return server


def _error(code: str, message: str, retryable: bool) -> ErrorOutput:
    """MCP 도구 전반에서 일관된 구분 가능한 오류 본문을 만든다."""
    return ErrorOutput(error=ErrorDetail(code=code, message=message, retryable=retryable))


def main() -> None:
    """환경 변수로 선택한 stdio 또는 Streamable HTTP MCP 서버를 실행한다.

    `streamable-http`는 인증·TLS를 제공하지 않으므로 신뢰 네트워크 또는
    인증된 Gateway 뒤에서만 사용해야 한다.
    """
    transport = os.environ.get("MEDICALDICT_MCP_TRANSPORT", "stdio")
    if transport not in ("stdio", "streamable-http"):
        raise SystemExit("MEDICALDICT_MCP_TRANSPORT는 stdio 또는 streamable-http여야 합니다.")
    host = os.environ.get("MEDICALDICT_MCP_HOST", "127.0.0.1")
    try:
        port = int(os.environ.get("MEDICALDICT_MCP_PORT", "8000"))
    except ValueError as error:
        raise SystemExit("MEDICALDICT_MCP_PORT는 정수여야 합니다.") from error
    if not 1 <= port <= 65_535:
        raise SystemExit("MEDICALDICT_MCP_PORT는 1에서 65535 사이여야 합니다.")
    create_server(host=host, port=port).run(transport=transport)
