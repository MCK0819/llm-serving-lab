# 모델 실행 환경 기록

기록일: 2026-09-11. 상태: 로컬 CPU 임베딩 검증 완료, 원격 GPU 검증 대기.

`확인됨`은 registry나 공식 모델 metadata에서 식별자를 확인했다는 뜻이다. 실제 장비에서 성공했다는 뜻은 아니다.

| 항목 | 후보 또는 설정 | 상태 |
|---|---|---|
| GPU 업체·리전 | 미선정 | PENDING |
| GPU | Runpod Secure Cloud RTX A5000 24GB 비용 우선 후보 | PENDING: checkout 가격·가용성·공인 IP 확인 전 |
| GPU host | CPU/RAM, NVIDIA driver, CUDA 호환성 | PENDING |
| vLLM upstream image | `vllm/vllm-openai:v0.28.0-cu129@sha256:ac259a0111c6cf462a72e449962b84f7a624b5cbec24bd7d9ec3b67d40ffd1bf` | registry index digest 확인된 후보, GPU pull·runtime PENDING |
| SSH 가능한 vLLM image | 로컬 `llm-serving-lab:vllm-ssh-preflight`, image ID `sha256:6b6254cf47511b244cc0250592ee882237801d3bdc905c4ea658e8cdd946f2f9` | 빌드·실제 로컬 SSH 터널 확인. 레지스트리 게시·GPU 기동 PENDING |
| 생성 모델 | `Qwen/Qwen3-4B-Instruct-2507` | metadata 확인됨, runtime PENDING |
| 생성 revision | `cdbee75f17c01a7cc42f958dc650907174af0554` | immutable 후보 확인됨 |
| 생성 형식·한도 | BF16, max model len 4096, max sequences 1 | 계획값, runtime PENDING |
| vLLM 접근 | remote `127.0.0.1:8000` → SSH tunnel → local `127.0.0.1:18000` | PENDING |
| TEI image | `ghcr.io/huggingface/text-embeddings-inference:cpu-1.9@sha256:ad950d30878eceb72aaf32024d26fa2b1d04a75304fa0b4776b49aa1941fea07` | Docker Desktop Linux CPU에서 실행 확인 |
| embedding model | `intfloat/multilingual-e5-small` | 실제 `/info`와 한국어 벡터 확인 |
| embedding revision | `614241f622f53c4eeff9890bdc4f31cfecc418b3` | immutable 후보 확인됨 |
| TEI 실행 한도 | CPU 2, memory 2GiB, float32, mean pooling, client batch 16 | 기동 확인. ONNX backend가 내부 batch requests를 8로 제한함 |
| TEI 접근·cache | local `127.0.0.1:18081`, named volume `tei-cache` | loopback으로 검증, 가중치 캐시는 재실행용 보관 |

## 검증 결과

2026-09-17 SSH 파생 이미지의 전체 로컬 빌드가 완료됐다. 설치된 vLLM은 `0.28.0+cu129`이며 이미지 포트 메타데이터는 `22/tcp`만 포함한다. 외부 네트워크가 차단된 일회용 컨테이너에서 실제 SSH 키 인증·호스트 키 검증·로컬 터널을 통과했다. 이미지에 SSH host key와 authorized_keys가 없음을 확인했고, 공개키를 주지 않은 기본 실행은 종료 코드 1로 거부됐다. 모델을 로드하거나 GPU를 사용한 시험은 아니다. 위 image ID는 로컬 식별자이며 레지스트리에서 pull 가능한 최종 게시 주소·digest는 아직 없다.

| 검증 | 결과 | 기록할 값 |
|---|---|---|
| CPU TEI 기동 | PASS | 2026-09-11 13:31:38 UTC ready. `/info`의 model SHA가 위 revision과 일치 |
| 한국어 query/passage embedding | PASS | 2개 × 384차원, finite, L2 norm 검사 통과, 단일 요청 76.896ms |
| CPU 자원 한도 | PASS, 소규모 기동만 | cgroup memory.peak 1,661,943,808 bytes, 관측 시점 859.4MiB/2GiB, OOMKilled=false |
| 512토큰 초과 입력 | PASS | 긴 고정 시험 입력과 truncate=false에 HTTP 422 Validation |
| 원격 vLLM 첫 응답·stream | PENDING | TTFT, 총 시간, token 수 |
| 실제 GPU 취소 | PENDING | active 관측, disconnect, baseline 복귀 시간 |
| SSH tunnel 재연결 | PENDING | 중단 오류와 재연결 후 smoke 결과 |
| GPU 자원 | PENDING | 최고 VRAM, GPU 사용률, metric 이름·단위 |

실행에 성공하면 mutable tag나 `main` 대신 실제 image RepoDigest와 전체 model/tokenizer SHA를 이 표에 옮긴다. 실패해도 환경을 성공으로 표시하지 않고 오류 코드와 로그에서 민감하지 않은 원인만 기록한다.

CPU 측정은 원격 CPU 서버가 아닌 개발 PC의 Docker Desktop Linux 컨테이너에서 수행했다. 다운로드·워밍업·요청을 포함한 cgroup 최고 메모리는 파일 캐시 등도 포함하며 모델 가중치 크기와 같지 않다. 한 번의 76.896ms는 성능 벤치마크나 P95가 아니다. 원격 CPU 선정 후 같은 검증과 부하 측정이 필요하다. 원시 요약은 [CPU 기동 결과](cpu-embedding-smoke.json)에 보관한다.

위 vLLM 값은 multi-platform index digest다. 확인된 amd64 manifest digest는 `sha256:50509e700235cea487715cedeb501d20a1cd15fa6a54ce93688284bd0d96995d`다. 둘 다 registry metadata 후보이며 GPU host에서 pull한 증거는 아니다. SSH를 포함한 최종 파생 이미지에는 별도의 immutable digest가 필요하다.

근거: [vLLM v0.28.0 release](https://github.com/vllm-project/vllm/releases/tag/v0.28.0), [vLLM 지원 모델](https://docs.vllm.ai/en/latest/models/supported_models/), [Qwen 모델 파일](https://huggingface.co/Qwen/Qwen3-4B-Instruct-2507/tree/cdbee75f17c01a7cc42f958dc650907174af0554), [TEI CLI](https://huggingface.co/docs/text-embeddings-inference/en/cli_arguments), [E5 모델 카드](https://huggingface.co/intfloat/multilingual-e5-small).
