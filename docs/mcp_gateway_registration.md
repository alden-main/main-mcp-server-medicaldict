# MCP Gateway 등록 정보 명세

## 목적

이 문서는 Gateway 운영자가 개별 MCP 서버를 등록·라우팅·인가·관찰하기 위해 받아야 하는 정보를 정의한다. 각 MCP 프로젝트는 배포 전에 이 문서의 **등록 매니페스트**를 작성해 Gateway 프로젝트에 전달한다.

이 문서는 [Gateway 연동 MCP 서버 공통 표준](mcp_gateway_server_standard.md)의 서버 측 계약을 Gateway 등록 절차에 적용한 것이다. Gateway 구현체나 인증 공급자는 지정하지 않는다.

## 1. Gateway가 반드시 알아야 할 정보

| 구분 | 필수 정보 | Gateway 사용 목적 |
|---|---|---|
| 식별 | `service_id`, 서비스·도구 계약 버전, 배포 환경 | 라우팅, 정책 선택, 변경 관리 |
| 연결 | transport, 내부 endpoint, 포트, Docker network alias 또는 Service DNS | upstream 연결 |
| 보안 | 데이터 등급, 필요한 인증 방식, Gateway→서버 신뢰 방식, 테넌트 경계 | 인증·인가·네트워크 정책 |
| 권한 | 도구 목록, 도구별 scope, 상태 변경 여부 | 최소 권한 인가 |
| 제한 | 최대 본문, timeout, 동시성, rate limit 권장값, 결과 제한 | Gateway 보호·용량 계획 |
| 상태 | health/readiness 경로, 준비 조건, 장애 시 재시도 가능 여부 | healthcheck·circuit breaker |
| 관찰성 | `request_id`/trace 전달 방식, 감사 필드, 로그 금지 데이터 | 추적·감사·개인정보 보호 |
| 운영 | 소유 팀, 연락처, 배포·롤백 방법, 의존성 | 장애 대응·변경 관리 |

## 2. 등록 매니페스트 형식

Gateway 등록에는 아래 YAML 형식을 사용한다. 비밀값, 액세스 토큰, 인증서 원문, 환자·사용자 원문은 이 매니페스트에 포함하지 않는다.

```yaml
api_version: mcp-gateway-registration/v1

service:
  service_id: example-mcp
  display_name: Example MCP
  service_version: 1.0.0
  tool_contract_version: v1
  environment: development # development | staging | production
  owner:
    team: platform-tools
    contact: platform-tools@example.internal

connection:
  transport: streamable-http
  upstream_url: http://example-mcp:8000/mcp
  network:
    type: docker-external # docker-external | kubernetes-service | private-dns
    name: mcp-gateway-network
    dns_name: example-mcp
  health:
    liveness_path: null
    readiness_path: null

security:
  data_classification: internal # public | internal | confidential | sensitive-medical
  internet_exposed: false
  tenant_isolation: none # none | gateway-enforced | server-enforced | both
  gateway_to_server_trust: private-network # private-network | mtls | signed-jwt
  client_authentication: gateway-managed
  required_claims: []
  direct_access_blocked: true

tools:
  - name: example_read
    required_scopes: [example-mcp:read]
    mutates_state: false
    idempotency: not_applicable
  - name: example_update
    required_scopes: [example-mcp:write]
    mutates_state: true
    idempotency: required

limits:
  max_request_body_bytes: 1048576
  tool_timeouts_seconds:
    default: 30
  recommended_rate_limit:
    requests: 60
    interval_seconds: 60
  max_concurrent_requests: 20

observability:
  request_id:
    accepted_header: X-Request-ID
    generated_by_gateway: true
  trace_context: traceparent
  audit_fields: [timestamp, service_id, tool_name, request_id, subject_id, result_code]
  forbidden_log_data: [authorization, cookie, access_token, source_document]

reliability:
  replicas: 1
  state_model: persistent # stateless | persistent | sticky-session
  retryable_error_codes: [upstream_unavailable, internal_error]
  rollback: deploy_previous_image
```

### 매니페스트 검증 규칙

- 필수: `service_id`와 `connection.upstream_url`은 환경별로 유일해야 한다.
- 필수: `connection.transport`은 원격 연결에서 `streamable-http`여야 한다.
- 필수: `internet_exposed: false`와 `direct_access_blocked: true`가 아닌 서비스는 별도 보안 승인을 받아야 한다.
- 필수: `mutates_state: true`인 도구는 하나 이상의 쓰기 scope와 idempotency 정책을 선언해야 한다.
- 필수: `state_model: persistent` 또는 `sticky-session`인 서버는 replica 수 증가 시 상태 일관성 전략을 Gateway에 알려야 한다.
- 권장: `upstream_url`은 Docker network alias, Kubernetes Service DNS 또는 내부 DNS를 사용한다. 호스트 IP와 공개 URL을 사용하지 않는다.

## 3. Gateway 구축 시 프로젝트별 확인 절차

1. MCP 프로젝트 소유자가 등록 매니페스트와 배포된 이미지 태그를 전달한다.
2. Gateway 운영자는 `service_id`, endpoint, 네트워크 연결, `/mcp` MCP handshake를 확인한다.
3. 보안 담당자는 데이터 등급, direct access 차단, Gateway→서버 신뢰 방식, 테넌트 경계를 검토한다.
4. Gateway 운영자는 도구별 scope·rate limit·timeout·상태 변경 정책을 등록한다.
5. Gateway는 `request_id`와 trace context를 생성·전달하고, 감사 로그가 민감 원문을 포함하지 않는지 확인한다.
6. 배포 환경별로 healthcheck, 장애 재시도, circuit breaker, 롤백 절차를 검증한다.
7. 운영 승인 후에만 외부 AI 서비스에 Gateway HTTPS endpoint와 필요한 scope를 제공한다.

## 4. 의학용어 MCP 등록 매니페스트

아래 값은 현재 프로젝트 구현을 기준으로 한 초기 등록 정보다. `미확정` 값은 Gateway·인증·테넌트 정책이 확정된 뒤 갱신해야 하며, 임의 값으로 배포하면 안 된다.

```yaml
api_version: mcp-gateway-registration/v1

service:
  service_id: medical-term-review
  display_name: Medical Term Review MCP
  service_version: 0.1.0
  tool_contract_version: v1
  environment: development
  owner:
    team: 미확정
    contact: 미확정

connection:
  transport: streamable-http
  upstream_url: http://medical-term-review:8000/mcp
  network:
    type: docker-external
    name: mcp-internal-dev
    dns_name: medical-term-review
  health:
    liveness_path: /healthz
    readiness_path: /healthz

security:
  data_classification: sensitive-medical
  internet_exposed: false
  tenant_isolation: none # 테넌트 모델 미확정
  gateway_to_server_trust: private-network
  client_authentication: gateway-managed
  required_claims: [] # scope/claim 계약 미확정
  direct_access_blocked: true # MCP 서버는 mcp-gateway-network에만 연결

tools:
  - name: analyze_medical_terms
    required_scopes: [medical-term-review:analyze]
    mutates_state: true # 분석 이력 생성
    idempotency: not_available
  - name: analyze_document_terms
    required_scopes: [medical-term-review:analyze]
    mutates_state: true # 분석 이력 생성
    idempotency: not_available
  - name: search_medical_terms
    required_scopes: [medical-term-review:read]
    mutates_state: false
    idempotency: not_applicable
  - name: submit_suggestions
    required_scopes: [medical-term-review:submit]
    mutates_state: true
    idempotency: not_available
  - name: submit_review_suggestions
    required_scopes: [medical-term-review:submit]
    mutates_state: true
    idempotency: not_available
  - name: get_analysis
    required_scopes: [medical-term-review:read]
    mutates_state: false
    idempotency: not_applicable
  - name: decide_suggestions
    required_scopes: [medical-term-review:decide]
    mutates_state: true
    idempotency: partial # 동일 결정 재시도만 멱등

limits:
  max_request_body_bytes: 4194304
  tool_timeouts_seconds:
    default: 미확정
  recommended_rate_limit:
    requests: 미확정
    interval_seconds: 60
  max_concurrent_requests: 미확정

observability:
  request_id:
    accepted_header: 미구현
    generated_by_gateway: true
  trace_context: 미구현
  audit_fields: [analysis_id, suggestion_id, dictionary_version, timestamp, decision]
  forbidden_log_data: [source_document, authorization, cookie, access_token]

reliability:
  replicas: 1
  state_model: sticky-session # search_result_id가 프로세스 메모리에 있음
  retryable_error_codes: []
  rollback: 이전 Docker image 태그로 재배포
```

## 5. Gateway에 전달할 최종 인수인계 항목

각 MCP 프로젝트는 다음을 한 묶음으로 전달한다.

- 검토·승인된 등록 매니페스트
- 이미지 저장소와 immutable image tag 또는 digest
- 환경별 Compose/Kubernetes 배포 정의와 필요한 외부 네트워크 이름
- MCP endpoint handshake 검증 결과
- 도구 schema와 required scope 목록
- 데이터 분류·로그 금지 항목·보존 기간
- 장애 연락처, 배포 창, 롤백 절차
- 미확정 또는 알려진 제약 사항

Gateway 운영자는 이 항목이 없는 MCP 서버를 production route에 등록하지 않는다.
