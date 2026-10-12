"""의학 용어 검토 MCP 서버 패키지.

이 패키지는 활성 사전의 읽기 전용 조회와 분석·승인 이력을 분리한다.
"""

from .server import create_server

__all__ = ["create_server"]
