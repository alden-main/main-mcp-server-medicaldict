"""의학 용어 검토 MCP 도구를 조립하고 입력·오류 계약을 제공한다."""

from __future__ import annotations

import os
import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any, Literal

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import CallToolResult, TextContent
from pydantic import ValidationError as PydanticValidationError
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
DEFAULT_MAX_TEXT_CHARS = 100_000
DEFAULT_MAX_REQUEST_BODY_BYTES = 1_048_576
audit_logger = logging.getLogger("medical_term_review.audit")


class GatewayFastMCP(FastMCP):
    """입력 검증 실패도 Gateway가 해석할 MCP tool result로 바꾸는 FastMCP다."""

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        """도구 인자 스키마 오류를 안전한 `invalid_input` 결과로 정규화한다."""
        started = time.perf_counter()
        try:
            result = await super().call_tool(name, arguments)
        except ToolError as error:
            if isinstance(error.__cause__, PydanticValidationError):
                result = _error("invalid_input", "도구 입력이 계약에 맞지 않습니다.", False)
            else:
                result = _error("internal_error", "도구 실행 중 내부 오류가 발생했습니다.", True)
        _audit_tool_call(name, result, started)
        return result


def create_server(
    snapshot_root: Path | None = None,
    store_path: Path | None = None,
    *,
    host: str = "127.0.0.1",
    port: int = 8000,
    max_text_chars: int = DEFAULT_MAX_TEXT_CHARS,
    max_request_body_bytes: int = DEFAULT_MAX_REQUEST_BODY_BYTES,
) -> Any:
    """stdio 또는 Streamable HTTP 전송에 쓸 의학용어 MCP 서버를 생성한다.

    Args:
        snapshot_root: active.json과 snapshots가 있는 경로다.
        store_path: 원문을 제외한 분석·승인 이력 SQLite 파일 경로다.
        host: Streamable HTTP로 실행할 때 바인딩할 호스트다.
        port: Streamable HTTP로 실행할 때 바인딩할 포트다.
        max_text_chars: 분석·제안 도구가 허용할 최대 Unicode code-point 문자 수다.
        max_request_body_bytes: Streamable HTTP 요청 본문의 최대 바이트 수다.

    Returns:
        등록이 완료된 FastMCP 서버 객체다.
    """
    root = snapshot_root or Path(os.environ.get("MEDICALDICT_SNAPSHOT_ROOT", "data/dictionary-snapshots"))
    history = store_path or Path(os.environ.get("MEDICALDICT_STORE_PATH", "data/analysis-history.sqlite"))
    if max_text_chars < 1 or max_request_body_bytes < 1:
        raise ValueError("요청 및 text 크기 제한은 1 이상이어야 합니다.")
    dictionary: DictionaryRepository | None = None
    analyzer: DocumentAnalyzer | None = None
    validator: SuggestionValidator | None = None
    store: AnalysisStore | None = None
    server = GatewayFastMCP(
        "medical-term-review",
        host=host,
        port=port,
        streamable_http_path="/mcp",
        # Gateway는 요청마다 discovery를 다시 수행한다. 이력은 SQLite에 있으므로
        # MCP 세션이 필요 없고, 세션을 만들면 discovery마다 종료되지 않는 세션이 쌓인다.
        stateless_http=True,
        json_response=True,
        max_request_body_size=max_request_body_bytes,
    )

    def dependencies() -> tuple[DictionaryRepository, DocumentAnalyzer, SuggestionValidator, AnalysisStore] | None:
        """사전과 이력 저장소의 준비 상태를 확인하고 필요하면 다시 연다."""
        nonlocal dictionary, analyzer, validator, store
        try:
            if dictionary is None:
                dictionary = DictionaryRepository(root)
                analyzer = DocumentAnalyzer(dictionary)
                validator = SuggestionValidator(dictionary)
            dictionary.check_ready()
            if store is None:
                store = AnalysisStore(history)
            store.check_ready()
        except (DictionaryConfigurationError, StoreError, OSError, sqlite3.Error):
            dictionary = analyzer = validator = store = None
            return None
        return dictionary, analyzer, validator, store

    @server.custom_route("/healthz", methods=["GET"], include_in_schema=False)
    async def healthz(_: Request) -> JSONResponse:
        """Gateway와 컨테이너가 서버 준비 상태만 확인하도록 응답한다.

        Returns:
            사전과 이력 저장소 초기화가 완료된 서버의 비민감 상태 응답이다.
        """
        ready = dependencies() is not None
        return JSONResponse(
            {"status": "ok" if ready else "unavailable", "service_id": "medical-term-review"},
            status_code=200 if ready else 503,
        )

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
        components = dependencies()
        if components is None:
            return _error("dictionary_unavailable", "활성 사전 또는 이력 저장소를 준비할 수 없습니다.", False)
        _, active_analyzer, _, active_store = components
        if oversized := _text_too_large(text, max_text_chars):
            return oversized
        try:
            result = active_analyzer.analyze(text)
            active_store.save(result)
            return AnalysisOutput.model_validate(result.to_dict())
        except ValidationError as error:
            return _error("invalid_input", str(error), False)
        except (DictionaryConfigurationError, StoreError):
            return _error("dictionary_unavailable", "활성 사전 또는 이력 저장소를 준비할 수 없습니다.", False)
        except Exception:
            return _error("internal_error", "의학용어 분석 중 내부 오류가 발생했습니다.", True)

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
        components = dependencies()
        if components is None:
            return _error("dictionary_unavailable", "활성 사전 또는 이력 저장소를 준비할 수 없습니다.", False)
        _, _, active_validator, active_store = components
        if oversized := _text_too_large(text, max_text_chars):
            return oversized
        if active_store.get(analysis_id) is None:
            return _error("analysis_not_found", "분석을 찾을 수 없습니다.", False)
        validated = [item for item in (active_validator.validate(item.model_dump(mode="json"), text) for item in suggestions) if item]
        if len(validated) != len(suggestions):
            return _error("invalid_suggestion", "제안의 범위, 상태 또는 사전 코드를 검증하지 못했습니다.", False)
        try:
            active_store.append_suggestions(analysis_id, validated)
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
        components = dependencies()
        if components is None:
            return _error("dictionary_unavailable", "활성 사전 또는 이력 저장소를 준비할 수 없습니다.", False)
        _, active_analyzer, _, active_store = components
        if oversized := _text_too_large(text, max_text_chars):
            return oversized
        try:
            result = active_analyzer.analyze_document(text, document_offset)
            active_store.save(result)
            payload = result.to_dict()
            payload["candidates"] = [
                candidate.to_dict(text[candidate.start_offset - document_offset:candidate.end_offset - document_offset])
                for candidate in result.candidates
            ]
            return DocumentAnalysisOutput.model_validate(payload)
        except ValidationError as error:
            return _error("invalid_input", str(error), False)
        except (DictionaryConfigurationError, StoreError):
            return _error("dictionary_unavailable", "활성 사전 또는 이력 저장소를 준비할 수 없습니다.", False)
        except Exception:
            return _error("internal_error", "의학용어 분석 중 내부 오류가 발생했습니다.", True)

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
        components = dependencies()
        if components is None:
            return _error("dictionary_unavailable", "활성 사전 또는 이력 저장소를 준비할 수 없습니다.", False)
        active_dictionary = components[0]
        try:
            result = active_dictionary.search(query, limit, set(match_modes) if match_modes else None)
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
        except Exception:
            return _error("internal_error", "의학용어 검색 중 내부 오류가 발생했습니다.", True)

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
        components = dependencies()
        if components is None:
            return _error("dictionary_unavailable", "활성 사전 또는 이력 저장소를 준비할 수 없습니다.", False)
        active_dictionary, _, active_validator, active_store = components
        if oversized := _text_too_large(text, max_text_chars):
            return oversized
        if active_store.get(analysis_id) is None:
            return _error("analysis_not_found", "분석을 찾을 수 없습니다.", False)
        validated = []
        for item in suggestions:
            payload = item.model_dump(mode="json")
            candidate = active_store.get_candidate(analysis_id, item.candidate_id) if item.candidate_id else None
            if item.candidate_id and candidate is None:
                return _error("invalid_suggestion", "분석에 속한 후보 근거를 찾을 수 없습니다.", False)
            if candidate:
                evidence_terms = candidate.terms
            else:
                search_result = active_dictionary.get_search_result(item.search_result_id or "")
                if search_result is None or search_result.dictionary_version != active_dictionary.version:
                    return _error("invalid_suggestion", "현재 활성 사전의 검색 근거를 찾을 수 없습니다.", False)
                evidence_terms = [hit.term for hit in search_result.results]
            suggestion = active_validator.validate_review(payload, text, evidence_terms, candidate)
            if suggestion is None:
                return _error("invalid_suggestion", "제안의 범위, 상태, 근거 또는 사전 코드를 검증하지 못했습니다.", False)
            validated.append(suggestion)
        try:
            active_store.append_suggestions(analysis_id, validated)
            return SuggestionSubmissionOutput(analysis_id=analysis_id, suggestions=[SuggestionOutput.model_validate(item.to_dict()) for item in validated])
        except StoreError as error:
            return _error("invalid_suggestion", str(error), False)

    @server.tool(title="분석·승인 이력 조회", description="analysis_id로 저장된 제안과 사용자 결정을 조회합니다. 개인정보 보호를 위해 재조회 응답의 source_text는 null입니다.")
    def get_analysis(analysis_id: str) -> AnalysisOutput | ErrorOutput:
        """저장된 분석과 승인 이력을 반환하며 원문은 반환하지 않는다."""
        components = dependencies()
        if components is None:
            return _error("dictionary_unavailable", "활성 사전 또는 이력 저장소를 준비할 수 없습니다.", False)
        result = components[3].get(analysis_id)
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
        components = dependencies()
        if components is None:
            return _error("dictionary_unavailable", "활성 사전 또는 이력 저장소를 준비할 수 없습니다.", False)
        active_store = components[3]
        try:
            updated = []
            for item in decisions:
                updated.append(active_store.decide(analysis_id, item.suggestion_id, item.decision, item.selected_term_code, item.decided_by))
            return SuggestionSubmissionOutput(analysis_id=analysis_id, suggestions=[SuggestionOutput.model_validate(item.to_dict()) for item in updated])
        except (KeyError, ValueError, StoreError) as error:
            return _error("invalid_decision", str(error), False)

    return server


def _error(code: str, message: str, retryable: bool) -> CallToolResult:
    """실패를 `isError`와 구조화 오류를 가진 MCP tool result로 만든다."""
    payload = ErrorOutput(error=ErrorDetail(code=code, message=message, retryable=retryable)).model_dump(mode="json")
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))],
        structuredContent={"result": payload},
        isError=True,
    )


def _text_too_large(text: str, max_text_chars: int) -> CallToolResult | None:
    """원문이 분석·저장·로그 전에 문자 한도를 넘는지 확인한다."""
    if isinstance(text, str) and len(text) > max_text_chars:
        return _error("request_too_large", f"text는 {max_text_chars:,}자 이하여야 합니다.", False)
    return None


def _audit_tool_call(name: str, result: Any, started: float) -> None:
    """원문 없이 MCP request ID, 결과 코드와 실행 시간을 감사 로그에 남긴다."""
    request_id = None
    try:
        from mcp.server.lowlevel.server import request_ctx
        request_id = request_ctx.get().request_id
    except LookupError:
        pass
    code = "ok"
    dictionary_version = "unavailable"
    structured = result.structuredContent if isinstance(result, CallToolResult) else None
    if isinstance(result, tuple) and len(result) == 2 and isinstance(result[1], dict):
        structured = result[1]
    if isinstance(structured, dict):
        payload = structured.get("result", {})
        if isinstance(payload, dict):
            dictionary_version = str(payload.get("dictionary_version", dictionary_version))
    if isinstance(result, CallToolResult) and result.isError:
        code = str((structured or {}).get("result", {}).get("error", {}).get("code", "internal_error"))
    audit_logger.info(
        "tool_call request_id=%s tool=%s result_code=%s duration_ms=%d service_version=%s dictionary_version=%s",
        request_id,
        name,
        code,
        round((time.perf_counter() - started) * 1000),
        "0.1.0",
        dictionary_version,
    )


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
    try:
        max_text_chars = int(os.environ.get("MEDICALDICT_MAX_TEXT_CHARS", str(DEFAULT_MAX_TEXT_CHARS)))
        max_request_body_bytes = int(os.environ.get("MEDICALDICT_MAX_REQUEST_BODY_BYTES", str(DEFAULT_MAX_REQUEST_BODY_BYTES)))
    except ValueError as error:
        raise SystemExit("MEDICALDICT_MAX_TEXT_CHARS와 MEDICALDICT_MAX_REQUEST_BODY_BYTES는 정수여야 합니다.") from error
    try:
        server = create_server(
            host=host,
            port=port,
            max_text_chars=max_text_chars,
            max_request_body_bytes=max_request_body_bytes,
        )
    except ValueError as error:
        raise SystemExit(str(error)) from error
    server.run(transport=transport)
