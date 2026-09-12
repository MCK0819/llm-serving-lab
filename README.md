# LLM Serving Lab

회사 문서를 찾아 답하는 AI 서비스를 만들며, 모델을 직접 배포하고 속도·장애·보안을 검증하는 프로젝트입니다.

Self-hosted LLM serving backend with FastAPI, vLLM, and Korean document RAG.

## 만드는 이유

Python 백엔드 개발 경험을 AI Backend / LLM Serving으로 확장하기 위한 포트폴리오입니다. 문서 Q&A를 통해 여러 사람이 동시에 질문할 때의 성능, 연결이 끊겼을 때의 처리, 다른 조직의 문서가 섞이지 않도록 하는 권한을 검증합니다.

## 현재 상태

**문서 접수·조회·삭제까지 구현했습니다.** 14개 구현 단계 중 1–4단계와 6단계, 총 5개를 완료했습니다. 기본 API, 스트리밍 제한·연결 정리, API Key 인증, 조직별 문서 접근과 영속 작업 기록을 구현했습니다. 원격 GPU 검증은 보류하고 GPU 없이 가능한 문서 단계를 먼저 진행했습니다. PDF 분석·Worker·RAG·성능 측정은 아직 남아 있습니다. 질문 경로는 인증 후 RAG 미준비 상태를 반환하며, 성공 스트림은 시험용 데이터로 검증했습니다. 아래 구성은 전체 목표입니다.

실행 방법은 [개발 안내](docs/development.md), 확인한 범위와 한계는 [검증 기록](docs/verification/bootstrap.md)을 참고하세요.

연결 끊김·시간 초과·느린 수신자에 대한 결과는 [스트리밍 검증 기록](docs/verification/stream-lifecycle.md)에 정리했습니다.

4단계에서는 Windows 92개, Linux와 실제 PostgreSQL을 포함한 97개 테스트가 통과했습니다. [인증 검증 기록](docs/verification/authentication.md)에서 확인한 범위와 한계를 볼 수 있습니다.

5단계는 진행 중입니다. 로컬 CPU TEI에서 실제 한국어 임베딩(384차원)을 확인했고, 생성·취소·임베딩 점검 명령을 준비했습니다. 원격 GPU 계정·배포·실제 생성 검증은 남아 있습니다. [모델 실행 환경 기록](docs/experiments/environment.md)

배포 준비 변경까지 포함해 Windows 108개, Linux·PostgreSQL 전체 113개 테스트가 통과했습니다. [배포 전 점검 기록](docs/verification/serving-preflight.md)

6단계 변경 후 Windows 129개, Linux·실제 PostgreSQL 전체 172개 테스트가 통과했습니다. PDF 업로드 크기 제한, 다른 조직의 접근 차단, 동시 접수 한도, 취소 시 파일 정리와 큐 연결 실패를 검증했습니다. 접수된 문서는 Worker 구현 전까지 `queued` 상태입니다. [문서 접수 검증 기록](docs/verification/documents.md)

## 목표 구성

```text
질문 → FastAPI → 문서 검색·근거 구성 → 원격 vLLM → 답변 스트리밍
                    ↓
             PostgreSQL + pgvector
                    ↑
PDF 업로드 → 작업 접수 → Celery Worker → 파싱·청크·CPU 임베딩
```

- Python 3.14와 FastAPI 기반 Modular Monolith
- 원격 vLLM에서 Qwen3-4B-Instruct-2507 직접 서빙
- 한국어 텍스트 PDF, CPU TEI 임베딩, PostgreSQL/pgvector 검색
- 비동기 문서 처리, SSE 취소·시간 제한, 조직별 접근 제한
- Prometheus/Grafana 관측과 Locust 부하 시험

초기에는 GPU 한 대와 생성 모델 하나로 시작합니다. 동시성 1/10/50/100은 측정 조건이며 처리 성능 보장이 아닙니다. 성능 결과는 실제 측정 이후 재현 조건과 함께 공개합니다.

## 문서

- [처음 읽는 분을 위한 설명](docs/introduction.md)
- [설계 명세](docs/superpowers/specs/2026-09-09-llm-serving-lab-design.md)
- [API와 운영 규칙](docs/superpowers/specs/2026-09-10-llm-serving-lab-contracts.md)
- [선택한 이유와 대안 — 의사결정 기록](docs/decisions/README.md)
- [14개 단계별 구현 계획](docs/superpowers/plans/2026-09-10-llm-serving-lab.md)
- [전체 문서 안내](docs/README.md)

## 진행 순서

1. 스트리밍과 실제 GPU 연동
2. 문서 처리와 RAG
3. 보안·장애 복구·관측
4. 부하 측정·병목 개선·재측정

로컬·Docker API 실행을 검증했습니다. 원격 GPU 배포 절차는 실제 검증 후 추가합니다. 평가에는 직접 작성한 가상 회사 문서를 사용하며 실제 사내 문서·API Key·모델 가중치는 저장소에 포함하지 않습니다.

