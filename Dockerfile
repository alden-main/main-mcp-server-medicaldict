# 활성 사전은 읽기 전용 이미지 계층에 포함하고, 승인 이력은 볼륨에만 기록한다.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MEDICALDICT_SNAPSHOT_ROOT=/app/data/dictionary-snapshots \
    MEDICALDICT_STORE_PATH=/var/lib/medicaldict/analysis-history.sqlite

WORKDIR /app

RUN groupadd --system mcp && useradd --system --gid mcp --home-dir /nonexistent --shell /usr/sbin/nologin mcp

COPY pyproject.toml README.md ./
COPY src ./src
RUN python -m pip install --no-cache-dir .

COPY data/dictionary-snapshots ./data/dictionary-snapshots
RUN mkdir -p /var/lib/medicaldict \
    && chown -R mcp:mcp /app /var/lib/medicaldict \
    && chmod -R a-w /app/data/dictionary-snapshots

USER mcp
VOLUME ["/var/lib/medicaldict"]
EXPOSE 8000

# 기본 전송은 stdio다. 네트워크 배포에서는 MEDICALDICT_MCP_TRANSPORT를 설정한다.
ENTRYPOINT ["main-mcp-server-medicaldict"]
