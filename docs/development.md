# 개발 시작하기

현재는 기본 API, 생성 서버 통신, 스트리밍 제한과 API Key 인증까지 구현했다. 질문 경로는 인증을 검사하지만 실제 문서 검색은 아직 연결하지 않아, 유효한 키에는 `503 rag_unavailable`을 반환한다. 문서 업로드는 다음 단계다.

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

## 설정과 범위

환경변수 접두어는 `LLM_LAB_`이다. 예를 들어 `LLM_LAB_INFERENCE_URL`, `LLM_LAB_EMBEDDING_URL`로 내부 서비스 주소를 지정한다. 추론 통신 부품은 구현했지만 앱의 공개 경로에는 아직 연결하지 않았다. URL 안의 비밀번호·query·fragment는 허용하지 않는다. 실제 비밀값을 명령 기록·Git·문서에 넣지 않는다.

DB 설정을 생략해도 liveness는 동작한다. 문서 처리·전체 보안·관측은 아직 구현 중이다. 검증 기록은 [첫 API 검증](verification/bootstrap.md), [인증과 조직 구분](verification/authentication.md)에 있다.

스트리밍의 동작과 제한은 [스트리밍 수명주기 검증](verification/stream-lifecycle.md)을 참고한다.
