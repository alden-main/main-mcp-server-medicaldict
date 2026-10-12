# Gateway 연동 MCP 서버 공통 표준

## 목적과 적용 범위

이 문서는 여러 AI 서비스와 MCP 서버 사이에 Gateway를 도입하기 전에, 각 **MCP 서버가 공통으로 구현·문서화해야 할 운영 계약**을 정의한다. 다른 프로젝트의 MCP 서버도 이 문서를 기준으로 구현한다.

이 문서는 특정 Gateway, ID 공급자, 프록시 제품 또는 LLM 공급자를 지정하지 않는다. Gateway의 인증 방식·라우팅·정책 엔진은 별도 설계 대상으로 둔다. 다만 서버가 Gateway 뒤에서 안전하고 예측 가능하게 동작하기 위한 최소 계약은 여기서 고정한다.

## 규범 표기

- **필수(MUST)**: Gateway 연동 대상 MCP 서버가 반드시 만족해야 한다.
- **권장(SHOULD)**: 특별한 근거가 없으면 만족해야 한다.
- **선택(MAY)**: 서버 특성에 따라 적용할 수 있다.

## 1. 전송과 네트워크 경계

| 항목 | 표준 |
|---|---|
| 원격 전송 | 필수: Streamable HTTP MCP를 사용한다. |
| MCP endpoint | 필수: 서비스 루트 기준 `/mcp`를 사용한다. Gateway는 외부 경로를 내부 `/mcp`로 라우팅할 수 있다. |
| 로컬 개발 | 선택: stdio MCP를 함께 제공할 수 있다. 원격 Gateway 경로의 대체물로 사용하지 않는다. |
| 외부 노출 | 필수: 운영 MCP 서버는 Gateway 또는 인증된 프록시 뒤의 사설 네트워크에서만 접근 가능해야 한다. Gateway를 우회하는 공개 포트를 두지 않는다. |
| TLS | 필수: 외부 구간은 HTTPS/TLS를 사용한다. Gateway와 MCP 서버 간 내부 구간의 TLS 또는 mTLS 적용은 배포 보안 정책에 따른다. |
| HTTP 메서드·세션 | 필수: Streamable HTTP의 MCP 프로토콜 요구를 그대로 지원한다. 임의 REST API를 MCP endpoint에 추가하지 않는다. |

## 2. 서비스 식별과 호환성

각 서버는 다음 정보를 배포 문서와 서비스 메타데이터에 필수로 제공한다.

| 필드 | 규칙 | 예시 |
|---|---|---|
| `service_id` | 조직 내 불변, 소문자 kebab-case | `medical-term-review` |
| `service_version` | Semantic Versioning | `1.2.0` |
| `mcp_endpoint` | 서비스 내부 endpoint | `/mcp` |
| `transport` | 원격 전송 방식 | `streamable-http` |
| `tool_contract_version` | 도구 schema 호환성 버전 | `v1` |
| `data_classification` | 처리 데이터의 등급 | `sensitive-medical` |

- 필수: `service_id`는 배포·이미지·감사 로그에서 같은 값을 사용한다.
- 필수: 기존 도구의 입력/출력 schema를 호환되지 않게 바꿀 때 `tool_contract_version` 또는 주 버전을 올린다.
- 권장: Gateway는 `service_id`와 도구명을 기준으로 라우팅·인가 정책을 적용한다.

## 3. 인증과 호출자 정체성

- 필수: 외부 호출의 인증은 Gateway가 검증한다. MCP 서버는 인터넷에서 직접 호출될 수 없어야 한다.
- 필수: Gateway가 전달하는 호출자 정체성은 네트워크 격리, mTLS 또는 서명 검증 중 하나로 위조 불가능해야 한다. 단순한 임의 HTTP 헤더를 신뢰 원본으로 사용하면 안 된다.
- 필수: 서버가 호출자 정보를 사용한다면 Gateway가 검증한 claim만 사용하고, 원본 `Authorization` 토큰을 이력 DB나 일반 로그에 저장하지 않는다.
- 권장: 공통 claim 이름은 `subject_id`, `client_id`, `tenant_id`, `scopes`, `request_id`로 통일한다. Gateway가 JWT를 전달한다면 claim 이름도 동일하게 사용한다.
- 선택: 서비스가 자체 토큰 검증을 수행할 수 있다. 이 경우에도 Gateway와 scope·만료·감사 정책을 일치시킨다.

## 4. 도구 권한(scope) 계약

도구별 최소 권한을 명시한다. scope 형식은 `<service_id>:<action>`으로 통일한다.

| 동작 | scope 예시 |
|---|---|
| 조회·검색 | `<service_id>:read` |
| 분석 실행 | `<service_id>:analyze` |
| 검토·제안 제출 | `<service_id>:submit` |
| 상태 변경·승인 | `<service_id>:decide` |
| 관리성 작업 | `<service_id>:admin` |

- 필수: 상태를 변경하는 도구는 `read` scope만으로 호출 가능하면 안 된다.
- 필수: 각 서버의 도구 문서에 필요 scope와 데이터 변경 여부를 표시한다.
- 권장: Gateway가 도구 호출 전에 scope를 평가하고, 서버도 신뢰 가능한 호출자 claim이 있을 때 방어적으로 재검증한다.

## 5. 공통 오류 계약

도구 수준의 예상 가능한 오류는 아래 형식을 사용한다.

```json
{
  "error": {
    "code": "stable_machine_code",
    "message": "민감한 원문·토큰·내부 경로를 포함하지 않는 설명",
    "retryable": false
  }
}
```

| 코드 | 의미 | 재시도 |
|---|---|---|
| `invalid_input` | 입력 schema·범위·형식 오류 | 아니오 |
| `unauthenticated` | 인증 정보가 없거나 검증 실패 | 아니오 |
| `forbidden` | 인증은 됐으나 scope 또는 테넌트 권한 부족 | 아니오 |
| `not_found` | 요청한 공개 리소스·분석·식별자를 찾을 수 없음 | 아니오 |
| `conflict` | 멱등하지 않은 상태 전이 또는 버전 충돌 | 아니오 |
| `rate_limited` | 호출 한도 초과 | 예, `Retry-After` 존중 |
| `request_too_large` | 본문 또는 파일 크기 제한 초과 | 아니오 |
| `upstream_unavailable` | 필수 내부 의존성이 일시적으로 사용 불가 | 예 |
| `internal_error` | 예상하지 못한 서버 오류 | 보통 예 |

- 필수: 오류 코드는 안정된 기계 판독용 문자열이며, 내부 예외 문자열을 그대로 노출하지 않는다.
- 필수: HTTP/Gateway 차원의 인증·제한 오류와 MCP 도구 응답 오류의 책임을 문서화한다.
- 권장: `retryable=true`인 오류에는 재시도 조건과 최대 대기 시간을 응답 헤더 또는 Gateway 문서에 제공한다.

## 6. 추적성·감사·개인정보

- 필수: Gateway가 만든 `request_id` 또는 W3C `traceparent`를 수신하고, 모든 서버 로그와 최소 감사 이력에 연결한다.
- 필수: 감사 이벤트에는 최소 `timestamp`, `service_id`, `service_version`, `tool_name`, `request_id`, `subject_id` 또는 비가역 식별자, 결과 코드가 포함되어야 한다.
- 필수: 원문, 접근 토큰, 인증 헤더, 비밀번호, 진료 기록 등 민감 원문을 일반 로그·오류·추적 데이터에 기록하지 않는다.
- 필수: 테넌트 데이터가 있는 서비스는 이력 조회와 상태 변경에서 `tenant_id` 경계를 검증한다.
- 권장: 사용자 식별자는 중앙 정책에 맞춰 해시 또는 가명화한다.

## 7. 상태·확장성·멱등성

- 필수: 후속 호출에 필요한 식별자(예: `analysis_id`, `search_result_id`)가 여러 replica 중 어느 인스턴스에 요청되어도 검증 가능해야 한다.
- 필수: 프로세스 메모리만으로 보관한 상태가 있다면, 다중 replica 배포에서는 공유 저장소로 이전하거나 Gateway가 해당 수명 동안 sticky routing을 보장해야 한다.
- 필수: 상태 변경 도구는 가능한 경우 idempotency key를 받고, 동일 키의 재시도 결과를 일관되게 반환한다.
- 권장: 식별자는 추측하기 어려운 UUID 또는 동등한 무작위성을 사용한다.
- 권장: 서버는 시작 시 스키마 호환성·읽기 전용 데이터 버전·필수 의존성을 검증한다.

## 8. 리소스 한도와 복원력

각 서버는 다음 값을 문서화하고 Gateway 정책과 상한을 맞춘다.

- 최대 요청 본문 크기와 최대 입력 단위
- 도구별 timeout 및 Gateway timeout
- 동시 요청·세션·검색 결과 제한
- rate limit 단위(호출자, 테넌트, 도구 등)
- 재시도 가능한 의존성 및 backoff 정책

- 필수: 무제한 재시도, 무제한 결과, 무제한 입력을 허용하지 않는다.
- 필수: 시간 초과 또는 취소 뒤에도 상태 변경이 중복되지 않도록 설계한다.

## 9. 헬스체크와 운영 관찰성

- 필수: 컨테이너는 애플리케이션 프로세스가 실행 중인지 확인하는 healthcheck를 제공한다.
- 권장: Gateway 또는 오케스트레이터용 `/healthz` endpoint는 인증 없이도 민감 정보 없이 `200` 또는 `503`만 반환한다. MCP `/mcp` endpoint와 분리한다.
- 권장: readiness는 사전/DB 연결, 필수 schema 및 쓰기 저장소 상태를 확인한다.
- 필수: 메트릭·로그 라벨에는 `service_id`, `service_version`, 결과 코드, 도구명을 포함하고, 원문·토큰을 포함하지 않는다.

## 10. 서버별 도입 체크리스트

새 MCP 서버 또는 기존 서버를 Gateway에 연결하기 전에 다음을 확인한다.

- [ ] `/mcp` Streamable HTTP를 내부 네트워크에서 제공한다.
- [ ] `service_id`, 버전, 도구 계약 버전, 데이터 등급을 문서화했다.
- [ ] 도구별 필요 scope와 상태 변경 여부를 문서화했다.
- [ ] 공통 오류 형식과 안정된 오류 코드를 사용한다.
- [ ] `request_id`/trace context를 로그·감사 이력에 연결한다.
- [ ] 민감 원문·토큰이 로그·오류·감사 데이터에 없는지 검증했다.
- [ ] 다중 replica에서 식별자·세션·근거 검증이 유지되는지 검증했다.
- [ ] 요청 크기·timeout·결과 수·rate limit 상한을 문서화했다.
- [ ] healthcheck와 비민감 readiness 정책을 제공한다.
- [ ] Gateway 우회 경로가 없고, 외부 구간 TLS·인증 적용 위치가 정해졌다.

## 현재 의학용어 MCP 서버의 적용 상태

| 항목 | 상태 | 비고 |
|---|---|---|
| Streamable HTTP `/mcp` | 적용 | `MEDICALDICT_MCP_TRANSPORT=streamable-http` |
| stdio 개발 모드 | 적용 | 기본 전송 |
| 읽기 전용 사전·영속 이력 분리 | 적용 | 이미지 사전, Docker volume 이력 |
| 도구별 scope 검증 | 미적용 | Gateway/인증 계약 확정 후 구현 |
| 신뢰 가능한 호출자 claim | 미적용 | Gateway와 전달·검증 방식 결정 필요 |
| `request_id` 감사 연결 | 미적용 | 이력 스키마 확장과 보존 정책 결정 필요 |
| 다중 replica 검색 근거 공유 | 미적용 | `search_result_id`는 현재 프로세스 메모리 보관 |
| `/healthz` | 적용 | `GET /healthz`가 비민감 준비 상태를 반환 |

미적용 항목은 Gateway 계약과 인증·테넌트·보존 정책이 확정되기 전까지 임의 구현하지 않는다.
