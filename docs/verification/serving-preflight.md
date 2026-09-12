# 모델 배포 전 점검

Task 5의 중간 결과다. 원격 GPU 배포 완료 기록이 아니다.

## 준비한 것

- `scripts.smoke_inference`: 고정된 가상 질문으로 첫 텍스트와 완료를 검사한다. 실패하면 JSON 요약과 0이 아닌 종료 코드를 반환한다.
- `--cancel-after-first-text`: 첫 텍스트 뒤 연결을 닫는다. GPU 작업 취소를 입증하지 않았으므로 `gpu_cancellation_verified`는 false다.
- `scripts.smoke_embedding`: TEI 모델 ID, 한국어 query/passage 접두어, 384차원, 유한한 값과 정규화를 검사한다. 모델 revision은 실제 `/info` 값과 배포 기록을 별도로 대조했다.
- 공통 점검 통신: 전체 시간 제한, JSON 응답 128 KiB 상한, HTTP loopback 또는 HTTPS, redirect·환경 프록시 차단. 결과에 입력·답변·벡터·자격 증명을 출력하지 않는다.
- 고정한 모델·이미지 후보, CPU Compose, 실행 절차와 비용 원장을 추가했다.

## 실제 확인한 것

2026-09-11 로컬 Docker CPU에서 E5 모델을 실행해 한국어 입력 두 개의 384차원 벡터와 정규화를 확인했다. 긴 입력은 truncate=false에서 422로 거부했다. 실제 모델 SHA, 단일 요청 지연과 메모리 관측값은 [환경 기록](../experiments/environment.md)과 [기동 결과](../experiments/cpu-embedding-smoke.json)에 있다.

2026-09-12 Windows 재검증:

```text
pytest tests/unit tests/integration/test_stream_socket.py -q: 108 passed
ruff check app scripts tests migrations: passed
ruff format --check app scripts tests migrations: 48 files already formatted
mypy app scripts: no issues found in 28 source files
```

2026-09-12 Linux 최종 재검증은 Docker Linux 엔진이 응답하지 않아 완료하지 못했다. Desktop 재시작 뒤 프로세스는 확인했으나 `docker version`의 서버 응답이 돌아오지 않았다. 이전 실행에서는 112개가 통과했지만 이후 큰 정수 응답 사례 등을 보완했으므로 최종 변경의 Linux 통과 결과로 대신하지 않는다. 엔진 복구 후 [개발 안내](../development.md)의 전체 통합 테스트를 다시 실행한다.

점검 스크립트 테스트에는 실제 `python -m` 실행의 종료 코드와 비밀값 비노출, 응답 없는 작업의 시간 제한, 빈 답변, 연결 종료, 잘못된 벡터와 과도하게 큰 정수 응답을 포함한다.

기존 연결 정리 테스트의 100ms 실행 제한은 정리 여유를 10ms만 남겨 Windows에서 간헐적으로 실패했다. 이 테스트의 목적은 정리 순서이므로, 시간 경과 대신 오류를 직접 발생시키고 오류 전송 시도 전에 정리가 완료됐는지 검사하도록 바꿨다. 오류 전송을 생략해도 통과하지 않도록 전송 시도 여부도 확인한다. 애플리케이션의 시간 제한은 바꾸지 않았다.

## 남은 것

원격 업체·계정, 실제 결제 화면 견적, SSH 가능한 최종 GPU 이미지, 원격 vLLM 기동·GPU 취소·터널 복구·GPU 지표 검증은 남아 있다. 로컬 CPU 검증을 원격 배포 성공이나 부하 성능으로 표현하지 않는다. 유료 자원 생성과 충전은 수행하지 않았다.
