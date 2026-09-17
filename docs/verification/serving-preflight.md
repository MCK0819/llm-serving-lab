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

2026-09-12 첫 Linux 재검증은 Docker 엔진 무응답으로 진행하지 못했으나, 이후 엔진 복구를 확인하고 최종 변경 전체를 다시 검증했다. Docker Engine 28.3.0, 고정된 Python 3.14.7 이미지와 해시 고정 의존성을 사용했다.

```text
docker compose -f deploy/compose.test.yaml build test: 성공
docker compose -f deploy/compose.test.yaml run --rm test python -m pytest tests/unit tests/integration -q
113 passed in 14.08s
```

실제 PostgreSQL에 마이그레이션을 적용한 뒤 단위·TCP·인증 통합 테스트를 실행했다. 시험 컨테이너와 네트워크는 종료·제거했다. 재현 명령은 [개발 안내](../development.md)에 있다.

점검 스크립트 테스트에는 실제 `python -m` 실행의 종료 코드와 비밀값 비노출, 응답 없는 작업의 시간 제한, 빈 답변, 연결 종료, 잘못된 벡터와 과도하게 큰 정수 응답을 포함한다.

기존 연결 정리 테스트의 100ms 실행 제한은 정리 여유를 10ms만 남겨 Windows에서 간헐적으로 실패했다. 이 테스트의 목적은 정리 순서이므로, 시간 경과 대신 오류를 직접 발생시키고 오류 전송 시도 전에 정리가 완료됐는지 검사하도록 바꿨다. 오류 전송을 생략해도 통과하지 않도록 전송 시도 여부도 확인한다. 애플리케이션의 시간 제한은 바꾸지 않았다.

## 남은 것

### 2026-09-17 무료 준비 재개

`deploy/Dockerfile.vllm-ssh`, 전용 빌드 파일 허용 목록, SSH 설정과 시작 도구를 추가했다. 실제 OpenSSH를 설치한 일회용 Linux CPU 컨테이너에서 11개 시험이 통과했다. 공개키 누락·오류 거절, 파일 권한, 모델 실행 인자, SSH 인증과 포워딩 제한, 한 자식 프로세스 실패 시 다른 프로세스 종료, SIGTERM 정리를 확인했다. 기존 연결 점검 도구 16개 시험도 로컬에서 통과했다. 기본 이미지 전체를 빌드하거나 GPU를 대여한 결과는 아니다.

종료 도구는 직접 실행한 프로세스를 기다린 뒤 남은 프로세스 그룹을 강제 종료한다. 부모 프로세스가 먼저 종료되면 하위 프로세스의 정상 종료 시간이 짧아질 수 있다. 실제 vLLM 요청 취소와 GPU 해제는 원격에서 별도 검증한다. 최종 이미지에 상속된 포트 메타데이터가 있을 수 있으므로 외부 매핑은 SSH 22만 허용하는지 직접 확인해야 한다.

Runpod 콘솔 로그인과 표시 잔액 $0.00을 확인했다. 실제 가격·가용성 조회 내용과 사용자가 진행할 순서는 [첫 GPU 안내](../runbooks/first-gpu-session.md)에 기록했다. 유료 자원 생성·충전·이미지 게시를 수행하지 않았다.

원격 업체·계정, 실제 결제 화면 견적, SSH 가능한 최종 GPU 이미지, 원격 vLLM 기동·GPU 취소·터널 복구·GPU 지표 검증은 남아 있다. 로컬 CPU 검증을 원격 배포 성공이나 부하 성능으로 표현하지 않는다. 유료 자원 생성과 충전은 수행하지 않았다.

### 전체 이미지 빌드와 실제 SSH 검증

2026-09-17 사용자가 C 드라이브 여유를 약 63.4GiB로 확보한 뒤 전체 이미지를 로컬에서 빌드했다. 다운로드·설치·이미지 저장이 정상 완료됐다. 확인 시점의 C 드라이브 잔여 공간은 약 28.2GiB였으며 Docker 저장 위치는 변경하지 않았다.

완성된 이미지에서 `tests/fixtures/verify_gpu_image.py`를 실행했다. 컨테이너 외부 네트워크는 차단하고, 내부 임시 키로 실제 OpenSSH 인증과 엄격한 host key 확인을 거쳐 loopback HTTP 서버까지 터널 연결했다. 가상 HTTP 응답을 확인하는 시험이며 vLLM 모델은 시작하지 않는다. 빌드에 host key와 authorized_keys가 포함되지 않은 것도 검사한다.

```powershell
docker run --rm --network none --entrypoint python3 --mount "type=bind,src=$((Resolve-Path tests/fixtures/verify_gpu_image.py).Path),dst=/tmp/verify_gpu_image.py,readonly" llm-serving-lab:vllm-ssh-preflight /tmp/verify_gpu_image.py
```

결과는 `ssh_tunnel: passed`, `vllm_version: 0.28.0+cu129`, `model_started: false`, `baked_keys: false`였다. 별도로 공개키 환경변수 없이 기본 진입점을 실행하여 종료 코드 1과 시작 거부 메시지를 확인했다. image metadata의 공개 포트는 `22/tcp` 하나였으며 시험 컨테이너는 제거했다. 로컬 회귀는 22개 통과·Linux 전용 5개 제외, 앞선 Linux CPU 검증 11개 통과와 구분한다. 레지스트리 게시, 실제 GPU 기동, GPU 요청 취소, 성능 검증은 미완료다.
