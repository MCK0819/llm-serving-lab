# 첫 모델 서버 기동 절차

상태: 로컬 CPU TEI 기동 검증 완료, 원격 GPU 준비 중. 원격 업체와 계정은 선택되지 않았다. 결과는 [환경 기록](../experiments/environment.md)에 있다.

## 먼저 확인할 것

유료 자원을 만들기 전에 아래 항목을 한 화면에서 확인하고 `docs/experiments/cost-ledger.md`에 적는다.

- 사용자가 선택한 업체·계정과 결제 수단이 준비되어 있다.
- 선택 화면에 실제 리전, Secure/Community 등급, GPU, 시간당 가격, 저장소·공인 IP·세금·수수료가 표시된다.
- Runpod 후보라면 RTX A5000 24GB의 현재 가용성과 실제 결제 단가를 다시 확인한다. 공개 페이지의 `$0.27/시간`은 가용성이나 최종 청구액 보장이 아니다.
- Full SSH를 쓸 수 있고 공인 IP와 `22/tcp` 매핑이 제공된다. Runpod의 Basic SSH 프록시는 포트 포워딩을 보장한다고 문서화되어 있지 않다.
- 사용할 사용자 정의 이미지가 `sshd`와 vLLM을 함께 시작하는 것이 별도로 검증되어 있다. 현재 이 이미지는 준비되지 않았으므로 이 조건을 만족하기 전에는 원격 Pod를 만들지 않는다.
- vLLM의 8000번 포트는 외부에 노출하지 않는다. HTTP 프록시와 직접 TCP 포트에도 추가하지 않는다.
- 자동 충전을 켜지 않는다. 1~2시간 종료 시각을 미리 정한다. 업체 화면에서 자동 종료가 실제 설정됐음을 확인할 수 있을 때만 보조 장치로 쓰고, 그렇지 않으면 수동 종료 시각을 지킨다.

## CPU TEI 확인

현재 Compose의 `embedding` 프로필은 TEI만 실행하며 호스트의 `127.0.0.1:18081`에 연결된다. CPU 2개와 메모리 2GiB 한도를 사용하고 모델 캐시는 `tei-cache` 볼륨에 남는다.

```powershell
docker compose --env-file deploy/models.env.example -f deploy/compose.yaml --profile embedding up -d tei
python -m scripts.smoke_embedding --endpoint http://127.0.0.1:18081 --model intfloat/multilingual-e5-small --deadline 120
```

성공 조건은 `/info`의 모델 ID 일치, 한국어 `query: `와 `passage: ` 입력, 유한한 384차원 벡터 두 개, 각 벡터의 L2 norm이 1에 가까운 것이다. 요청은 `normalize: true`, `truncate: false`를 명시한다. 종료 전 실제 최고 메모리와 실행 결과를 환경 기록에 옮긴다.

```powershell
docker compose --env-file deploy/models.env.example -f deploy/compose.yaml --profile embedding down
```

`down`은 이름 있는 캐시 볼륨을 자동 삭제하지 않는다. 재실행용 가중치 캐시를 보존할지, 더는 필요 없어 별도로 정리할지는 크기와 목적을 확인한 뒤 결정한다.

## 원격 vLLM 연결

Full SSH가 준비된 원격 컨테이너 안에서 vLLM은 `127.0.0.1:8000`에만 바인딩한다. 모델은 BF16, 최대 문맥 4096, 초기 동시 시퀀스 1개로 시작한다. 이미지 digest, 모델과 tokenizer revision, 드라이버와 실제 실행 인자는 환경 기록에 남긴다.

SSH 화면이 보여 주는 공인 IP와 매핑 포트로 로컬 터널을 연다. 사전에 관리 콘솔 등 별도 신뢰 경로에서 서버 host key 지문을 확인하고 known_hosts에 등록한다. 지문 확인 없이 처음 보이는 키를 수락하거나 host key 검사를 끄지 않는다.

```powershell
ssh -N -T -i <PRIVATE_KEY_PATH> -o StrictHostKeyChecking=yes -o ExitOnForwardFailure=yes -o ServerAliveInterval=15 -o ServerAliveCountMax=3 -L 127.0.0.1:18000:127.0.0.1:8000 -p <SSH_PORT> root@<POD_IP>
```

다른 터미널에서 smoke를 실행한다. endpoint는 `/v1`을 붙이지 않은 base URL이다. client가 경로를 덧붙인다. 토큰이 필요하면 값은 환경변수로만 전달하고 출력이나 문서에 남기지 않는다.

```powershell
# 필요한 경우 LLM_LAB_SMOKE_TOKEN을 안전한 환경 설정으로 미리 제공한다.
python -m scripts.smoke_inference --endpoint http://127.0.0.1:18000 --model Qwen/Qwen3-4B-Instruct-2507 --deadline 120
python -m scripts.smoke_inference --endpoint http://127.0.0.1:18000 --model Qwen/Qwen3-4B-Instruct-2507 --deadline 120 --cancel-after-first-text
Remove-Item Env:LLM_LAB_SMOKE_TOKEN -ErrorAction SilentlyContinue
```

첫 명령은 비어 있지 않은 첫 텍스트와 완료 이벤트 하나를 요구한다. 두 번째 명령은 client 연결이 닫혔다는 사실만 확인하며 출력의 `gpu_cancellation_verified`는 `false`다.

GPU 작업 취소를 입증하려면 다른 요청이 없는 상태에서 충분히 긴 생성을 시작하고, vLLM `/metrics`의 해당 버전 실제 request metric과 `nvidia-smi`에서 active 상태를 먼저 관측해야 한다. 그 뒤 연결을 끊고 active request 수와 GPU 작업이 기준선으로 돌아오는 시각을 기록한다. 요청이 관측 전에 끝났거나 active 상태를 보지 못했다면 결과는 `INCONCLUSIVE`다. 가중치 캐시 때문에 GPU 메모리가 계속 점유되는 현상은 취소 실패로 판정하지 않는다.

터널 프로세스를 끊었다가 같은 명령으로 다시 연결하고 smoke가 다시 성공하는지도 확인한다. `/metrics`도 터널로만 읽는다.

## 종료와 비용 확인

정한 시각이 되면 실험 성공 여부와 관계없이 원격 자원을 종료한다. 단순히 vLLM 프로세스나 SSH를 끈 상태를 과금 종료로 보지 않는다. 업체 화면에서 Pod가 종료·삭제됐는지, 남은 volume/network storage와 계속되는 저장소 요금이 없는지 확인한다. 실제 사용 시간과 청구 상태를 비용 원장에 기록한다. 사용자 계정의 전체 사용액은 billing 화면을 직접 확인하지 않았다면 추정하지 않는다.

## 근거

- [Runpod 가격](https://www.runpod.io/pricing)
- [Runpod Pod 과금과 저장소](https://docs.runpod.io/pods/pricing)
- [Runpod 결제와 prepaid credit](https://docs.runpod.io/accounts-billing/billing)
- [Runpod SSH 방식](https://docs.runpod.io/pods/configuration/use-ssh)
- [Runpod 포트 노출](https://docs.runpod.io/pods/configuration/expose-ports)
- [vLLM Docker 이미지](https://docs.vllm.ai/en/latest/deployment/docker/)
- [Qwen3-4B-Instruct-2507 모델 카드](https://huggingface.co/Qwen/Qwen3-4B-Instruct-2507)
- [TEI CPU 이미지와 지원 모델](https://huggingface.co/docs/text-embeddings-inference/en/supported_models)
