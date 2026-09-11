# llm-serving-lab Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Alternatively, use superpowers:subagent-driven-development when the user chooses delegated execution. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 한국어 PDF에 근거한 답변 서비스를 통해 실제 원격 LLM 서빙·장애 대응·성능 개선을 입증한다.

**Architecture:** FastAPI Modular Monolith와 별도 원격 vLLM을 사용한다. 같은 코드베이스의 Celery Worker가 문서를 처리하고 PostgreSQL이 상태와 벡터를 보관한다. CPU TEI가 임베딩을 담당하며 API에는 생성·임베딩 모델 가중치를 로드하지 않는다.

**Tech Stack:** Python 3.14, FastAPI, Pydantic, httpx, SQLAlchemy, Alembic, PostgreSQL/pgvector, Celery/Redis, pypdf, TEI, vLLM, Docker Compose, pytest, ruff, mypy, Prometheus/Grafana, Locust.

**Spec:** [전체 설계](../specs/2026-09-09-llm-serving-lab-design.md), [API와 운영 규칙](../specs/2026-09-10-llm-serving-lab-contracts.md). 2026-09-10 사용자 승인. 구현자는 두 문서를 함께 읽는다.

## Global Constraints

- 4주 전체 상한 100,000원, 지출 목표 60,000~80,000원. 추가 연장 비용은 별도다.
- 표준 CPython 3.14를 사용한다. 2026-09-10 확인한 패치 기준은 3.14.7이며 로컬·API·Worker·테스트 환경을 맞춘다. free-threaded 빌드는 초기 범위에서 제외한다. 현재 `.venv`는 3.10.11이므로 재사용하지 않는다.
- 프로젝트 이름은 `llm-serving-lab`, 현재 작업 폴더는 `ai-serving-backend`다.
- 한국어 중심 텍스트 PDF, 단일 질문, 조직 공유·업로더 삭제만 지원한다.
- Qwen/Qwen3-4B-Instruct-2507, NVIDIA GPU 하나, 24GB급 검토, BF16, 전체 4096/입력 3584/출력 512토큰, n=1, temperature=0.
- intfloat/multilingual-e5-small, 384차원 정규화, query/passage 역할별 접두어(정확한 문자열은 아래 Task 7), 접두어·특수 토큰 포함 512토큰.
- 청크 본문 최대 300토큰, 같은 페이지 overlap 최대 40토큰 포함, cosine 정확 검색 Top-5.
- API 프로세스 1개, 질문 실행 2개/대기 4개/대기 10초, 연결 5초/무응답 30초/실행 120초/전송 정체 10초.
- PDF 20 MiB/전체 요청 21 MiB/100페이지/추출 문자 100만/5,000청크, 질문 JSON 16 KiB.
- SSE 이벤트 64 KiB/미완성 파싱 버퍼 128 KiB. 자동 질문 재시도·무한 버퍼 없음.
- 사용자별 분당 질문 20회/업로드 3회/나머지 합계 60회. 조직 작업 10개/전체 100개. 파일 여유 1 GiB.
- Worker 동시 실행 1개/임베딩 배치 16청크. lease 120초/heartbeat 20초/recovery 30초/시도 상한 300초/최대 3회/재시도 간격 10·30초.
- 종료 유예 30초+강제 종료 전 정리 여유 10초. 실행 중 권한과 데이터 경합 검증을 생략하지 않는다.
- OCR·Agent·도구 실행·멀티턴·다중 모델·양자화·Canary·Kubernetes·Kafka·별도 Gateway 서비스는 제외한다.
- 예시 코드는 계획상의 시작점이다. 아래 테스트를 아직 실행하거나 구현한 것은 아니다. 체크박스는 실제 검증 후에만 표시한다.

vLLM·TEI는 별도 서버의 이미지별 지원 환경을 따른다. 애플리케이션 Python 3.14를 추론 컨테이너에 강제로 적용하지 않는다. HTTP 계약과 통합 시험으로 호환성을 검증한다. 새 minor 버전은 별도 호환성 검증 후 올리고, 3.14 보안·버그 수정 패치는 테스트와 이미지 재고정 후 반영한다.

## 진행 방식과 시간 배정

쉽게 말해, 먼저 ‘AI와 안전하게 통화하는 기능’을 만들고, 그다음 ‘문서를 찾아주는 기능’을 연결한다. 마지막에는 일부러 고장 내고 여러 사람이 몰리는 상황을 재현한다.

| 단계 | 작업 | 예상 집중 시간 | 단계 종료 증거 |
|---|---|---:|---|
| 1주차: 서빙 경계 | Task 1–5 | 22시간 | 모의 스트림 장애 테스트와 원격 GPU 첫 응답·취소 기록 |
| 2주차: 문서와 RAG | Task 6–9 | 26시간 | 접수→처리→검색→답변, Worker 재시작 복구 |
| 3주차: 운영 검증 | Task 10–12 | 20시간 | 보안·종료·복원 시험, 실제 지표 대시보드 |
| 4주차: 측정과 설명 | Task 13–14 | 20시간 | 고정 조건의 측정·개선 시도·재측정 보고서 |
| 시행착오 여유 | 환경·실패 분석 | 12시간 | 총 100시간 기준 |

이는 추정치다. 80시간만 확보되면 UI·추가 패널·추가 실험을 줄이고, 필수 검증이 끝나지 않으면 미완료로 기록한다. GPU 과금 40~60시간 계획은 개발 시간과 별개다. 독립 검증 가능한 작업마다 검토하고 다음으로 넘어간다. 작업 안의 테스트 사례는 한 번에 하나씩 RED→GREEN으로 반복한다.

## 파일과 책임 지도

계획 작성 당시 앱 코드와 Git 저장소는 없었다. 현재 Git 저장소와 첫 API는 생성되었다. 아래는 **생성 예정 경로**이며 존재하는 파일 링크가 아니다. 공통 기반을 만들기 위한 빈 클래스·빈 디렉터리는 미리 생성하지 않는다.

| 경로 | 책임 |
|---|---|
| `pyproject.toml`, `requirements.lock`, `Dockerfile`, `.dockerignore`, `.gitignore` | Python·의존성·실행 이미지와 로컬 파일 제외 |
| `app/main.py`, `app/bootstrap.py` | 앱 팩토리와 명시적인 의존성 조립·lifespan |
| `app/core/settings.py`, `errors.py`, `database.py`, `health.py` | 설정·오류 계약·DB 생명주기·상태 확인 |
| `app/core/request_context.py`, `body_limit.py`, `rate_limit.py`, `metrics.py`, `logging.py` | 요청 ID·수신 바이트 제한·속도 제한·관측 |
| `app/users/models.py`, `repository.py`, `service.py`, `dependencies.py`, `cli.py` | API Key 검증, 조직 인증, 발급·폐기 |
| `app/inference/types.py`, `sse_parser.py`, `client.py`, `admission.py`, `response.py` | 생성 서버 계약·파싱·통신·대기·ASGI 전송 |
| `app/documents/models.py`, `schemas.py`, `storage.py`, `repository.py`, `service.py`, `router.py` | 문서 저장·상태·접수·조회·삭제 |
| `app/documents/parsing.py`, `chunking.py`, `jobs.py`, `worker.py`, `recovery.py` | PDF 처리와 원자적 작업 권한·복구 |
| `app/rag/embedding.py`, `tokenization.py`, `retrieval.py`, `prompt.py`, `service.py`, `router.py` | CPU 임베딩부터 근거·질문 스트림까지 |
| `migrations/versions/`, `alembic.ini`, `migrations/env.py` | 실제 PostgreSQL 스키마 변경 |
| `deploy/compose.yaml`, `compose.test.yaml`, `nginx.conf`, `models.env.example` | CPU 실행·격리된 통합 테스트·프록시·모델 설정 |
| `deploy/monitoring/`, `benchmarks/`, `scripts/`, `tests/` | 지표 설정·실험·운영 도구·검증 |

Repository는 도메인별 구체 클래스이며 범용 BaseRepository를 만들지 않는다. 테스트용 HTTP transport와 명시적 함수 인자를 우선하고 인터페이스는 필요한 경계에만 둔다. SQLAlchemy async 엔진을 API lifespan에서 소유한다. Worker는 Linux 프로세스에서 작업별 async 실행 경계를 열고 엔진·HTTP client를 같은 이벤트 루프에서 닫는다. prefork 전에 네트워크 자원을 생성하지 않는다.

## 공통 테스트·커밋 규칙

Task 1에서 `integration`, `gpu`, `load` marker를 등록한다. 기본 테스트는 로컬 GPU·모델 다운로드·외부 네트워크가 필요 없어야 한다. async 테스트는 pytest-asyncio의 auto 모드를 사용한다. tokenizer/TEI 실제 호환성은 별도 통합 시험으로 분류한다.

```powershell
.venv314/Scripts/python.exe -m pytest tests/unit -q
.venv314/Scripts/python.exe -m ruff check app tests benchmarks scripts
.venv314/Scripts/python.exe -m mypy app
```

아직 생성되지 않은 경로는 해당 작업이 생성한 뒤 검사한다. Docker 기반 검증은 Linux 컨테이너에서 `python -m pytest`로 동일한 테스트를 실행한다. ASGI 메모리 transport만으로 실제 네트워크 streaming·disconnect를 검증했다고 하지 않는다.

커밋은 각 Task의 GREEN 후 해당 파일만 stage한다. Git 저장소 초기화와 최초 문서 커밋은 완료했다. 구현 파일만 선택적으로 stage한다. 실행 전에 using-git-worktrees 절차로 작업 위치를 확인한다. 이 계획 작성 단계에서는 코드·Git·클라우드 자원을 생성하지 않았다.

## Task 1: 실행 가능한 앱과 안전한 오류 응답

**Files:** Create `pyproject.toml`, `requirements.lock`, `.gitignore`, `.dockerignore`, `Dockerfile`, `app/__init__.py`, `app/main.py`, `app/core/settings.py`, `app/core/errors.py`, `app/core/request_context.py`, `tests/unit/test_app.py`.

**Interfaces:** Produces `create_app(settings: Settings) -> FastAPI`, `AppError(status: int, code: str, message: str)`, `Settings` with validated TEI/vLLM URLs and inference limits. DB·문서·작업 설정은 해당 기능 단계에서 검증과 함께 추가한다. `GET /health/live` returns `{"status":"ok"}`; it needs no external service.

- [x] Python 실행 환경을 확인한다: `py -0p`, `docker version`, `git --version`. `py -3.14 -m venv .venv314`로 별도 환경을 만든다. 3.14가 없다면 공식 배포판 준비를 선행하고 3.10으로 계속 진행하지 않는다. 생성 후 `python --version`으로 3.14.7 이상 3.14 패치인지 확인하고 실제 패치 버전을 기록한다.
- [x] 첫 실패 테스트를 작성한다. pytest와 FastAPI 설치는 이 테스트에 필요한 준비로 수행한다.

```python
from fastapi.testclient import TestClient
from app.main import create_app
from app.core.settings import Settings

def test_liveness_without_model_or_database():
    with TestClient(create_app(Settings())) as client:
        response = client.get("/health/live")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert response.headers["x-request-id"]
```

- [x] `python -m pytest tests/unit/test_app.py -q` 실행: 앱 부재로 실패를 확인한다.
- [x] 최소 앱을 작성하고 UUID request ID middleware를 추가한다. 설정 기본값으로 인증 없는 공개 upstream을 사용하지 않는다. URL 검증과 실제 연결은 분리해 liveness에 DB를 강제하지 않는다.

```python
def create_app(settings: Settings) -> FastAPI:
    app = FastAPI()
    @app.get("/health/live")
    async def live() -> dict[str, str]:
        return {"status": "ok"}
    return app
```

- [x] AppError와 validation error의 안전한 공통 JSON, 예상치 못한 예외의 일반화한 500, request ID를 각각 실패 테스트부터 추가한다. 예외 메시지에 `secret-marker`를 심고 응답에 없음을 검증한다.
- [x] 해당 테스트 GREEN→ruff/mypy. `pyproject.toml`에 Python `>=3.14,<3.15`, pytest 설정을 고정한다. 필요한 패키지만 설치하고 잠금 파일에 전이 의존성을 포함한다. `python -m pip check`와 핵심 의존성 import를 먼저 확인한다. SQLAlchemy 드라이버·Pydantic·tokenizers·Celery·Locust의 전이 의존성은 실제 3.14 Windows/Linux 환경에서 검증한다. 설치 실패 시 원인을 기록하고 조용히 Python을 낮추지 않는다. Docker Python 패치·배포판·digest와 ruff py314/mypy 3.14 설정도 일치시킨다. 이미지의 비root 실행·ignore(`.venv*`, `.env`, credentials, PDF/모델 캐시)를 구성한다.
- [x] 커밋: `feat: bootstrap typed API and safe error responses`.

## Task 2: 생성 서버 스트림 해석과 연결 정리

**Files:** Create `app/inference/types.py`, `sse_parser.py`, `client.py`, `tests/unit/test_sse_parser.py`, `test_inference_client.py`, `tests/fakes/streams.py`.

**Interfaces:** `Delta(text: str)`, `Usage(input_tokens: int, output_tokens: int)`, `Completed(reason: Literal['stop','length'], usage: Usage | None)` are frozen dataclasses. `SSEParser.feed(data: bytes) -> list[str]` emits complete SSE data payloads, `finish() -> None` rejects an incomplete event. `InferenceClient(http: httpx.AsyncClient, model: str).open(messages: list[dict[str,str]])` returns an async context manager yielding an `AsyncIterator[Delta | Completed]`. It enters the upstream connection and validates HTTP status before yielding the iterator.

- [x] Write byte-boundary tests. Bytes may split inside a Korean character or between CR/LF. Add multiple data lines, comments, event and buffer limit tests individually.

```python
from app.inference.sse_parser import SSEParser

def test_korean_bytes_can_arrive_one_at_a_time():
    parser = SSEParser(event_limit=65536, buffer_limit=131072)
    payloads = []
    for byte in 'data: {"text":"안녕"}\n\n'.encode():
        payloads.extend(parser.feed(bytes([byte])))
    parser.finish()
    assert payloads == ['{"text":"안녕"}']
```

- [x] Run `python -m pytest tests/unit/test_sse_parser.py -q` → expected RED, then implement a bounded byte buffer with incremental UTF-8 decoding. Parse blank-line-delimited SSE and join repeated `data:` lines with `\n`; do not use an unlimited `aiter_lines()` buffer for hostile input.
- [x] In `test_inference_client.py`, use `httpx.MockTransport` and an `AsyncByteStream` fake whose `aclose()` records closure. Test valid delta/finish/[DONE]/usage; empty choices usage; invalid JSON/type; 5xx; ConnectError; ReadTimeout; EOF before normal finish; cancellation. Each failed test precedes its implementation.
- [x] Implement the connection lifetime with the supplied HTTP client. Client creation happens in lifespan, not inside the request loop.

```python
async with self.http.stream(
    "POST", "/v1/chat/completions",
    json={"model": self.model, "messages": messages, "stream": True,
          "max_tokens": 512, "n": 1, "temperature": 0,
          "stream_options": {"include_usage": True}},
) as response:
    response.raise_for_status()
    # Feed response.aiter_bytes() into SSEParser; yield typed events.
```

- [x] Map known connection unavailability to 503, malformed/5xx to 502, timeout to 504 using AppError. Never wrap cancellation as a retryable error. Handle role-only chunks; accept only stop/length completion; preserve null usage if absent. Reject tool events. Close upstream on every exit.
- [x] Run both test modules and typing. Commit `feat: add bounded inference stream client`.

## Task 3: 처리 자리와 중단 가능한 SSE 전송

**Files:** Create `app/inference/admission.py`, `response.py`, `tests/unit/test_admission.py`, `test_stream_response.py`, `tests/integration/test_stream_socket.py`, `tests/fakes/upstream_app.py`.

**Interfaces:** `Admission(running_limit: int, waiting_limit: int, wait_seconds: float).slot()` is an async context manager. `encode_event(event: str, payload: dict[str, object]) -> bytes`. `StreamResponse` is an ASGI response owning an `AsyncExitStack` containing the admission slot and open upstream; the stack is closed even when response start/send/disconnect fails. Consumes Task 2 typed events.

- [x] Write and run the first rejection test; use tiny explicit test limits rather than sleeping production durations.

```python
import pytest
from app.core.errors import AppError
from app.inference.admission import Admission

async def test_full_admission_rejects_without_waiting():
    gate = Admission(running_limit=1, waiting_limit=0, wait_seconds=0.01)
    async with gate.slot():
        with pytest.raises(AppError) as error:
            async with gate.slot():
                pytest.fail("overload entered execution")
        assert error.value.status == 503
    async with gate.slot():
        pass
```

- [x] Implement counters and FIFO waiters using synchronous state transitions on one event loop (no await during counter changes); remove cancelled waiters, bound waiting count, release exactly once. Test queued cancellation, wait expiry, release/timeout race separately before implementing them.
- [x] Write ASGI send/receive fakes: one send waits on an Event; one receive emits http.disconnect. Assert upstream closure and slot reacquisition in both cases; successful sources→delta→done and failing error are separate tests. Include failure while opening upstream before headers.
- [x] Implement bounded sending; the deadline covers slot acquisition **after** waiting, through final cleanup. Use a disconnect watcher during preparation too, so a disconnected waiting client does not linger until timeout.

```python
async with asyncio.timeout(send_timeout):
    await send({"type": "http.response.body", "body": event_bytes,
                "more_body": True})
```

- [x] Use a single close owner and `finally` for the exit stack. No producer queue is needed between generator and send. Cancel sibling watcher tasks and await them. No error event after done or after disconnect; no second terminal event when the first terminal send fails.
- [x] Add real Uvicorn TCP tests: disconnect after first delta, upstream stalls, slow reader, bounded event production and free slot afterwards (RSS measurement deferred to load testing). ASGI fakes test deterministic send timeout; TCP tests test real closure without assuming TCP buffers fill after one small event.
- [x] Run unit modules and `python -m pytest tests/integration/test_stream_socket.py -q`. Commit `feat: bound and cancel streaming requests`.

검증 범위와 구현 조정은 [스트리밍 검증 기록](../../verification/stream-lifecycle.md)을 참고한다.

## Task 4: 인증과 질문 경로의 첫 연결

**Files:** Create `app/core/database.py`, `app/users/{models,repository,service,dependencies,cli}.py`, `app/rag/router.py`, `app/bootstrap.py`, `migrations/env.py`, `migrations/versions/0001_users.py`, `alembic.ini`, `deploy/compose.test.yaml`, `tests/integration/test_auth.py`, `tests/unit/test_question_contract.py`.

**Interfaces:** `Identity(user_id: UUID, organization_id: UUID)`; `AuthService.authenticate(raw_key: str) -> Identity` async. Repository `find_active_key(key_id: UUID)` returns hashed credential and identity. `POST /questions` schema has only nonblank `question`; unknown fields are forbidden. This task wires the route to a temporary test-only context provider, not a public ungrounded chat endpoint. Production RAG wiring arrives in Task 9.

- [x] Write the contract test with Pydantic `extra='forbid'` and whitespace normalization.

```python
import pytest
from pydantic import ValidationError
from app.rag.router import QuestionInput

def test_client_cannot_change_model_or_tenant():
    with pytest.raises(ValidationError):
        QuestionInput(question="휴가 규정", organization_id="other", model="other")
```

- [x] Run RED; implement the schema. Then test 401 missing/invalid/revoked key and correct org resolution against PostgreSQL. Seed two organizations and three users via a fixture; cleanup uses an isolated test database only.
- [x] Implement random high-entropy per-user keys, e.g. `key_id.secret` with `secrets.token_urlsafe(32)`; store SHA-256 of the random secret and compare with `hmac.compare_digest`. Key ID is a lookup identifier, not authentication. CLI issues once and supports revocation. No logs contain the raw key.

```python
digest = hashlib.sha256(secret.encode("utf-8")).hexdigest()
valid = hmac.compare_digest(digest, stored_digest)
```

- [x] Implement migration, concrete repository, request-scoped sessions, service injection in bootstrap. No DB access in router. Do not hold a session transaction across a stream. Test-only fixtures supply fictional sources so SSE integration can run before ingestion exists.
- [x] Run `docker compose -f deploy/compose.test.yaml run --rm test python -m pytest tests/integration/test_auth.py -q` after test PostgreSQL health passes. Secrets are supplied via local environment, never committed.
- [x] Commit `feat: authenticate tenant-scoped requests`.

검증: [인증과 조직 구분](../../verification/authentication.md). 기본 질문 경로는 인증 뒤 RAG 미준비 503을 반환하며, 성공 스트림은 테스트 준비 함수로만 검증했다. 질문 16 KiB 수신 제한도 이 단계에서 적용했다.

## Task 5: 첫 원격 GPU·CPU 임베딩 기동 검증

**Files:** Create `deploy/models.env.example`, `scripts/smoke_inference.py`, `scripts/smoke_embedding.py`, `docs/runbooks/deployment.md`, `docs/experiments/environment.md`, `docs/experiments/cost-ledger.md`. Create initial CPU `deploy/compose.yaml`; extend it in later tasks.

**Interfaces:** Smoke scripts accept endpoint and model identity through environment/CLI; credentials only through environment. Output timings/status/token counts to JSON, with no sensitive payloads. Model lock record contains provider, region, GPU/CPU/RAM, driver, image digests, model/tokenizer revisions, vLLM arguments and measured memory.

- [ ] Before any paid run, compare actual checkout estimate including storage/IP/tax with 100,000원 cap, SSH forwarding support, GPU availability, CPU region distance. Record concrete selected resources and teardown steps. Planning approval does not create an account or invent credentials; missing account access is an execution dependency to request when this task is reached.
- [ ] Write smoke assertions before starting the server. Use a fictional fixed prompt and require nonempty text and exactly one completion. The script exits nonzero on protocol failure.

```python
events = []
async with inference.open([{"role": "user", "content": "안녕하세요를 한 문장으로 답하세요."}]) as stream:
    async for event in stream:
        events.append(event)
assert any(isinstance(event, Delta) and event.text for event in events)
assert sum(isinstance(event, Completed) for event in events) == 1
```

- [ ] Pin image/model candidates, launch one Qwen model with BF16 and 4096 context on the chosen remote GPU. Configure loopback vLLM and authenticated encrypted connection. Never expose an unprotected inference endpoint to make a smoke test pass.
- [ ] Verify first response, actual generation cancellation with no other requests running, tunnel drop/reconnect, memory use, GPU metrics access. Capture vLLM active request count returning to baseline and timing; socket closure alone is not proof GPU work stopped.
- [ ] Start CPU TEI with the E5 candidate; verify 384 finite dimensions, normalization and Korean query/passage handling. Record image revision, CPU RAM peak and chosen process memory caps. Keep model downloads out of default unit tests.
- [ ] Freeze the successful environment record; if unsuccessful record the exact failure and adjust deployment within scope before claiming this milestone. Stop paid GPU resources and verify billing state/storage separately. Commit `docs: record verified serving environment` with sanitized configuration only.

## Task 6: 문서 접수·조회·삭제와 영속 상태

**Files:** Create `app/documents/{models,schemas,storage,repository,service,router}.py`, `app/core/body_limit.py`, `migrations/versions/0002_documents_jobs_chunks.py`, `tests/integration/test_documents.py`, `tests/unit/test_storage.py`, `test_body_limit.py`; modify bootstrap and Compose.

**Interfaces:** `DocumentService.accept(identity: Identity, filename: str, body: AsyncIterator[bytes]) -> DocumentReceipt`; async `list(identity, limit, cursor)`, `get(identity, document_id)`, `delete(identity, document_id)`. `FileStorage.save(organization_id, document_id, body) -> Path` async; blocking disk operations are offloaded. Tables: documents, jobs, chunks. Job row one per document; chunks unique `(document_id, ordinal, embedding_revision)`, vector(384), FK org/document relationships enforced.

- [ ] In `test_documents.py` define `api_a`, `api_b`, `pdf_file` fixtures: two org-scoped authenticated clients and a tiny valid text PDF. Write cross-org lookup test before route implementation.

```python
async def test_document_id_does_not_grant_cross_tenant_access(api_a, api_b, pdf_file):
    accepted = await api_a.post("/documents", files={"file": pdf_file})
    assert accepted.status_code == 202
    document_id = accepted.json()["document_id"]
    response = await api_b.get(f"/documents/{document_id}")
    assert response.status_code == 404
```

- [ ] Run RED, then implement the five document operations and safe response schemas. Add same-org shared read, uploader-only delete, repeated delete, cursor ties/invalid cursor, deleted invisibility tests one at a time.
- [ ] Enforce actual received body bytes in ASGI before multipart parsing; enforce file size while storing, one file part, bounded filename metadata, and PDF signature/media type. No full PDF parse in HTTP request. Test false Content-Length and missing Content-Length with oversized chunks.
- [ ] Save to a generated path, atomically finalize the file, then insert document+job in one transaction. Acquire one transaction advisory lock for global admission counts; enforce org10/global100 in that transaction. Never use user filename in a filesystem path.

```sql
SELECT pg_advisory_xact_lock(741001);
-- Under this lock count queued/processing jobs globally and for the organization,
-- then insert document and job within the same transaction.
```

- [ ] Test disk-full/DB commit failure cleans partial files, Redis unavailable still returns202, concurrent admissions do not exceed caps. Broker send is after commit, with a short timeout; failure leaves a recoverable DB job. Deletion locks the document row, records deleted, and excludes new searches immediately.
- [ ] Run document integration + storage/body unit tests. Commit `feat: persist tenant-scoped document jobs`.

## Task 7: 페이지 파싱·청크·CPU 임베딩

**Files:** Create `app/documents/parsing.py`, `chunking.py`, `app/rag/embedding.py`, `tokenization.py`, `tests/unit/test_parsing.py`, `test_chunking.py`, `test_embedding.py`, `tests/integration/test_tei.py`, `tests/fixtures/pdfs/README.md`.

**Interfaces:** `PageText(page: int, text: str)`, `Chunk(ordinal: int, page: int, text: str)`; sync `parse_pdf(path: Path) -> list[PageText]`; `chunk_pages(pages, tokenizer) -> list[Chunk]`. `EmbeddingClient(http, tokenizer, revision).embed_query(text: str) -> list[float]` and `.embed_passages(texts: list[str]) -> list[list[float]]` async. `Tokenizer` adapter exposes `count(text: str) -> int` and offset mapping for boundaries; generator adapter also exposes `count_messages(messages) -> int` with chat template.

- [ ] Write an embedding transport test with a fixture recording outgoing request JSON; return known normalized 384-vector data.

```python
async def test_passage_prefix_is_added_once(embedding_client, recorded_requests):
    await embedding_client.embed_passages(["휴가 규정"])
    assert recorded_requests[0]["inputs"] == ["passage: 휴가 규정"]
```

- [ ] Run RED. Keep raw text in chunks; add exactly one service-owned `passage: ` or `query: ` at the call boundary. User text beginning with those words is content, not an instruction to strip it. Validate prefixed special-token count≤512; no implicit truncate.
- [ ] Add tests for wrong dimension, NaN, zero-norm vector, wrong batch length, timeout and malformed TEI data. Reject invalid vectors; normalize consistently in the adapter before storage/query.

```python
norm = math.sqrt(sum(value * value for value in vector))
if len(vector) != 384 or not all(math.isfinite(v) for v in vector) or norm == 0:
    raise AppError(502, "invalid_embedding", "문서 검색용 응답을 처리하지 못했습니다.")
normalized = [value / norm for value in vector]
```

- [ ] Build synthetic pypdf fixtures for corrupt/password/no-text/too-many-pages/extraction cap and a small authored Korean PDF for integration. Default unit tests use deterministic tokenizer doubles; actual pinned tokenizers have dedicated compatibility tests. Test same-page overlap≤40, total≤300, advancing offsets, long Korean sentence boundaries and special-token budget.
- [ ] Implement pypdf in the Worker process, page1-based text extraction, paragraph/sentence-preferred splitting with tokenizer offsets and no cross-page overlap. Enforce page/character/chunk caps and batches≤16.
- [ ] Run unit modules, then explicit TEI integration against Task5 CPU image with network-free cached model. Commit `feat: parse and embed bounded Korean document chunks`.

## Task 8: Worker 중복 처리와 중단 복구

**Files:** Create `app/documents/jobs.py`, `worker.py`, `recovery.py`, `tests/integration/test_job_recovery.py`; modify Compose, document repository and migrations if constraints need correction.

**Interfaces:** `Lease(document_id: UUID, organization_id: UUID, attempt_id: UUID, attempt_count: int)`; async `JobRepository.claim(document_id) -> Lease | None`, `.renew(lease) -> bool`, `.publish(lease, chunks, vectors, revision) -> bool`, `.fail(lease, code, retryable) -> None`, `.recover_due() -> list[UUID]`. Worker task message contains only document ID; authoritative org comes from DB.

- [ ] Fixture `jobs` owns a real PostgreSQL repository, `queued_document` inserts an accepted job. `expire_lease` is a test-only SQL helper setting DB expiry in the past. Write stale publication test.

```python
async def test_old_attempt_cannot_publish(jobs, queued_document, expire_lease):
    first = await jobs.claim(queued_document)
    await expire_lease(first)
    second = await jobs.claim(queued_document)
    assert second.attempt_id != first.attempt_id
    assert await jobs.publish(first, [], [], "test-revision") is False
```

- [ ] Run RED. Claim with conditional row update and DB time, then increment attempt count exactly once. Renew current lease only; publish locks document then job in a consistent order, rechecks expiry/current attempt/deleted, inserts chunks and marks ready atomically.

```sql
UPDATE jobs SET lease_until = clock_timestamp() + interval '120 seconds'
WHERE document_id = :document_id AND attempt_id = :attempt_id
  AND lease_until > clock_timestamp();
```

- [ ] Test concurrent duplicate claims, duplicate completion, DB commit before Redis failure, Redis reconnect, crash after claim, heartbeat loss, three-attempt exhaustion, deleted during computation. Use DB time mutation/barriers for races, not minute-long sleeps. Final publish and deletion must acquire document/job locks in the same order.
- [ ] Implement Celery Linux worker concurrency1, task ID-only dispatch, heartbeat separate from CPU parsing, absolute process task timeout300s, broker notification/recovery every30s. Celery delivery cannot bypass DB claim; no independent automatic task retry loop.
- [ ] Implement recovery of missed notifications and expired claims, 10/30s backoff, permanent input failure. Ensure crash-expired attempts also count toward maximum3. Add deleted-file/chunk cleanup and one-hour orphan reconciliation; skip active uploads using tracked IDs/creation times.
- [ ] Run real Redis+worker process test: terminate a processing worker, start it again, verify final one ready document with one chunk set. Do not terminate unrelated user processes. Record timings. Commit `feat: recover leased document processing safely`.

## Task 9: 권한을 적용한 검색과 실제 RAG 답변

**Files:** Create `app/rag/retrieval.py`, `prompt.py`, `service.py`, `tests/integration/test_retrieval.py`, `tests/unit/test_prompt.py`, `test_rag_service.py`; modify question router and bootstrap.

**Interfaces:** `SourceChunk(document_id: UUID, ordinal: int, filename: str, page: int, text: str)`; async `Retriever.search(organization_id, vector, revision, limit=5) -> list[SourceChunk]`; `PromptBuilder.build(question, chunks) -> PreparedPrompt` where `PreparedPrompt(messages, sources, input_tokens)`. `RagService.prepare(identity, question, request_id)` owns the prestream slot and opens the response resources from Task3; it returns a StreamResponse after preflight succeeds.

- [ ] Create a PostgreSQL fixture where another organization's vector is the closest and has a unique sentinel. Verify filtering before Top5. Add not-ready/deleted/wrong-revision and deterministic tie tests.

```sql
SELECT c.*, d.filename FROM chunks c JOIN documents d ON d.id = c.document_id
WHERE d.organization_id = :organization_id AND d.status = 'ready'
  AND d.deleted_at IS NULL AND c.embedding_revision = :revision
ORDER BY c.embedding <=> CAST(:query AS vector), d.id, c.ordinal
LIMIT 5;
```

- [ ] Run RED then implement bound-parameter exact search, no HNSW. Release DB transaction before prompt building/upstream call.
- [ ] Write prompt tests using tokenizer fixtures: full chat-template budget3584, drop lowest-ranked chunks, actual sources match retained chunks, no result causes no model call, present-but-no-fit errors422 with context_budget_exceeded. Source IDs are generated server-side and filename/text are serialized as untrusted content.

```python
async def test_no_evidence_skips_generation(rag_empty, inference_spy):
    events = await rag_empty.collect_for_test("휴가 규정")
    assert events[0]["sources"] == []
    assert events[-1]["finish_reason"] == "no_context"
    assert inference_spy.call_count == 0
```

`rag_empty` is a test adapter calling the real service with an empty retriever, collecting ASGI SSE data; `inference_spy` fails on any open call. Define both in this test module, not production code.

- [ ] Implement one flow: authenticate→rate check→slot→E5 query budget→embedding→authorized retrieval→prompt budget→open inference→SSE. Empty result gets fixed `관련 문서에서 답변 근거를 찾지 못했습니다.` and no_context. Normal sources are sent once and match the prompt; only supported terminal reasons accepted.
- [ ] Test deletion before search excludes document; deletion after authorized context acquisition allows existing response while new question excludes it. Test model override, cross-org sentinels and no credential/prompt in errors. Commit `feat: answer with authorized document evidence`.

## Task 10: 속도 제한·종료·민감정보 검증

**Files:** Create `app/core/rate_limit.py`, `health.py`, `logging.py`, `tests/unit/test_rate_limit.py`, `test_health.py`, `test_log_safety.py`, `tests/integration/test_shutdown.py`; modify bootstrap, request middleware and response.

**Interfaces:** `RateLimiter(clock: Callable[[], float]).check(user_id: UUID, category: Literal['question','upload','read']) -> None`; use monotonic time and per-user bounded timestamp deques. `HealthService.ready() -> bool` checks DB, writable storage, draining flag. Shutdown coordinator owns request tasks, no module global registry.

- [ ] Test sliding-window boundary with injected time and explicit Retry-After; max20 query hits followed by429, advance60s permits. Test categories independent, user segregation, eviction. Run RED then implement under one event-loop lock.

```python
limiter = RateLimiter(clock=lambda: now[0])
for _ in range(20):
    limiter.check(user_id, "question")
with pytest.raises(AppError) as error:
    limiter.check(user_id, "question")
assert error.value.status == 429
now[0] += 60
limiter.check(user_id, "question")
```

- [ ] Test ready false on DB/storage failure or draining, true with GPU/Redis stopped if DB/storage work. Service-specific question503 and document202 remain covered. Implement internal-only dependency metrics, no public endpoint exposing credentials/addresses.
- [ ] Log tests put canaries in Authorization, PDF text, question, upstream error. Assert no canary in captured application logs or error responses, including malformed requests. Disable raw body logging; do not rely only on regex redaction after logging.
- [ ] Real process shutdown test sends SIGTERM to a test-owned container: new work503, bounded completion/cancel, release upstream, worker recovery afterwards. Implement lifecycle drain30s then cancel/close and external stop grace40s.
- [ ] Run tests and commit `feat: enforce service limits and bounded shutdown`.

## Task 11: 실제 배포와 백업·복원 연습

**Files:** Modify `deploy/compose.yaml`, `docs/runbooks/deployment.md`; Create `deploy/nginx.conf`, `scripts/backup.ps1`, `scripts/restore.ps1`, `docs/runbooks/restore.md`, `tests/integration/test_deployed_api.py`.

**Interfaces:** CPU Compose exposes only HTTPS proxy (and strictly restricted admin access); API/DB/Redis/TEI are internal. Backup manifest pairs DB dump and PDF archive at one quiescent point and records checksums/model revision. Restore targets a fresh named test volume, never overwrite live data during the drill.

- [ ] Define deployed smoke checks before configuration: unauthorized401, valid upload202, polling ready, sources/delta/done, cross-org404, first delta observed before completion, GPU stopped permits upload, no public DB/TEI/vLLM port.
- [ ] Implement HTTPS and nonbuffered SSE proxy. Validate exact directives against installed Nginx version; cap body21MiB and ensure proxy timeouts exceed app bounded processing. Example starting location:

```nginx
location /questions {
    proxy_pass http://api:8000;
    proxy_buffering off;
    proxy_read_timeout 140s;
}
```

- [ ] Pin CPU/GPU images and model revisions from Task5. Add CPU/Worker/TEI memory caps based on measured peak, restricted credentials, restart policy, and private metrics path. Run `docker compose -f deploy/compose.yaml config --quiet`; do not print expanded secrets.
- [ ] Implement backup script: block intake, stop worker/recovery, wait for uploads and DB writes to finish, dump DB, archive PDFs, checksum, resume processes in finally. Store paired artifacts in a protected off-host directory; never commit them. On failure mark snapshot incomplete rather than using a partial backup.
- [ ] Restore to fresh volumes using the manifest; start internal services, run migrations/version compatibility check, then verify one known authorized document query and cross-org rejection. Record elapsed restore time and lost-data window rather than claiming zero loss.
- [ ] Record provider-specific resource release instructions and estimated pending billing. Commit `ops: verify private deployment and restore procedure`.

## Task 12: 서비스·모델·GPU 지표 연결

**Files:** Create `app/core/metrics.py`, `deploy/monitoring/prometheus.yaml`, `deploy/monitoring/grafana-dashboard.json`, `docs/observability.md`, `tests/unit/test_metrics.py`; modify service measurement sites and Compose.

**Interfaces:** Application histogram names include `llm_lab_request_duration_seconds`, `llm_lab_first_text_seconds`, `llm_lab_admission_wait_seconds`, `llm_lab_retrieval_seconds`, `llm_lab_embedding_seconds`. Counters distinguish successful/error/rejected/cancelled/incomplete outcomes. Labels are bounded route/category/outcome; no request_id/user/doc labels. vLLM metric mapping is recorded by actual running version.

- [ ] Write a test with isolated Prometheus registry: sources alone must not observe first text; first nonempty delta observes once; stream error increments failed outcome, not success. Missing usage remains missing. Use an injected clock with fixed values.

```python
assert observations.first_text == []
recorder.on_delta("", elapsed=0.5)
assert observations.first_text == []
recorder.on_delta("안녕", elapsed=0.8)
recorder.on_delta("하세요", elapsed=0.9)
assert observations.first_text == [0.8]
```

`recorder` is the request metrics recorder implemented here; `observations` is a test collector receiving its metric operations. Define `on_delta(text: str, elapsed: float) -> None` and finish outcome methods in this task.

- [ ] Run RED, add measurement in service/client/stream-finally; request duration includes cancellation/stream end, not headers alone. Document time origin for each metric.
- [ ] Configure private scrapes for application, vLLM, GPU collector chosen in Task5, and DB job state. Verify actual metric names/units; GPU unavailable is an absent series/explicit unavailable status, never a fake0.
- [ ] Build one dashboard with HTTP error/latency, first text, app/model queues, token rates, embedding/retrieval, GPU/VRAM and job failures. Verify each panel with one induced event and record a sanitized screenshot/export.
- [ ] Run tests; commit `feat: observe request and model serving lifecycle`.

## Task 13: 재현 가능한 평가·부하 도구

**Files:** Create `benchmarks/sse.py`, `locustfile.py`, `evaluate.py`, `cases.jsonl`, `tests/unit/test_benchmark_sse.py`, `test_evaluation.py`, `docs/experiments/protocol.md`; Create authored fixtures under `benchmarks/documents/` with provenance README.

**Interfaces:** `StreamStats.feed(event: str, data: dict, elapsed: float) -> None`, `.finish() -> Outcome` with status/first_text/total/usage; `hit_at_5(expected_spans, retrieved_sources) -> bool` uses document/page/span evidence, not chunk IDs. Evaluation script emits one JSON record per fixed case.

- [ ] Write and run parser measurement tests for sources-before-text, empty delta, error event with HTTP200, missing terminal, multiple terminal, length, missing usage.

```python
stats = StreamStats()
stats.feed("sources", {"sources": []}, elapsed=0.1)
stats.feed("delta", {"text": "안녕"}, elapsed=0.4)
stats.feed("error", {"code": "upstream_timeout"}, elapsed=0.7)
outcome = stats.finish()
assert outcome.first_text == 0.4
assert outcome.status == "failed"
```

- [ ] Implement Locust streaming POST with `stream=True`, `catch_response=True`. Manually consume the entire response and mark protocol errors as failure even for HTTP200. Record full stream duration and first-text via separate Locust events; do not confuse its initial header timing with total response.
- [ ] Use the same bounded SSE parsing contract; validate against the controllable TCP fake from Task3 before paying for GPU. Track first text only for eligible streams, report excluded/failed counts, never count data chunks as tokens.
- [ ] Author20 Korean cases: direct10/paraphrase6/unanswerable4; dev and holdout each5/3/2. Fix expected facts/doc/page/spans; keep holdout immutable during tuning. Test scoring with one known hit/miss. Manually judge answer-source correctness/abstention and record labels, not just similarity.
- [ ] Define two load profiles: protected app2/4 and isolated capacity experiment. Avoid per-user20/min limiter dominating GPU trials: explicitly raise rate ceiling only in the isolated authenticated benchmark configuration, log overrides, and keep normal protection-profile limits. This is experimental configuration, never silently the public deployment default.
- [ ] Document warmup then concurrency1/10/50/100, each5min×3 repeats, same dataset and model/output limits. Sample sizes, rejection/error ratios, completed requests/sec, tokens/sec, input/output lengths and GPU/queue data required. Verify generator CPU/network is not saturated.
- [ ] Run unit tests; commit `test: add reproducible RAG and streaming benchmarks`.

## Task 14: 병목 하나를 검증하고 포트폴리오로 정리

**Files:** Create `docs/experiments/baseline.md`, `optimization.md`, `results/README.md`, root `README.md`; Modify the specific measured bottleneck file only, its corresponding test, and relevant decision record.

**Interfaces:** Each experiment record links sanitized raw result files, environment lock, commit ID, configuration, dataset checksum, cost and quality results. Report columns: concurrency, n, success/error/rejection, P50/P95/P99, first text, output tokens/sec, GPU/VRAM/queues.

- [ ] Run preflight unit/integration suites, verify budget ledger including pending storage and fixed costs. Execute baseline protocol from Task13; stop escalating concurrency on resource instability, record the incomplete condition and reason.
- [ ] Select one hypothesis from evidence, e.g. ingestion batch contention increases query embedding latency, or too many admitted generations hurt first text. Write the expected cause and comparison in optimization.md before changing settings. Do not promise a particular improvement percentage.
- [ ] For behavior changes, add the regression test to the responsible Task's module and verify RED before minimal fix. For parameter-only experiments, use the existing behavioral suite and controlled benchmark rather than a test that merely asserts the new constant.
- [ ] Run the same workload after the single change. Compare distribution, errors, throughput, GPU and fixed quality cases. Keep trade-offs and negative results. Restore a rejected experiment's configuration without deleting its evidence.
- [ ] Run `python -m pytest tests/unit -q`, targeted integration suites and deployed smoke after the final change. Capture a short upload→status→answer demo and a failure/overload example with fictional data.
- [ ] Write README in easy Korean: why the project exists, diagram, quickstart, what was actually verified, one before/after result, security boundary, known limits, reproduction cost. Link architecture/spec/ADR/runbooks and raw results. Distinguish production practices exercised from a claim of production certification.
- [ ] Review remaining cloud resources and cost ledger; release unneeded GPU/storage according to verified provider lifecycle. Commit `docs: explain measured serving trade-offs`.

## 자체 검토: 명세와 작업 연결

| 명세 요구 | 담당 작업 |
|---|---|
| 목적·예산·4주·작은 모듈 | 공통 제약, 1, 5, 14 |
| 인증·API 필드·오류·페이지네이션 | 1, 4, 6, 9 |
| SSE·upstream 실패·disconnect·취소·backpressure | 2, 3, 9, 10 |
| 파일 제한·파싱·청크·임베딩 | 6, 7 |
| DB 권위·중복·lease·재시도·삭제·복구 | 6, 8, 9 |
| 조직 필터·토큰 예산·출처·답 없음 | 7, 9, 13 |
| rate·queue·timeout·readiness·종료 | 3, 6, 10 |
| 비밀값·내부 endpoint·prompt injection 경계 | 4, 5, 9, 10, 11, 13 |
| GPU/LLM/HTTP/RAG 지표 | 5, 12, 13 |
| 재배포·백업·복원 | 11 |
| 부하·병목·재측정·품질·README | 13, 14 |

Task 1 앱 기초 구현·검증을 완료했다. 구체화한 범위는 [검증 기록](../../verification/bootstrap.md)에 남겼다. Task 2 스트림 파서·HTTP 클라이언트도 구현했다. [검증 기록](../../verification/inference-client.md)을 참고한다. 다음은 **Task 3의 처리 자리·취소 가능한 SSE 전송**이다. 환경 기동·GPU 호환성과 벤치마크 수치는 계획 작성 시 검증된 것으로 간주하지 않는다.

## 구현 시 확인할 공식 자료

- [FastAPI lifespan](https://fastapi.tiangolo.com/advanced/events/): 앱 자원의 시작과 종료를 context manager로 묶는다.
- [HTTPX async streaming](https://www.python-httpx.org/async/): async stream context와 명시적 response close 수명 규칙.
- [Celery task guide](https://docs.celeryq.dev/en/stable/userguide/tasks.html): 멱등성·프로세스 실행·시간 제한을 실제 고정 버전과 대조한다.
- [Locust request measurement](https://docs.locust.io/en/stable/writing-a-locustfile.html): response 판정과 스트리밍 측정 구현을 고정 버전과 대조한다.

자료 확인일: 2026-09-10. 문서상의 최신 버전 번호를 검증 없이 프로젝트 잠금 버전으로 옮기지 않는다.




