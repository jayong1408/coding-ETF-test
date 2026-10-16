# ETF 데이터 품질·복구·인계 실행 매뉴얼

## 1. 설치

Python 3.11 이상 환경에서 저장소 루트 기준으로 실행한다.

```bash
python -m pip install -r requirements-etf-data-pipeline.txt
```

운영 마스터는 Parquet을 우선 사용한다. 전체 Excel 마스터는 이 파이프라인이 읽지 않으므로 먼저 원래 환경에서 Parquet으로 1회 변환한다.

## 2. 필수 입력

- 마스터: CSV 또는 Parquet. 키 alias는 `날짜/시장/티커/종목코드`와 `date/market/ticker`를 지원한다.
- 캘린더: `date,market,is_trading_day,calendar_verified,calendar_source`.
- 복구 원자료: 키와 가격 필드 외에 `verification_status,source_name,source_uri,source_retrieved_at,source_hash,calendar_verified`.
- `verification_status=VERIFIED`를 부여하기 전 원 다운로드 파일과 해시, 출처 권한, 종목·날짜·필드 일치를 사람이 확인한다.

## 3. 품질 진단

```bash
python run_quality_pipeline.py \
  --master /path/ETF_Daily_Master.parquet \
  --calendar /path/verified_trading_calendar.csv \
  --actions /path/ETF_Corporate_Actions.parquet \
  --output-dir /path/run/quality_before
```

캘린더를 생략할 수는 있으나 날짜 누락은 확정되지 않고 상태가 `CONDITIONAL`이 된다. 실패 시 `run_failure.json`에 원인과 재실행 명령이 기록된다.

기본 실행은 입력을 100,000행 청크로 읽어 임시 SQLite에 스테이징한 뒤 종목별로 검사하고 종료 시 스테이지를 삭제한다. 작은 fixture에서만 `--in-memory`를 사용한다. 청크 크기는 `--batch-rows`로 조정할 수 있다.

## 4. 검증 원자료로 후보 복구

```bash
python run_repair_pipeline.py \
  --master /path/ETF_Daily_Master.parquet \
  --verified-source /path/verified_source.csv \
  --output-dir /path/run/repair \
  --candidate /path/run/repair/ETF_Daily_Master_repaired_candidate.parquet
```

원본 경로와 candidate 경로가 같으면 실패한다. 기존 candidate도 덮어쓰지 않는다. `repair_audit.csv`, `source_comparison.csv`, `unresolved_cases.csv`를 검토한다.

## 5. 복구 전후 및 Alpha 게이트

```bash
python validate_repair_pipeline.py \
  --before /path/ETF_Daily_Master.parquet \
  --candidate /path/run/repair/ETF_Daily_Master_repaired_candidate.parquet \
  --calendar /path/verified_trading_calendar.csv \
  --actions /path/ETF_Corporate_Actions.parquet \
  --alpha-contract /path/confirmed_alpha_v1_contract.json \
  --output-dir /path/run/validation
```

저장소 기본 계약은 추론본이므로 실제 계약 파일로 교체하기 전 `alpha_admitted_rows.csv`는 비어 있다. 계약이 `CONFIRMED`인 경우에도 PASS 종목만 허용한다.

## 6. 무료 계정용 인계 패키지

```bash
python build_handoff_package.py \
  --master /path/run/repair/ETF_Daily_Master_repaired_candidate.parquet \
  --quality-dir /path/run/validation/after \
  --repair-dir /path/run/repair \
  --output-dir /path/run/handoff \
  --max-file-mb 5
```

출력 폴더는 새 폴더여야 한다. `handoff_summary/`, `handoff_details/`, `handoff_manifest.json`, `HANDOFF_README_KR.md`를 만들며 상세 CSV가 한도를 넘으면 종목·연도·part로 분할한다. 분할 행 합계, 파일 크기, 누락 목록 중복, SHA-256을 자동 검사한다.

무료 계정에는 먼저 `HANDOFF_README_KR.md`, `handoff_summary/decision_brief.md`, `handoff_summary/manifest.json`을 제공하고, 이후 문제가 집중된 종목·연도 상세 CSV만 추가한다.

## 7. 표본 및 테스트

```bash
python -m unittest discover -s tests -v
python -m scripts.generate_sample_artifacts --output-dir /new/path/sample_validation
```

표본 산출물은 모두 `SYNTHETIC`/`sample_data=true`로 표시한다. 실제 복구 데이터로 사용하지 않는다.

## 8. 완료 판정

- 코드 완료: 테스트와 합성 종단 간 실행 통과.
- 실제 품질 진단 완료: 실제 마스터와 검증 캘린더로 보고서 생성 및 검토.
- 실제 복구 완료: 검증 원자료로 candidate 생성, unresolved 검토, 전후 검증 완료.
- Alpha 연계 완료: 확인된 실제 계약으로 실행하고 모든 유입 행이 PASS.
- 인계 준비 완료: 실제 결과를 이용한 패키지의 파일 존재·크기·행 수·분할·해시 검증 통과.

현재 저장소 산출물은 첫 번째 단계만 증명하며 나머지는 필요한 실제 입력이 제공될 때 실행한다.
