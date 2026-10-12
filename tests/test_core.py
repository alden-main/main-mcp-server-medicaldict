"""사전 조회, 분석, 승인 이력의 핵심 계약을 검증한다."""

from __future__ import annotations

import sqlite3
import unittest
import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx

from main_mcp_server_medicaldict.analysis import DocumentAnalyzer, SuggestionValidator
from main_mcp_server_medicaldict.dictionary import DictionaryRepository
from main_mcp_server_medicaldict.models import CandidateType, DecisionStatus, IssueType, MatchType
from main_mcp_server_medicaldict.store import AnalysisStore, StoreError
from main_mcp_server_medicaldict.server import create_server


ROOT = Path(__file__).parents[1]


class MedicalDictionaryTests(unittest.TestCase):
    """외부 LLM 없이도 결정적으로 검증 가능한 동작을 모은다."""

    def setUp(self) -> None:
        """현재 활성 사전 저장소를 각 테스트에 제공한다."""
        self.repository = DictionaryRepository(ROOT / "data/dictionary-snapshots")

    def test_exact_korean_and_english_lookup(self) -> None:
        """한글과 영문 정규화 정확 일치 조회가 사전 항목을 반환한다."""
        self.assertTrue(self.repository.find_exact("당 뇨 병"))
        self.assertTrue(self.repository.find_exact("DIABETES MELLITUS"))
        self.assertTrue(self.repository.find_exact("Diabetes-Mellitus"))

    def test_analysis_preserves_source_range(self) -> None:
        """분석 응답의 범위가 호출 텍스트에서 같은 원문을 가리킨다."""
        text = "진단명은 당뇨병입니다."
        result = DocumentAnalyzer(self.repository).analyze(text)
        matches = [item for item in result.suggestions if item.source_text == "당뇨병"]
        self.assertTrue(matches)
        self.assertEqual(text[matches[0].start_offset:matches[0].end_offset], "당뇨병")

    def test_validator_rejects_invalid_term_code_and_range(self) -> None:
        """환각 사전 코드와 문서 밖 범위의 LLM 응답은 공개하지 않는다."""
        validator = SuggestionValidator(self.repository)
        invalid_code = {"start_offset": 0, "end_offset": 3, "review_status": "review_recommended", "confidence": "high", "reason": "x", "recommended_term": {"source_id": "x", "term_code": "x", "concept_code": "x"}}
        invalid_range = {"start_offset": 0, "end_offset": 99, "review_status": "review_recommended", "confidence": "high", "reason": "x", "recommended_term": {"source_id": "x", "term_code": "x", "concept_code": "x"}}
        self.assertIsNone(validator.validate(invalid_code, "당뇨병"))
        self.assertIsNone(validator.validate(invalid_range, "당뇨병"))

    def test_validated_host_suggestion_can_be_stored(self) -> None:
        """접속한 LLM 호스트의 제안은 서버 검증 후에만 분석 이력에 추가된다."""
        term = self.repository.find_exact("당뇨병")[0]
        payload = {"start_offset": 0, "end_offset": 3, "review_status": "review_recommended", "confidence": "medium", "reason": "테스트", "recommended_term": {"source_id": term.source_id, "term_code": term.term_code, "concept_code": term.concept_code}}
        suggestion = SuggestionValidator(self.repository).validate(payload, "당뇨병")
        result = DocumentAnalyzer(self.repository).analyze("당뇨병")
        with TemporaryDirectory() as directory:
            store = AnalysisStore(Path(directory) / "history.sqlite")
            store.save(result)
            store.append_suggestions(result.analysis_id, [suggestion])
            self.assertEqual(len(store.get(result.analysis_id).suggestions), len(result.suggestions) + 1)

    def test_store_has_no_document_source_and_decision_is_idempotent(self) -> None:
        """이력 DB에는 원문 열이 없고 같은 승인 재시도는 같은 결과를 반환한다."""
        result = DocumentAnalyzer(self.repository).analyze("당뇨병")
        with TemporaryDirectory() as directory:
            path = Path(directory) / "history.sqlite"
            store = AnalysisStore(path)
            store.save(result)
            columns = {row[1] for row in sqlite3.connect(path).execute("PRAGMA table_info(suggestions)")}
            self.assertNotIn("source_text", columns)
            suggestion = store.get(result.analysis_id).suggestions[0]
            self.assertIsNone(suggestion.source_text)
            first = store.decide(result.analysis_id, suggestion.suggestion_id, DecisionStatus.APPROVED, suggestion.recommended_term.term_code, "reviewer")
            second = store.decide(result.analysis_id, suggestion.suggestion_id, DecisionStatus.APPROVED, suggestion.recommended_term.term_code, "reviewer")
            self.assertEqual(first.decision, second.decision)
            with self.assertRaises(StoreError):
                store.decide(result.analysis_id, suggestion.suggestion_id, DecisionStatus.REJECTED, None, "reviewer")

    def test_document_candidates_cover_particles_typo_and_chunk_offsets(self) -> None:
        """조사 결합·오탈자 후보는 전체 문서 기준 Unicode code point 범위를 유지한다."""
        text = "당뇨병은 Diabetes Mellitus를 앓고 당뇨뼝 의심"
        offset = 17
        result = DocumentAnalyzer(self.repository).analyze_document(text, offset)
        observed = {(item.candidate_type, text[item.start_offset - offset:item.end_offset - offset]) for item in result.candidates}
        self.assertIn((CandidateType.VARIANT_MATCH, "당뇨병은"), observed)
        self.assertIn((CandidateType.VARIANT_MATCH, "Diabetes Mellitus를"), observed)
        self.assertIn((CandidateType.UNKNOWN_OR_POSSIBLE_TYPO, "당뇨뼝"), observed)
        for item in result.candidates:
            self.assertGreaterEqual(item.start_offset, offset)
            self.assertLessEqual(item.end_offset, offset + len(text))

    def test_search_is_limited_and_typo_returns_similar_result(self) -> None:
        """유사 검색은 제한된 결과만 반환하고 오탈자 후보에 사전 코드를 제공한다."""
        result = self.repository.search("당뇨뼝", limit=3)
        self.assertLessEqual(len(result.results), 3)
        self.assertTrue(result.results)
        self.assertEqual(result.dictionary_version, self.repository.version)
        self.assertIs(self.repository.get_search_result(result.search_result_id), result)
        aliases = self.repository.search("DM", limit=10)
        self.assertTrue(any(hit.match_type is MatchType.SYNONYM_OR_ABBREVIATION for hit in aliases.results))

    def test_extended_mcp_tools_are_registered(self) -> None:
        """확장 분석·검색·근거 제출 도구가 기존 호환 도구와 함께 등록된다."""
        with TemporaryDirectory() as directory:
            server = create_server(ROOT / "data/dictionary-snapshots", Path(directory) / "history.sqlite")
            names = {tool.name for tool in asyncio.run(server.list_tools())}
        self.assertTrue({"analyze_document_terms", "search_medical_terms", "submit_review_suggestions"}.issubset(names))

    def test_server_accepts_network_listener_configuration(self) -> None:
        """원격 MCP 배포는 FastMCP의 Streamable HTTP 리스너 설정을 사용한다."""
        with TemporaryDirectory() as directory:
            server = create_server(
                ROOT / "data/dictionary-snapshots", Path(directory) / "history.sqlite",
                host="0.0.0.0", port=9100,
            )
        self.assertEqual(server.settings.host, "0.0.0.0")
        self.assertEqual(server.settings.port, 9100)

    def test_healthz_returns_non_sensitive_ready_status(self) -> None:
        """Gateway의 상태 확인은 MCP 도구나 사전 내용을 노출하지 않는다."""
        with TemporaryDirectory() as directory:
            server = create_server(ROOT / "data/dictionary-snapshots", Path(directory) / "history.sqlite")

            async def request_health() -> httpx.Response:
                transport = httpx.ASGITransport(app=server.streamable_http_app())
                async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                    return await client.get("/healthz")

            response = asyncio.run(request_health())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok", "service_id": "medical-term-review"})

    def test_review_suggestion_requires_owned_candidate_and_matching_code(self) -> None:
        """확장 제안은 같은 분석 후보 범위와 그 후보가 허용한 코드에만 연결된다."""
        text = "당뇨뼝"
        result = DocumentAnalyzer(self.repository).analyze_document(text)
        candidate = next(item for item in result.candidates if item.candidate_type is CandidateType.UNKNOWN_OR_POSSIBLE_TYPO)
        with TemporaryDirectory() as directory:
            store = AnalysisStore(Path(directory) / "history.sqlite")
            store.save(result)
            restored = store.get_candidate(result.analysis_id, candidate.candidate_id)
            self.assertIsNotNone(restored)
            payload = {
                "start_offset": 0, "end_offset": 3, "review_status": "review_recommended",
                "confidence": "medium", "reason": "표기 변형 검토", "issue_type": IssueType.POSSIBLE_TYPO.value,
                "candidate_id": candidate.candidate_id,
                "recommended_term": restored.terms[0].to_dict(),
            }
            accepted = SuggestionValidator(self.repository).validate_review(payload, text, restored.terms, restored)
            self.assertIsNotNone(accepted)
            payload["end_offset"] = 2
            self.assertIsNone(SuggestionValidator(self.repository).validate_review(payload, text, restored.terms, restored))
