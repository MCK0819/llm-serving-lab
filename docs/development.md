# 개발 시작하기

현재는 기본 API, 생성 서버 통신, 스트리밍 제한, API Key 인증, 문서 접수·조회·삭제와 PDF 처리 부품을 구현했다. 질문 경로는 실제 문서 검색을 아직 연결하지 않아 유효한 키에는 `503 rag_unavailable`을 반환한다. 업로드 작업과 PDF 처리 부품을 연결하는 Worker는 다음 단계다.

## Windows에서 실행

표준 Python 3.14.7과 Git을 준비한 뒤 저장소 루트에서 실행한다. 기존 3.10 가상환경은 사용하지 않는다.

```powershell
py -3.14 -m venv .venv314
.venv314/Scripts/python.exe -m pip install --require-hashes -r requirements-dev.lock
.venv314/Scripts/python.exe -m uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8000
```

`http://127.0.0.1:8000/health/live`는 `{"status":"ok"}`를 반환한다. 각 요청의 `X-Request-ID`는 서버가 새로 만든 식별자다. 이 응답은 DB·GPU가 준비되었다는 뜻이 아니라 웹 프로그램이 응답한다는 뜻이다.

## 검사

```powershell
.venv314/Scripts/python.exe -m pytest tests/unit -q
.venv314/Scripts/python.exe -m pytest tests/integration/test_stream_socket.py -q
.venv314/Scripts/python.exe -m ruff check app tests
.venv314/Scripts/python.exe -m ruff format --check app tests
.venv314/Scripts/python.exe -m mypy app
.venv314/Scripts/python.exe -m pip check
```

단위 테스트는 GPU·DB·모델 다운로드가 필요 없다. 별도 TCP 통합 테스트는 로컬 Uvicorn 서버와 모의 추론 서버를 실행해 연결 종료·응답 지연·읽지 않는 클라이언트를 검증한다. 실제 GPU 성능 시험을 대신하지 않는다.

## Docker에서 실행

Docker Desktop의 Linux 엔진을 켠 상태에서 실행한다.

```powershell
docker build -t llm-serving-lab:bootstrap .
docker run --rm --name llm-lab-api -p 127.0.0.1:8000:8000 llm-serving-lab:bootstrap
```

API는 컨테이너에서 UID 10001로 실행된다. 배포 이미지에는 앱 코드·실행용 잠금 파일·DB 마이그레이션 파일을 복사한다. 기본 이미지는 Python 패치와 digest로 고정했다. 개발용 도구는 `requirements-dev.lock`, 실행용은 `requirements.lock`에 분리했다.

## PostgreSQL 통합 테스트

Docker의 Linux 엔진에서 저장소 루트 기준으로 실행한다. 이 Compose는 테스트 전용이며 종료 시 데이터가 사라진다. GPU·기존 DB·일반 애플리케이션 비밀값은 필요 없다.

```powershell
$env:LLM_LAB_TEST_DB_PASSWORD = [Guid]::NewGuid().ToString("N")
try {
    docker compose -f deploy/compose.test.yaml build test
    if ($LASTEXITCODE -ne 0) { throw "Test image build failed" }
    docker compose -f deploy/compose.test.yaml run --rm test python -m pytest tests/unit tests/integration -q
    if ($LASTEXITCODE -ne 0) { throw "Tests failed" }
} finally {
    docker compose -f deploy/compose.test.yaml down
    Remove-Item Env:LLM_LAB_TEST_DB_PASSWORD
}
```

테스트 컨테이너는 실행 전에 `alembic upgrade head`를 수행한다. PostgreSQL 포트는 호스트에 공개하지 않는다. 통합 테스트의 관리 CLI도 `LLM_LAB_TEST_DATABASE_URL`로만 연결하도록 격리했다.

## 운영자용 계정·키 명령

접근 가능한 PostgreSQL의 연결 정보를 `LLM_LAB_DATABASE_URL` 환경변수로 제공한다. 주소 형식은 `postgresql+psycopg://…`이며 비밀값을 Git에 저장하지 않는다. DB 사용 앱은 Linux Docker에서 실행한다. 아래는 해당 환경 안에서 실행하는 명령이며, 각 ID 자리에는 이전 명령의 결과를 넣는다.

```text
python -m alembic upgrade head
python -m app.users.cli create-organization --name "테스트 회사"
python -m app.users.cli create-user --organization <조직 UUID> --name "테스트 사용자"
python -m app.users.cli issue-key --user <사용자 UUID>
python -m app.users.cli revoke-key --key-id <키 앞부분 UUID>
```

발급 명령은 원문 키를 한 번 출력한다. 안전하게 보관하고 공유 로그에 복사하지 않는다. 요청에는 `Authorization: Bearer <API Key>` 헤더를 사용한다. 키 재조회 명령은 없으며 분실하면 새 키를 발급하고 이전 키를 폐기한다. `/docs`에도 Bearer 인증과 질문 JSON 형식이 표시된다.

## 문서 접수 사용하기

DB에는 pgvector 확장이 설치되어 있어야 한다. 테스트 Compose는 PostgreSQL 17과 pgvector 0.8.6 이미지를 digest로 고정한다. `alembic upgrade head`가 문서·처리 작업·청크 테이블과 조직 간 연결 제약을 만든다.

DB와 계정·키를 준비한 앱의 `/docs`에서 Authorize에 발급한 키를 넣는다. `POST /documents`의 file에 텍스트를 선택할 수 있는 PDF 하나를 지정한다. 접수되면 202, 문서 번호와 조회 주소(Location)가 반환된다.

| 요청 | 현재 동작 |
|---|---|
| POST /documents | PDF를 저장하고 처리할 작업을 DB에 기록 |
| GET /documents | 같은 조직의 문서 목록, 기본 20개·최대 100개 |
| GET /documents/{문서 번호} | 상태 확인. 다른 조직이나 삭제한 문서는 404 |
| DELETE /documents/{문서 번호} | 올린 사람만 삭제 가능. 조회에서 즉시 제외 |

현재는 Worker가 없으므로 접수 후 상태가 `queued`에 머문다. 텍스트 추출·암호화·손상 확인 부품은 구현했으며 이후 Worker가 호출한다. 업로드 시점에는 PDF 형식 표시와 시작 바이트만 검사하므로 접수 성공이 분석 성공을 뜻하지 않는다. 삭제는 논리 삭제이고 실제 파일 청소·재시도는 8단계에서 연결한다.

파일은 최대 20 MiB, multipart 요청 전체는 21 MiB다. 조직별 미완료 작업 10개·전체 100개, 저장 공간 최소 여유 1 GiB를 적용한다. 파일 이름은 255자까지의 표시 정보이며 실제 파일 경로에는 사용하지 않는다.

`LLM_LAB_UPLOAD_ROOT`로 PDF 저장 위치를 지정한다. 로컬 기본값은 `data/uploads`, Docker 기본값은 `/var/lib/llm-lab/uploads`다. 컨테이너를 교체해도 원본을 유지하려면 이 위치에 영속 볼륨을 연결해야 한다. DB와 PDF를 함께 보관해야 하며 운영용 Compose와 백업·복원 검증은 후속 단계다.

`LLM_LAB_BROKER_URL`은 선택 설정이다. 지정하면 DB 기록 후 Celery의 `documents.process` 작업에 문서 번호만 전달한다. Redis 연결 실패·1초 알림 대기 초과에도 접수 기록은 유지된다. 동시에 전송하는 알림은 하나로 제한하며, 생략되거나 실패한 알림의 복구 스캔은 8단계에서 구현한다. 현재는 실행할 Worker가 없으므로 Redis 서버를 따로 띄울 필요가 없다.

DB 저장 완료 여부를 확인할 수 없는 장애에서는 파일을 지우지 않고 503을 반환한다. 자동 재업로드는 같은 문서를 중복 접수할 수 있으므로 목록을 먼저 확인한다. 이런 파일과 강제 종료로 남은 파일의 정기 정리도 8단계 범위다.

## 설정과 범위

환경변수 접두어는 `LLM_LAB_`이다. 예를 들어 `LLM_LAB_INFERENCE_URL`, `LLM_LAB_EMBEDDING_URL`로 내부 서비스 주소를 지정한다. 추론 통신 부품은 구현했지만 앱의 공개 경로에는 아직 연결하지 않았다. URL 안의 비밀번호·query·fragment는 허용하지 않는다. 실제 비밀값을 명령 기록·Git·문서에 넣지 않는다.

DB 설정을 생략해도 liveness는 동작한다. 문서 처리·전체 보안·관측은 아직 구현 중이다. 검증 기록은 [첫 API 검증](verification/bootstrap.md), [인증과 조직 구분](verification/authentication.md)에 있다.

스트리밍의 동작과 제한은 [스트리밍 수명주기 검증](verification/stream-lifecycle.md)을 참고한다.

## 모델 기동 점검

5단계 준비로 실제 CPU TEI의 한국어 임베딩을 검증했다. 실행 절차는 [모델 서버 기동](runbooks/deployment.md), 고정한 버전과 측정값은 [환경 기록](experiments/environment.md)에 있다. GPU 대여는 아직 하지 않았다.

```powershell
.venv314/Scripts/python.exe -m scripts.smoke_embedding --endpoint http://127.0.0.1:18081
.venv314/Scripts/python.exe -m scripts.smoke_inference --endpoint http://127.0.0.1:18000
.venv314/Scripts/python.exe -m scripts.smoke_inference --endpoint http://127.0.0.1:18000 --cancel-after-first-text
```

대상 서버나 SSH 터널을 먼저 준비해야 한다. 기본 전체 제한은 120초이며 실패 시 JSON 요약과 0이 아닌 종료 코드를 반환한다. 프롬프트·답변·벡터·토큰 비밀값을 결과에 출력하지 않는다. 생성 취소 명령은 클라이언트 연결 정리만 검사하므로 실제 GPU 작업 종료는 별도 지표로 확인해야 한다. 단위 테스트에는 모델 다운로드나 GPU가 필요 없다.

## PDF 처리 부품과 실제 토크나이저 시험

7단계의 `parse_pdf`, `chunk_pages`, `EmbeddingClient`는 Worker에서 조합할 처리 부품이다. 현재 업로드 API가 이들을 자동 실행하지는 않는다. 동기 PDF 파싱과 토크나이저 처리를 HTTP 요청 안에서 호출하지 않는다.

첫 준비에는 네트워크가 필요하다. 잠금 파일의 의존성을 설치한 환경에서 다음 명령으로 고정된 E5·Qwen 토크나이저 파일만 받는다. 생성 모델 가중치는 받지 않는다. 캐시와 파일 해시는 Git에서 제외된 `.cache/tokenizers`에 남는다.

```powershell
.venv314/Scripts/python.exe -m scripts.cache_tokenizers
```

TEI 모델도 처음에는 [모델 기동 절차](runbooks/deployment.md#cpu-tei-확인)로 한 번 실행해 캐시를 준비한다. 그다음 캐시만 사용해 검증한다.

```powershell
$env:LLM_LAB_TEST_E5_TOKENIZER = (Resolve-Path .cache/tokenizers/e5).Path
$env:LLM_LAB_TEST_QWEN_TOKENIZER = (Resolve-Path .cache/tokenizers/qwen).Path
$env:LLM_LAB_TEST_TEI_URL = "http://127.0.0.1:18081"
$env:LLM_LAB_MODELS_OFFLINE = "1"
docker compose --env-file deploy/models.env.example -f deploy/compose.yaml --profile embedding up -d tei
# TEI가 ready 상태가 된 후 실행한다.
.venv314/Scripts/python.exe -m pytest tests/integration/test_tei.py -q
docker compose --env-file deploy/models.env.example -f deploy/compose.yaml --profile embedding down
Remove-Item Env:LLM_LAB_MODELS_OFFLINE
Remove-Item Env:LLM_LAB_TEST_E5_TOKENIZER, Env:LLM_LAB_TEST_QWEN_TOKENIZER, Env:LLM_LAB_TEST_TEI_URL
```

테스트는 토크나이저 버전·해시, Qwen 메시지 토큰 수, 한글 청크의 크기·내용 보존, 작성한 PDF에서 실제 384차원 벡터까지 확인한다. `LLM_LAB_TEST_*` 설정이 없으면 해당 통합 시험은 건너뛴다. 일반 단위 테스트 통과와 실제 모델 통합 시험 통과를 구분한다. 캐시가 없는 상태에서 offline 모드를 켜면 기동할 수 없다.

처리 규칙과 검증 범위는 [PDF 처리 기록](verification/document-processing.md)에 정리했다.
