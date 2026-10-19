# ETF 데이터 품질·복구 파이프라인 감사

최초 작성 2026-10-16, 최신 감사 2026-10-19. 브랜치 `rebuild/etf-master-v2`, Draft PR #2.

## 1. 조사 범위와 PR 상태

- 2026-10-19 작업 시작 시 재확인한 로컬/원격 HEAD는 `c3238233596c9bc46e6db439538c7093bc5939c0`으로 일치했고 작업 트리는 깨끗했다. PR #2는 open/draft, mergeable/clean이며 base는 `main`이었다.
- HEAD `c323823`의 GitHub Actions는 push run `38844062915`와 pull_request run `38844067484`가 모두 `success`였다. 이는 합성 fixture와 코드 경로의 성공이며 실제 ETF 가격의 정확성을 증명하지 않는다.
- Draft PR #2 `ETF Daily Master V2: safe historical rebuild`는 `rebuild/etf-master-v2` → `main`이며 조사 시점에 open/draft, mergeable/clean 상태였다.
- 조사 시작 커밋은 `fd84f5a42d769b4ba945b69e50037e5a69e20a77`이다.
- 기존 Actions 실행은 성공했으나 Python 문법 컴파일과 노트북 JSON 파싱만 수행했다. 데이터 규칙의 단위 테스트나 종단 간 실행 검증은 없었다.
- 비교 대상 `work/etf-master-repair`에는 비파괴 후보 생성과 빈 셀 우선 병합 테스트가 있었으나, 출처 검증 상태·셀 단위 provenance·거래일 검증·Alpha 게이트가 부족해 그대로 병합하지 않았다.
- 실제 Google Drive, 운영 마스터, 원자료, Alpha V1 코드는 이 저장소에 없다. 따라서 본 감사는 저장소 코드 및 합성 표본 실행에 한정된다.

## 2. 기존 V2/V3 구조

### V2 입력

- `input/KR_ETF_Universe.xlsx`, `input/US_ETF_Universe.xlsx`
- 유니버스 후보 열: ticker/symbol/티커/종목코드/단축코드/code/yf_ticker, 선택적으로 name/currency.
- 기본 표본: KR `360750`, `069500`, `102110`, `252670`; US `SPY`, `QQQ`, `IVV`.

### V2 가격 출력

키는 `market + ticker + date`이며, 가격 본문은 `open/high/low/close/adj_close/volume`, `turnover_estimated`, 공급자·조회시각·run_id를 포함한다. Parquet이 정본이고 Excel은 연도/시장/part로 분할하는 보조 출력이다. 체크포인트와 액션 Parquet, 수집 상태/실패/품질 CSV, run manifest를 별도 기록한다.

### V3 표적 진단

`diagnose_kr_etf_missing_data_v2.py`는 yfinance download, `Ticker.history(repair=True)`, Yahoo chart API를 비교하며 360750의 2022-12-22~29 의심 구간과 3개 ETF의 2023-07 의심 날짜를 조회한다. 마스터를 수정하지 않고 원응답 CSV·방법 비교·대상 날짜 결과를 남긴다.

## 3. 기존 구현 판정

| 구분 | 확인 내용 | 판정/조치 |
|---|---|---|
| 비파괴 V2 파일명 | V1 대신 별도 V2 Parquet/Excel 사용 | 유지 |
| 합성 대체 금지 | V2/V3는 합성 가격을 만들지 않음 | 유지 |
| 레거시 합성 fallback | 기존 `전달_한_미_etf_정보_모음.py`에는 일부 고급지표 실패 시 simulated data 생성 코드가 남아 있음 | 운영 품질/복구 입력으로 사용 금지; 새 파이프라인과 합성 표본 경로를 분리 |
| 체크포인트 | 배치 Parquet과 status CSV로 재개 | 유지 |
| 날짜 처리 | V3는 서울 날짜 보존으로 고쳤으나 V2 본문은 `utc=True` 후 timezone 제거 | V2도 exchange-local 변환으로 수정, raw/reason/timezone 추가 |
| 성공 상태 | 행이 한 건이라도 오면 `SUCCESS` | `download_status`와 `quality_status` 분리 |
| 결측 | close/open/adj_close/volume 개수만 집계 | 셀·날짜·컬럼·연도·연속 구간 보고 추가 |
| 공통 결측 | 종목별 결과만 존재 | 같은 시장·날짜에서 2개 이상 종목에 발생한 결측의 종목/필드/비율 집계 추가 |
| 스키마/자료형 | 키 누락 시 예외, 숫자 변환 실패는 NaN으로 흡수 | 필수 키·OHLCV/수정종가 열 누락과 숫자 변환 실패를 `schema_issues.csv`에 BLOCKED로 기록 |
| 거래일 | 공급자 응답에 없는 날짜를 직접 비교 | 검증 캘린더가 없으면 확정 결측 금지 |
| 중복 | 최종 저장 전 조용히 drop_duplicates | 제거 전 중복 보고 및 BLOCKED 판정 |
| OHLC | high < low만 검사 | open/close의 high/low 범위, 비양수 가격, 음수 거래량 추가 |
| Open 단독 패턴 | 별도 분류 없음 | Open만 있고 H/L/C가 없거나 Open만 없고 다른 OHLC가 있는 행을 명시적 BLOCKED 이상으로 기록 |
| 수정주가 | 결측만 검사 | adjustment factor 급변 및 기업행사 근거 부재 검사 |
| 수익률 | 없음 | 수정주가 수익률 이상치 검토 항목 추가 |
| JSON 결과 | manifest만 JSON | 품질 결과와 종목 판정을 CSV·JSON 동시 생성 |
| 복구 | PR #2에는 없음 | 검증 출처만 허용하는 별도 후보/감사 엔진 추가 |
| 복구 입력 진위 | 행의 `VERIFIED` 자기표시만으로 실행 가능 | exact source file SHA-256, 행 수, 검증 근거, 원천 증거 목록을 가진 source manifest를 운영 CLI에서 필수 검증 |
| 복구 후보 감사 | 감사 CSV 생성 | 후보의 모든 가격 셀 차이가 정확히 1개 applied audit와 일치하는지 검증하고 원본·후보·감사 해시 불일치를 차단 |
| 재실행 | 명시적 회귀 테스트 없음 | 동일 source로 candidate를 재입력하면 추가 변경·감사 행이 0인 멱등성 테스트 추가 |
| Alpha V1 | 코드와 계약 없음 | PASS만 허용하는 게이트 추가, 실제 계약 부재는 명시적 미완료 |
| 대용량 | Parquet 체크포인트 후 최종 전체 concat, Excel 전체 export | 새 품질 진단은 Parquet/CSV 청크를 임시 SQLite에 적재해 종목별 검사; 인계는 메타데이터/청크 집계; Excel 마스터 로드 거부. V2 전체 재구축의 최종 concat은 잔여 확장 과제 |
| 보안 | 레거시 Python/노트북에 자격증명처럼 보이는 문자열 존재 | 값 마스킹. 실제 값이었다면 이력 노출을 전제로 회전 필요 |

## 4. 명시적 날짜 규칙

1. 키 날짜는 거래소 현지 달력 날짜다: KR `Asia/Seoul`, US `America/New_York`.
2. timezone-aware 값은 해당 거래소 timezone으로 변환한 뒤 timezone을 제거하고 자정으로 정규화한다.
3. naive timestamp는 거래소 현지 시각으로 가정하되 `date_normalization_reason`에 남긴다.
4. `date_raw`, `date`, `date_normalization_reason`, `exchange_timezone`을 함께 보존한다.
5. 날짜 누락은 `date, market, is_trading_day, calendar_verified, calendar_source`가 있는 캘린더에서 `calendar_verified=true`이면서 실제 거래일로 표시된 경우에만 확정한다.
6. 캘린더 미제공/미검증이면 행 누락을 확정하지 않고 종목 판정을 최소 `CONDITIONAL`로 둔다.

현재 날짜 완전성 검사는 종목별 최초~최종 관측일 사이를 대상으로 한다. 상장일/분석 종료일 계약이 별도로 제공되지 않으면 최초 관측 이전과 최종 관측 이후의 leading/trailing 누락은 판정하지 않는다.

360750 회귀 테스트는 서울 자정이 전일 UTC 날짜로 이동하지 않는지와 2022-12-22~29 표본 세션을 확인한다. 저장소에는 독립 검증된 KRX 캘린더 원자료가 없으므로 테스트 캘린더는 `TEST_FIXTURE_NOT_PRODUCTION`으로 표시되며 실제 결측 확인 결과로 사용할 수 없다.

## 5. 품질 판정

- `PASS`: 키/필수 OHLCV/수정종가 완전, 중복 없음, 확정 거래일 누락 없음, blocking/conditional 이상 없음, 거래일 캘린더 검증 완료.
- `CONDITIONAL`: blocking 오류는 없으나 거래일 캘린더 미검증, 수정계수 급변 근거 부족, 수익률 이상치 등 추가 검토가 필요.
- `BLOCKED`: 키·필수 셀 누락, 중복 키, 확정 거래일 누락, 비정상 가격/거래량, OHLC 불일치 중 하나 이상.

다운로드 상태와 품질 상태는 독립이다. 다운로드 성공만으로 PASS 또는 백테스트 허용이 되지 않는다.

### 임계값과 근거

기계 판독 기준은 `config/quality_rules.json`에 기록한다. 기본 절대 최대 가격은 설정하지 않는다(`null`). 통화·단위·시장 근거가 없는 전역 상한을 조용히 적용하지 않기 위해서다. 비양수 가격과 음수 거래량, OHLC 논리 위반은 BLOCKED다.

수정주가 일간수익률 절대값 50%와 수정계수 변화율 절대값 20%는 오류 확정 기준이 아니라 조사 대상을 줄이기 위한 `CONDITIONAL` 검토 기준이다. 실제 기업행사 자료가 급변을 설명하면 수정계수 경고를 해소할 수 있다. 두 값은 실행 결과의 `quality_rule_config`와 run manifest에 함께 기록된다.

## 6. 복구 정책

- 원본을 덮어쓰지 않고 새 candidate만 생성한다. 이미 존재하는 candidate 경로도 자동 덮어쓰지 않는다.
- `verification_status=VERIFIED`이며 `source_name`, `source_uri`, `source_hash`가 모두 있는 원자료만 사용한다.
- 기존 유효 값은 출처와 다르더라도 자동 교체하지 않고 `EXISTING_VALUE_CONFLICT_NOT_OVERWRITTEN`으로 남긴다.
- 새 날짜 행은 검증 원자료와 `calendar_verified=true`, 완전한 OHLCV가 모두 있어야 한다.
- 보간, 전일 복사, mock fallback은 없다.
- 변경 전후 값, 필드, 출처, 조회시각, 해시, 사유, 검증 결과를 `repair_audit.csv`에 남긴다. 불충분하거나 충돌한 값은 `unresolved_cases.csv`에 남긴다.
- 운영 CLI는 `config/verified_source_manifest.example.json` 계약에 맞는 source manifest를 요구한다. exact repair input의 SHA-256과 행 수가 맞고, 모든 VERIFIED 행의 `source_name/source_uri/source_hash`가 `source_evidence`에 포함되어야 실행한다.
- 복구 후 검증은 원본·후보·감사표의 파일 해시를 repair run manifest와 대조하고 실제로 달라진 각 가격 셀과 applied audit의 원래 값/대체 값을 대조한다. 미감사 변경, 중복 감사, 값 불일치는 BLOCKED다.
- 동일 candidate와 동일 source를 다시 실행하면 변경과 새 audit가 0이어야 한다. 자동 테스트로 이 멱등성을 고정한다.

## 7. Alpha V1 호환성

레거시 마스터에서 `date/market/ticker/adj_close/volume`을 추론했지만 실제 Feature Engine/백테스트 계약은 저장소에 없다. `config/alpha_v1_contract.json`은 `INFERRED_NOT_CONFIRMED`이며 운영 계약이 아니다. 게이트는 계약이 `CONFIRMED`이고 종목 `quality_status=PASS`인 경우에만 행을 허용한다. 기본 추론 계약에서는 출력이 전부 차단된다. 실제 Alpha V1 코드·스키마·가격 데이터가 제공될 때까지 통합 상태는 `NOT_RUN_WITH_REAL_ALPHA_V1`이다.

2026-10-19 GitHub 공개 API로 `jayong1408` 계정의 공개 저장소를 재확인했으며 반환된 저장소는 `coding-ETF-test` 하나였다. 이 저장소의 공개 코드에도 Alpha V1 Feature Engine/백테스트 계약이 없다. 따라서 식별자·시간대·종가/수정주가·기업행사·결측·수익률 규칙은 실제 Alpha 저장소나 계약 파일을 받기 전까지 확정하지 않는다.

## 8. 실행 증거와 미완료

- 사용자 전달 정보에는 V2 Yahoo/yfinance 실행이 `synthetic_data_allowed=false`, 7종목·35,196행, `quality_flagged_rows=660`이었다고 기록되어 있다. 그러나 해당 `run_manifest.json`, `diagnostic_manifest.json`, `diagnostic_manifest_v3.json` 원파일은 작업 디렉터리와 저장소에서 발견되지 않아 해시·행 수·내용을 독립 검증하지 못했다. 이 수치는 `USER_REPORTED_NOT_LOCALLY_VERIFIED` 상태로만 유지한다.
- 같은 이유로 Plus가 조사한 069500·102110·252670의 2023년 사례와 360750 의심 구간은 실제 복구 입력으로 사용하지 않았다. 검증 원자료와 manifest가 도착하기 전에는 master/candidate에 값을 채우지 않는다.
- 자동 단위 테스트는 날짜, 360750 회귀, 캘린더 fail-closed, 결측/중복/OHLC, 연속 결측, 검증/미검증 복구, 기존 값 보존, Alpha 게이트, 인계 패키지를 포함한다.
- 구현 커밋 반영 후 GitHub Actions의 push 및 pull_request 실행(헤드 `5b9ed311b358ccd683f6fb8eea7e78462a2c2dcb`)이 모두 성공했다.
- `artifacts/sample_validation/`은 9행 합성 데이터로 만든 실행 증거다. 실제 ETF 데이터나 실제 복구 근거가 아니다.
- 합성 실행에서 복구 전 `BLOCKED 1/PASS 1`, 검증된 합성 셀 1건 적용 후 `PASS 2`가 되었고 인계 파일 존재·크기·행 수·해시·중복 검사가 통과했다. Alpha 계약은 미확정이므로 입장 허용 행은 0건이다.
- 실제 마스터/캘린더/원자료/Alpha 계약이 없으므로 실제 결측 원인 분석, 실제 복구, 운영 Alpha 통합은 미완료다.

## 9. 다음 실행에 필요한 입력

1. 실제 ETF Daily Master Parquet과 기업행사 파일.
2. 출처·추출 시각·해시가 있는 한국/미국 거래일 캘린더.
3. 원자료별 라이선스/출처 URL, 원본 다운로드 파일, 검증 상태를 포함한 복구 후보.
4. 360750 의심 구간의 원본 마스터 행과 독립 원자료.
5. Alpha V1 Feature Engine 및 백테스트의 실제 입력 계약과 대표 입력 fixture.
6. Google Drive/Colab에서 실제 산출물을 쓸 별도 output 경로와 충분한 저장공간.

보안 후속: 마스킹 전 문자열이 실제 자격증명이었다면 Git 이력에도 남아 있으므로 자격증명을 즉시 회전하고, 필요 시 별도 승인된 이력 정리 절차를 수행해야 한다.
