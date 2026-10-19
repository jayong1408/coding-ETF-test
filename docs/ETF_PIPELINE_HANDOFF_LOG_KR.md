# ETF 파이프라인 작업 인계 로그

기준 시각: 2026-10-19 (Asia/Seoul)

작업 브랜치: `rebuild/etf-master-v2`

Draft PR: #2 (`rebuild/etf-master-v2` → `main`)
산출물 생성 코드: `defe49d8ab6921f4dec9d7fc02378e157da576b5`

## 먼저 읽을 파일

1. `docs/ETF_DATA_PIPELINE_AUDIT.md`
2. `artifacts/sample_validation/handoff/HANDOFF_README_KR.md`
3. `artifacts/sample_validation/handoff/handoff_summary/decision_brief.md`
4. `artifacts/sample_validation/handoff/handoff_manifest.json`

이 저장소의 인계 패키지는 9행 합성 fixture로 생성한 코드 경로 증거다. 실제 ETF 가격 품질, 실제 복구 성공 또는 Alpha V1 통합 완료를 뜻하지 않는다.

## 이번 구현에서 확인한 사실

- 작업 시작 시 원격/로컬 HEAD `c323823`과 PR #2의 open/draft, mergeable/clean 상태를 확인했다.
- `c323823`의 push Actions run `38844062915`와 PR Actions run `38844067484`는 성공 상태였다.
- GitHub 공개 API에서 `jayong1408`의 공개 저장소는 `coding-ETF-test` 하나만 확인됐다. 실제 Alpha V1 저장소/계약은 확인하지 못했다.
- 작업 디렉터리에는 사용자 설명의 `run_manifest.json`, `diagnostic_manifest.json`, `diagnostic_manifest_v3.json`이 없다. 7종목·35,196행·`quality_flagged_rows=660` 수치는 사용자 전달 사실로만 보존하며 독립 검증하지 않았다.
- 실제 가격 원자료나 Plus 복구 후보를 적용하지 않았고 원본 마스터도 수정하지 않았다.

## 구현·검증 완료 범위

- 필수 열/숫자 변환 오류를 `schema_issues.csv`로 명시하고 BLOCKED 판정에 반영.
- 종목 간 공통 결측일을 `common_missing_dates.csv`로 집계.
- Open만 존재하거나 Open만 빠진 불완전 OHLC 패턴을 명시적 BLOCKED 이상으로 분류.
- 기본 절대 가격 상한 제거. 50% 수정주가 수익률과 20% 수정계수 변화는 CONDITIONAL 검토 임계값으로 문서화.
- exact source file SHA-256과 원천 증거를 검증하는 source manifest를 운영 복구 CLI의 필수 입력으로 적용.
- 원본·후보·audit 파일 해시와 모든 변경 셀의 감사 일치를 사후 검증 전에 강제.
- 같은 source 재실행 시 추가 변경/audit가 없는 멱등성 테스트 추가.
- 합성 CLI 실행은 `actual_data_run=false`, `sample_data=true`, `production_ready=false`를 강제.
- CI가 단위 테스트, 합성 E2E, manifest 검증 복구 CLI, audit 검증 CLI를 실행하도록 확장.

## 로컬 실행 증거

- 단위 테스트: 23개 통과, 실패 0 (`ResourceWarning`을 오류로 승격).
- Python 컴파일: 품질·복구·검증·인계 모듈 및 CLI 통과.
- 운영 CLI 합성 smoke: source manifest PASS, 변경 셀 1개/audit 1개 일치, Alpha 입장 0행, production-ready false.
- 무료 계정 인계 패키지: manifest 기록 16파일을 다시 열어 크기·SHA-256·CSV 행 수를 대조했고 실패 0.
- 패키지 검증: 필수 요약 파일, 상세 파일 크기, 분할 전후 행 수, 결측 목록 중복 검사 모두 통과.

## 실제 생성 산출물

- 합성 실행 요약: `artifacts/sample_validation/SAMPLE_RUN_RESULT.json`
- source manifest fixture: `artifacts/sample_validation/inputs/SYNTHETIC_source_manifest.json`
- audit 일치 결과: `artifacts/sample_validation/repair/repair_audit_validation.json`
- 무료 계정 요약: `artifacts/sample_validation/handoff/handoff_summary/`
- 선택 전달 상세: `artifacts/sample_validation/handoff/handoff_details/`
- 전체 패키지 manifest: `artifacts/sample_validation/handoff/handoff_manifest.json`

## 아직 완료하지 않은 범위

- 실제 V2/V3 manifest와 7종목 원본 Parquet의 해시·행 수 검증.
- 360750 및 069500·102110·252670 의심 구간의 독립 원자료 확인.
- 검증 거래일 캘린더와 기업행사 파일을 이용한 실제 품질 판정.
- 검증 원자료를 사용한 실제 복구 candidate 생성·재검증.
- 실제 Alpha V1 Feature Engine/백테스트 계약 확인 및 통합 테스트.
- PR 병합. 별도 승인이 없으므로 Draft 상태를 유지한다.

## 다음 실행에 필요한 입력

1. 실제 master Parquet와 파일 SHA-256.
2. 사용자 설명에 언급된 V2/V3 manifest 원파일.
3. 검증된 거래일 캘린더와 출처/해시.
4. 복구 후보 CSV/Parquet, exact file hash가 있는 source manifest, 원 다운로드 파일과 원천별 hash.
5. Plus가 이미 candidate를 만들었다면 원본 hash, candidate hash, 셀 단위 repair audit를 함께 제공.
6. Alpha V1 저장소 접근 권한 또는 확정 계약 JSON과 대표 fixture.

실제 자료가 제공되면 `docs/ETF_DATA_PIPELINE_RUNBOOK_KR.md` 순서로 진단 → manifest 검증 복구 → audit 검증 → 사후 품질 → Alpha 게이트 → 실제 인계 패키지를 새 output 경로에 생성한다.
