# 첫 API 구현 검증

날짜: 2026-09-10. 범위: 구현 계획 Task 1의 앱 기초.

## 확인한 동작

- Python 3.14.7 별도 설치, 전용 가상환경 사용. 기존 Python 3.10 유지.
- 앱이 없는 상태의 liveness 테스트 실패 → 최소 구현 후 통과.
- 404·403·500·입력 검증·AppError 테스트 실패 → 안전한 JSON 응답 구현 후 통과.
- 설정의 잘못된 URL·0/음수 한도·입력 여유 없는 출력 예약 거절.
- 공통 오류 처리로 사라졌던 Allow 헤더를 실패 테스트로 확인한 뒤 보존.
- Docker Linux 이미지 빌드, 실제 HTTP 200과 request ID, Python 3.14.7, UID 10001 확인.
- 이미지의 테스트 컨테이너는 검증 후 종료했다.

## 계획의 구체화

현재 사용하는 설정만 구현했다. DB URL과 문서·작업 제한은 해당 기능을 추가할 때 함께 테스트한다. 검증하지 않은 모든 한도를 설정 필드만 만들어 구현했다고 표시하지 않는다.

FastAPI TestClient의 최신 의존성 조합에서 경고가 발생해 HTTPX ASGITransport를 사용하는 비동기 테스트로 변경했다. 앱 factory가 아직 네트워크 자원을 소유하지 않으므로 빈 lifespan은 추가하지 않았다. HTTP client를 소유하는 다음 단계에서 lifespan을 구현한다.

첫 API의 실행과 검증은 LLM 모델·GPU 성능 검증이 아니다. 실제 LLM 연동은 다음 작업에 남아 있다.

## 검토와 남은 제한

독립 코드 검토에서 무한대 timeout 허용을 발견해 실패 테스트 후 수정했다. 예상치 못한 예외는 HTTP 응답에서 숨기지만 Uvicorn 서버 로그에는 traceback이 남을 수 있다. 실제 민감한 문서를 다루기 전에 Task 10에서 서버 로그까지 검증해야 한다. 이번 단계는 전체 production 보안 완료를 의미하지 않는다.


## 최종 결과

Windows Python 3.14.7: 16 passed. 새 Linux Python 3.14.7 컨테이너: 16 passed. ruff check/format, mypy(6개 소스 파일), pip check 통과. 실행용·개발용 해시 잠금 파일 설치와 최종 Docker 빌드 성공. 기록된 테스트는 모두 GPU 없이 수행했다.

