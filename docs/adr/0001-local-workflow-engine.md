# ADR 0001 — 로컬 workflow 유지

- 상태: Accepted (2026-10-05).
- 결정: modular application coordinator + SQLite WAL/lease를 사용한다. 외부 workflow engine은 도입하지 않는다.
- 근거: 로컬 10k event indexed projection 0.0168s, fixture crash/fencing/rollback 통과. 현재 여러 호스트 간 scheduler/queue 필요를 증명하는 측정값 없음.
- 비용: host 간 공유 실행과 distributed exactly-once는 제공하지 않음. 외부 tool side effect는 operation idempotency/PR 조회/outbox delivery key로 복구한다.
- 재검토: 실제 운영에서 multi-host worker, durable timer, DB contention 또는 1초 projection 목표 초과가 반복되면 별도 ADR과 비용·운영 비교를 작성한다.
- 범위: provider latency/실제 capacity/production load는 측정하지 않았다. 이번 local 결과로 외부 서비스 성능을 주장하지 않는다.
