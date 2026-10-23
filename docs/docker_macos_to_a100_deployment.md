# macOS에서 빌드하여 Linux A100 서버에 배포하기

이 문서는 macOS 개발 PC에서 이 프로젝트의 Docker 이미지를 만들고, 별도의 Linux A100 서버에서 MCP Gateway 뒤의 내부 MCP 서버로 실행하는 절차를 설명한다.

`medical-term-review` 서비스는 GPU를 사용하지 않는다. A100 서버에 배포하더라도 NVIDIA Container Toolkit, `--gpus` 옵션, CUDA 기반 이미지는 필요하지 않다. 일반적인 A100 서버는 x86_64 Linux이므로 macOS에서 `linux/amd64` 이미지를 만들어 전달하면 된다.

## 배포 구조와 전제 조건

```text
macOS 개발 PC                                  Linux A100 서버
---------------                                ----------------
소스 + data/dictionary-snapshots/ ─(Docker)──→ medical-term-review-mcp:local
                                                     │
                                             Docker Compose 실행
                                                     │
                                      named volume: medicaldict-history
                                      (분석·승인 이력 SQLite만 영속화)
                                                     │
                                      사설 Docker 네트워크
                                      mcp-gateway-network
                                                     │
                                     MCP Gateway → medical-term-review:8000/mcp
```

- 활성 사전 스냅샷(`data/dictionary-snapshots/active.json`, `snapshots/`)은 이미지의 읽기 전용 계층에 포함된다. 별도 SQLite 사전 파일을 생성하거나 서버에 마운트하지 않는다.
- 분석·검토·승인 이력은 Docker named volume `medicaldict-history`의 `/var/lib/medicaldict/analysis-history.sqlite`에만 기록된다. 원문은 이력 DB에 저장하지 않는다.
- `compose.yaml`은 호스트 포트를 공개하지 않는다. 서비스는 외부 Docker 네트워크 `mcp-gateway-network`에만 연결되고, Gateway는 서비스 이름 `medical-term-review`로 접근한다.
- Apple Silicon macOS에서는 기본 이미지가 arm64가 될 수 있다. x86_64 Linux 서버에는 반드시 `linux/amd64` 이미지를 전달한다. 서버의 `uname -m`이 `aarch64`이면 아래 `linux/amd64`를 `linux/arm64`로 바꾼다.
- 개발 PC에는 Docker Desktop(Buildx 포함), `scp`, `ssh`가 필요하다. 서버에는 Docker Engine과 Docker Compose v2 플러그인이 필요하다.

이 문서는 이미지 버전 `0.1.0`, 서버 배포 경로 `/srv/main-mcp-server-medicaldict`를 예시로 사용한다. 운영에서는 승인된 버전과 경로로 바꾼다.

## 1. macOS에서 사전 스냅샷과 테스트 확인

이미지에 포함될 활성 사전 파일이 존재하는지 확인하고 테스트를 실행한다. 현재 프로젝트에는 별도의 XLS/SQLite 사전 빌드 단계가 없다.

```bash
test -f data/dictionary-snapshots/active.json
test -d data/dictionary-snapshots/snapshots

uv sync
uv run pytest
```

배포 대상 사전의 상태를 기록하려면 이미지 빌드 전에 활성 포인터와 포함 파일의 해시를 보관한다.

```bash
cat data/dictionary-snapshots/active.json
shasum -a 256 data/dictionary-snapshots/active.json
find data/dictionary-snapshots/snapshots -maxdepth 1 -type f \
  -exec shasum -a 256 {} +
```

`Dockerfile`은 `data/dictionary-snapshots/` 전체를 `/app/data/dictionary-snapshots`로 복사한 뒤 해당 경로를 쓰기 불가능하게 만든다. 따라서 사전 변경은 새 이미지를 빌드해 배포하는 방식으로만 반영된다.

## 2. macOS에서 Linux용 이미지 빌드 및 내보내기

먼저 A100 서버의 CPU 아키텍처를 확인한다.

```bash
ssh <서버_사용자>@<서버_호스트> 'uname -m'
```

일반적인 A100 x86_64 서버의 출력은 `x86_64`다. 단일 플랫폼 이미지를 로컬 Docker 저장소로 가져오도록 `--load`를 함께 사용한다.

```bash
docker buildx build \
  --platform linux/amd64 \
  --tag medical-term-review-mcp:0.1.0 \
  --load \
  .

docker image inspect medical-term-review-mcp:0.1.0 \
  --format '{{.Os}}/{{.Architecture}}'
# 기대값: linux/amd64

mkdir -p dist
docker tag medical-term-review-mcp:0.1.0 medical-term-review-mcp:local
docker save medical-term-review-mcp:0.1.0 medical-term-review-mcp:local \
  | gzip > dist/medical-term-review-mcp_local_linux-amd64.tar.gz
shasum -a 256 dist/medical-term-review-mcp_local_linux-amd64.tar.gz
```

현재 `compose.yaml`의 이미지 이름은 `medical-term-review-mcp:local`이다. 그러므로 서버에서 별도 빌드 없이 이 이미지를 실행하려면 위와 같이 `local` 태그를 포함해 내보낸다. 아카이브에는 승인 버전 태그(`0.1.0`)도 함께 보존하므로 서버에서 롤백 대상을 식별할 수 있다.

이미지 레지스트리를 사용하는 환경에서는 이 파일 전달 대신 immutable 태그 또는 digest를 `docker push`/`docker pull`로 관리할 수 있다. 이 문서는 레지스트리 없이 이미지 파일을 전달하는 절차를 기준으로 한다.

## 3. 서버에 Compose 파일과 이미지 전달

서버에 배포 경로를 만든다. 기존 배포 파일과 named volume은 삭제하지 않는다.

```bash
ssh <서버_사용자>@<서버_호스트> \
  'sudo install -d -m 0755 /srv/main-mcp-server-medicaldict'
```

macOS에서 현재 프로젝트의 Compose 파일과 이미지 아카이브를 전달한다.

```bash
scp compose.yaml \
  <서버_사용자>@<서버_호스트>:/tmp/medical-term-review-compose.yaml
scp dist/medical-term-review-mcp_local_linux-amd64.tar.gz \
  <서버_사용자>@<서버_호스트>:/tmp/
```

서버에서 macOS에서 기록한 SHA-256 값과 받은 파일의 값을 대조한 후 이미지와 Compose 파일을 설치한다.

```bash
ssh <서버_사용자>@<서버_호스트>

cd /tmp
sha256sum medical-term-review-mcp_local_linux-amd64.tar.gz

sudo mv medical-term-review-compose.yaml \
  /srv/main-mcp-server-medicaldict/compose.yaml
sudo docker load -i medical-term-review-mcp_local_linux-amd64.tar.gz
sudo docker image inspect medical-term-review-mcp:local \
  --format '{{.Os}}/{{.Architecture}}'
# 기대값: linux/amd64
```

배포 계정이 `/srv/main-mcp-server-medicaldict`의 소유자라면 `sudo mv` 대신 해당 계정 권한으로 파일을 옮겨도 된다.

## 4. Gateway 전용 네트워크 준비

`compose.yaml`은 `mcp-gateway-network`를 외부 네트워크로 선언하므로 시작 전에 존재해야 한다. Gateway 스택이 이미 만들었다면 다시 생성하지 않는다. 없을 때만 생성한다.

```bash
sudo docker network inspect mcp-gateway-network >/dev/null 2>&1 \
  || sudo docker network create --internal mcp-gateway-network
```

MCP 서버는 이 네트워크에만 연결해야 한다. Gateway는 AI 서비스 네트워크와 `mcp-gateway-network` 양쪽에 연결해 호출을 중계한다. Gateway를 제외한 AI 서비스나 호스트에 MCP 서버 포트를 공개하지 않는다.

Gateway의 내부 upstream 주소와 readiness 주소는 각각 다음과 같다.

```text
http://medical-term-review:8000/mcp
http://medical-term-review:8000/healthz
```

네트워크 경계, 외부 TLS·인증, 도구 권한 계약은 [Gateway 연동 MCP 서버 공통 표준](mcp_gateway_server_standard.md) 및 [MCP Gateway 등록 정보 명세](mcp_gateway_registration.md)를 따른다.

## 5. 컨테이너 시작과 정상 상태 확인

서버에서 컨테이너를 시작한다. `--no-build`는 서버의 소스 파일로 이미지가 의도치 않게 다시 빌드되는 일을 막는다.

```bash
cd /srv/main-mcp-server-medicaldict
sudo docker compose up -d --no-build

sudo docker compose ps
sudo docker compose logs --tail=100 medical-term-review
```

첫 실행 시 Compose가 named volume `medicaldict-history`를 만든다. 이 볼륨을 보존하는 한 컨테이너 재생성이나 이미지 교체 후에도 분석·승인 이력은 유지된다.

Compose healthcheck가 `healthy`가 될 때까지 확인한다. 호스트 포트는 공개하지 않으므로 `curl localhost:8000`으로 검사하지 않는다. 대신 컨테이너 내부에서 `/healthz`를 호출한다.

```bash
CONTAINER_ID=$(sudo docker compose ps -q medical-term-review)
sudo docker inspect --format '{{.State.Health.Status}}' "$CONTAINER_ID"
sudo docker exec "$CONTAINER_ID" python -c \
  "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/healthz').read().decode())"
# 기대값: {"status":"ok","service_id":"medical-term-review"}
```

마지막으로 Gateway가 `mcp-gateway-network`와 AI 서비스용 네트워크 양쪽에 연결돼 있고, `medical-term-review`는 전자에만 연결됐는지 확인한다. Gateway 등록 전에는 `/mcp` handshake도 Gateway가 실제 upstream 주소로 검증해야 한다.

## 업데이트와 롤백

사전 스냅샷은 이미지에 포함되므로 사전 또는 애플리케이션 변경 때마다 새 이미지 태그를 빌드·검증해 전달한다. 현재 Compose 파일은 `medical-term-review-mcp:local`을 참조하므로 다음 배포에서도 새 승인 이미지를 이 태그로 로드한 뒤 재생성한다.

```bash
cd /srv/main-mcp-server-medicaldict
sudo docker compose up -d --no-build --force-recreate
```

운영에서 태그를 명시적으로 관리하려면 `compose.yaml`의 `image: medical-term-review-mcp:local`을 `image: medical-term-review-mcp:<승인_버전>`으로 변경하고, 동일한 Compose 파일과 해당 태그 이미지를 함께 배포한다. 이 방식이 이미지 태그와 배포 버전의 대응을 더 명확하게 만든다.

롤백 전에는 이전 이미지가 서버 Docker 이미지 저장소에 남아 있는지 확인한다. 이전 승인 이미지에 `medical-term-review-mcp:local` 태그를 다시 지정한 후 컨테이너를 재생성한다. `medicaldict-history` 볼륨을 제거하지 않으면 이력은 롤백 대상이 아니다.

```bash
sudo docker tag medical-term-review-mcp:<이전_승인_버전> \
  medical-term-review-mcp:local
cd /srv/main-mcp-server-medicaldict
sudo docker compose up -d --no-build --force-recreate
```

새 컨테이너가 `healthy`이고 Gateway upstream 검사와 도구 호출이 정상인 것을 확인한 뒤에만 Gateway backend에 포함한다.

## 문제 해결

| 증상 | 확인 및 조치 |
|---|---|
| `exec format error` 또는 시작 직후 종료 | 서버 `uname -m`과 `docker image inspect` 플랫폼을 확인한다. Apple Silicon에서 x86_64 서버로 전달할 때는 `--platform linux/amd64`로 다시 빌드한다. |
| `pull access denied` 또는 이미지가 없음 | 서버에 `medical-term-review-mcp:local` 태그가 있는지 `sudo docker image inspect medical-term-review-mcp:local`로 확인한다. `compose.yaml`의 `image` 값과 `docker load`한 태그가 같아야 한다. |
| healthcheck가 `unhealthy` | `sudo docker compose logs medical-term-review`로 활성 사전 파일, `/var/lib/medicaldict` 쓰기 권한, 서버 초기화 오류를 확인한다. 호스트 포트를 추가해 우회하지 않는다. |
| 이력 SQLite를 열 수 없음 | `medicaldict-history` volume의 마운트와 컨테이너 사용자 `mcp`의 `/var/lib/medicaldict` 쓰기 권한을 확인한다. 사전 경로(`/app/data/dictionary-snapshots`)는 의도적으로 읽기 전용이다. |
| `network mcp-gateway-network declared as external, but could not be found` | Gateway가 쓰는 정확한 네트워크 이름을 확인하고, 없을 때만 `sudo docker network create --internal mcp-gateway-network`를 실행한다. |
| AI 서비스에서 MCP 서버에 직접 연결할 수 있음 | AI 서비스가 `mcp-gateway-network`에 연결되지 않았는지 확인한다. Gateway만 AI 서비스 네트워크와 MCP 네트워크 양쪽에 연결해야 한다. |
