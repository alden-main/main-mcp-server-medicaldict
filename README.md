# 의학 용어 검토 MCP 서버

활성 SQLite 의학용어 사전을 읽기 전용으로 조회하고, 분석·검토·승인 이력을 별도 SQLite에 원문 없이 저장하는 MCP 서버입니다. 기본 전송 방식은 **stdio**입니다.

여러 AI 서비스와 Gateway를 연동하는 MCP 서버 공통 계약은 [Gateway 연동 MCP 서버 공통 표준](docs/mcp_gateway_server_standard.md)을 따릅니다.
Gateway에 전달할 서비스별 등록 정보는 [MCP Gateway 등록 정보 명세](docs/mcp_gateway_registration.md)를 사용합니다.

## 로컬 stdio Docker 실행

이미지에는 활성 사전 스냅샷을 포함하고, 쓰기 가능한 승인 이력은 `/var/lib/medicaldict` 볼륨에만 저장합니다.

```bash
docker build -t medical-term-review-mcp:local .
docker run --rm -i \
  --mount type=volume,src=medicaldict-history,dst=/var/lib/medicaldict \
  medical-term-review-mcp:local
```

`-i`는 필수입니다. stdio MCP 프로토콜은 표준 입력과 출력을 사용합니다.

사전 위치 또는 이력 위치를 바꿔야 할 경우 다음 환경 변수를 사용합니다.

- `MEDICALDICT_SNAPSHOT_ROOT` — `active.json`과 `snapshots/`가 있는 읽기 전용 경로
- `MEDICALDICT_STORE_PATH` — 컨테이너가 쓰기 가능한 분석·승인 이력 SQLite 경로

## stdio MCP 클라이언트 연결

stdio MCP를 지원하는 LLM 호스트/클라이언트에는 아래와 같이 Docker 명령을 MCP 서버 명령으로 등록합니다. 설정 키의 정확한 이름은 사용하는 호스트 제품별 문서를 따릅니다.

```json
{
  "command": "docker",
  "args": [
    "run", "--rm", "-i",
    "--mount", "type=volume,src=medicaldict-history,dst=/var/lib/medicaldict",
    "medical-term-review-mcp:local"
  ]
}
```

FastAPI는 필요하지 않습니다. 현재 서버는 FastMCP의 stdio 전송을 직접 사용하며, LLM 서비스가 이 명령을 자식 프로세스로 실행해 MCP 메시지를 주고받습니다. 즉 LLM 서비스에는 “MCP stdio 서버 명령”으로 이 Docker 명령과 인자를 알려주면 됩니다.

## 여러 AI 서비스용 Streamable HTTP 배포

여러 AI 서비스가 도구를 공유하는 경우에는 `compose.yaml`을 사용합니다. Compose 구성은 호스트 포트를 공개하지 않고, 사전에 생성한 `mcp-gateway-network`에만 8000번 포트를 노출합니다. MCP 서버는 AI 서비스용 네트워크에 연결하지 않습니다.

```bash
docker network create mcp-gateway-network
docker compose up --build
```

Gateway는 같은 네트워크에서 다음 MCP 및 health endpoint에 연결합니다.

```text
http://medical-term-review:8000/mcp
http://medical-term-review:8000/healthz
```

외부 AI 서비스에 제공할 때는 이 컨테이너를 직접 공개하지 말고, 인증·TLS·서비스별 도구 권한·속도 제한을 담당하는 MCP Gateway 또는 리버스 프록시 뒤에 배치합니다.

```text
AI service ── HTTPS + 인증 ── Gateway / Proxy ── http://medical-term-review:8000/mcp
```

원격 MCP 클라이언트에는 Gateway가 제공하는 HTTPS endpoint를 등록합니다. 예시는 다음과 같으며, 실제 설정 키는 AI 서비스 제품의 MCP 클라이언트 문서를 따릅니다.

```json
{
  "url": "https://mcp.example.internal/medical-dictionary/mcp"
}
```

FastAPI는 필요하지 않습니다. 이 서버는 FastMCP의 `streamable-http` 전송을 직접 사용합니다. 다음 환경 변수로 실행 전송을 선택합니다.

- `MEDICALDICT_MCP_TRANSPORT`: `stdio`(기본값) 또는 `streamable-http`
- `MEDICALDICT_MCP_HOST`: HTTP 리스너 주소. 컨테이너에서는 `0.0.0.0`을 사용합니다.
- `MEDICALDICT_MCP_PORT`: HTTP 리스너 포트. 기본값은 `8000`입니다.

분석 응답은 최대 1,000개의 정확 일치 suggestion으로 제한됩니다. 한도를 넘으면 MCP 도구 호출은 성공으로 반환되지만 `status`는 `partially_completed`, `error_code`는 `result_limit_exceeded`가 됩니다. 호출자는 더 작은 문서 청크로 나누어 다시 분석해야 합니다.

이 프로젝트의 요구사항 범위에는 실제 인증·조직 권한이 포함되지 않습니다. 따라서 인증 없는 Streamable HTTP 서버를 공개 인터넷에 노출하면 안 됩니다.
