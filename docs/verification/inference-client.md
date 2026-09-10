# 스트림 파서와 추론 HTTP 클라이언트 검증

범위: 구현 계획 Task 2. 실제 모델을 호출하지 않고 HTTPX MockTransport와 제어 가능한 응답 스트림으로 검증했다.

- 한글 바이트 분할, LF/CRLF/CR 줄바꿈, 여러 data 줄, 주석과 UTF-8 BOM 처리
- 이벤트 64 KiB·미완성 입력 128 KiB 제한, 잘못된 UTF-8·미완성 이벤트 거절
- vLLM 응답을 Delta와 Completed로 변환, stop/length 구분, 사용량 미제공 시 null 유지
- HTTP 상태·Content-Type을 응답 반복자 반환 전에 검사
- 정상 종료는 finish_reason과 `[DONE]`을 모두 요구하며 EOF만으로 성공 처리하지 않음
- 연결 실패 503, 시간 초과 504, 비정상 응답 502. 자동 재시도 없음
- 취소·소비자 조기 종료·중간 오류에서 응답 연결 종료, 공유 HTTP client는 앱 종료 시 정리
- 요청마다 HTTP client를 만들지 않고 앱 lifespan에서 하나를 소유

최종 Windows Python 3.14.7 검사: 전체 단위 테스트 **52 passed**, ruff 검사·포맷, mypy(12개 소스 파일), pip check 통과.

추가 독립 코드 검토는 보조 에이전트 사용량 제한으로 실행되지 않았다. 이번 변경은 자체 검토와 위 자동 검사를 거쳤다. 실제 TCP 취소·느린 소비자·전체 요청 deadline은 Task 3, 실제 vLLM 호환성과 GPU 생성 중단은 Task 5에서 검증한다. 현재 질문 API가 외부에 연결된 상태는 아니다.
