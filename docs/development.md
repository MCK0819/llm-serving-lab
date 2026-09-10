# 개발 시작하기

현재는 API가 살아 있는지 확인하는 기능과 공통 오류 처리까지 구현했다. 문서 업로드와 AI 답변은 다음 단계다.

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
.venv314/Scripts/python.exe -m ruff check app tests
.venv314/Scripts/python.exe -m ruff format --check app tests
.venv314/Scripts/python.exe -m mypy app
.venv314/Scripts/python.exe -m pip check
```

단위 테스트는 GPU·DB·모델 다운로드가 필요 없다. HTTPX ASGI transport로 앱 동작을 검증하며 실제 TCP 취소·스트리밍 성능 시험을 대신하지 않는다.

## Docker에서 실행

Docker Desktop의 Linux 엔진을 켠 상태에서 실행한다.

```powershell
docker build -t llm-serving-lab:bootstrap .
docker run --rm --name llm-lab-api -p 127.0.0.1:8000:8000 llm-serving-lab:bootstrap
```

API는 컨테이너에서 UID 10001로 실행된다. 이미지에는 앱 코드와 실행용 잠금 파일만 복사한다. 기본 이미지는 Python 패치와 digest로 고정했다. 개발용 도구는 `requirements-dev.lock`, 실행용은 `requirements.lock`에 분리했다.

## 설정과 범위

환경변수 접두어는 `LLM_LAB_`이다. 예를 들어 `LLM_LAB_INFERENCE_URL`, `LLM_LAB_EMBEDDING_URL`로 내부 서비스 주소를 지정한다. 현재 이 주소로 통신하는 기능은 없으며, URL과 실행 제한의 검증만 제공한다. URL 안의 비밀번호·query·fragment는 허용하지 않는다. 실제 비밀값을 명령 기록·Git·문서에 넣지 않는다.

DB·문서 처리 설정은 해당 기능 단계에서 추가한다. 보안·관측 전체가 구현된 상태는 아니다. 이번 작업의 검증 기록은 [첫 API 검증](verification/bootstrap.md)에 있다.
