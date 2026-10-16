"""Build a small, independently readable free-account handoff package."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
from typing import Any

import pandas as pd


SUMMARY_FILES = [
    "README_KR.md", "manifest.json", "schema.csv", "quality_summary_by_ticker.csv",
    "missing_summary_by_year.csv", "repair_summary.csv", "decision_brief.md", "validation_results.json",
]
DETAIL_SPECS = {
    "missing_dates.csv": ["market", "ticker", "date", "field", "category", "status"],
    "anomaly_dates.csv": ["market", "ticker", "date", "field", "issue", "severity", "value", "detail"],
    "repair_audit.csv": ["market", "ticker", "date", "field", "original_value", "replacement_value", "source_name", "source_uri", "repair_reason", "validation_result", "applied"],
    "source_comparison.csv": ["market", "ticker", "date", "field", "original_value", "source_value", "source_name", "source_uri", "verification_status", "comparison_status"],
    "unresolved_cases.csv": ["market", "ticker", "date", "field", "reason", "source_count"],
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git_version() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], text=True, stderr=subprocess.DEVNULL).strip()
    except Exception:
        return "UNKNOWN"


def _read_csv_or_empty(path: Path | None, columns: list[str]) -> pd.DataFrame:
    if path and path.exists() and path.stat().st_size:
        try:
            frame = pd.read_csv(path, dtype={"ticker": "string"})
            return frame
        except pd.errors.EmptyDataError:
            pass
    return pd.DataFrame(columns=columns)


def _master_schema(path: Path) -> tuple[pd.DataFrame, dict[str, Any]]:
    suffix = path.suffix.casefold()
    if suffix in {".parquet", ".pq"}:
        try:
            import pyarrow.parquet as pq
            parquet = pq.ParquetFile(path)
            fields = [(field.name, str(field.type)) for field in parquet.schema_arrow]
            rows = int(parquet.metadata.num_rows)
            schema = pd.DataFrame(fields, columns=["column_name", "data_type"])
        except ImportError as exc:
            raise RuntimeError("pyarrow is required to inspect a Parquet master without loading it into memory") from exc
    elif suffix == ".csv":
        sample = pd.read_csv(path, nrows=1000, dtype={"ticker": "string", "티커/종목코드": "string"})
        schema = pd.DataFrame({"column_name": sample.columns, "data_type": [str(value) for value in sample.dtypes]})
        rows = sum(len(chunk) for chunk in pd.read_csv(path, chunksize=100_000, usecols=[sample.columns[0]])) if len(sample.columns) else 0
    else:
        raise ValueError("Handoff master must be Parquet or CSV; Excel is not loaded into memory.")
    meaning = {
        "date": "exchange-local trading date", "market": "KR or US market", "ticker": "instrument code",
        "open": "raw opening price", "high": "raw high price", "low": "raw low price", "close": "raw closing price",
        "adj_close": "provider adjusted close", "volume": "reported volume", "trading_value": "reported or explicitly labelled estimated traded value",
    }
    schema["meaning"] = schema["column_name"].map(meaning).fillna("source or audit metadata")
    schema["missing_allowed"] = schema["column_name"].map(lambda value: "NO" if value in {"date", "market", "ticker", "open", "high", "low", "close", "adj_close", "volume"} else "POLICY_DEPENDENT")
    metadata = {"path": str(path.resolve()), "size_bytes": path.stat().st_size, "rows": rows, "sha256": sha256_file(path)}
    return schema, metadata


def _write_detail_parts(frame: pd.DataFrame, name: str, output: Path, max_bytes: int) -> tuple[list[Path], dict[str, Any]]:
    base = output / name
    estimate = len(frame.to_csv(index=False, lineterminator="\n").encode("utf-8"))
    if estimate <= max_bytes or frame.empty:
        frame.to_csv(base, index=False, encoding="utf-8")
        return [base], {"source_rows": int(len(frame)), "part_rows": int(len(frame)), "row_count_match": True}
    working = frame.copy()
    date_series = pd.to_datetime(working.get("date"), errors="coerce") if "date" in working else pd.Series(pd.NaT, index=working.index)
    working["_year"] = date_series.dt.year.fillna(0).astype(int)
    paths: list[Path] = []
    stem = Path(name).stem
    for (ticker, year), group in working.groupby([working.get("ticker", pd.Series("ALL", index=working.index)).fillna("UNKNOWN"), "_year"], dropna=False):
        clean = group.drop(columns=["_year"])
        safe_ticker = "".join(character for character in str(ticker) if character.isalnum() or character in {"-", "_"}) or "UNKNOWN"
        payload = clean.to_csv(index=False, lineterminator="\n").encode("utf-8")
        rows_per_part = max(1, int(len(clean) * max_bytes / max(len(payload), 1) * 0.90))
        for number, start in enumerate(range(0, len(clean), rows_per_part), start=1):
            suffix = f"_{number:03d}" if len(clean) > rows_per_part else ""
            path = output / f"{stem}_{safe_ticker}_{year}{suffix}.csv"
            clean.iloc[start:start + rows_per_part].to_csv(path, index=False, encoding="utf-8")
            paths.append(path)
    part_rows = sum(len(pd.read_csv(path)) for path in paths)
    return paths, {"source_rows": int(len(frame)), "part_rows": int(part_rows), "row_count_match": int(len(frame)) == int(part_rows)}


def _file_record(path: Path, root: Path) -> dict[str, Any]:
    record: dict[str, Any] = {
        "path": path.relative_to(root).as_posix(), "size_bytes": path.stat().st_size,
        "sha256": sha256_file(path), "created_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    if path.suffix.casefold() == ".csv":
        try:
            frame = pd.read_csv(path, dtype={"ticker": "string"})
            record["rows"] = int(len(frame)); record["columns"] = list(frame.columns)
            if "date" in frame.columns:
                dates = pd.to_datetime(frame["date"], errors="coerce").dropna()
                record["period_start"] = dates.min().date().isoformat() if not dates.empty else None
                record["period_end"] = dates.max().date().isoformat() if not dates.empty else None
        except pd.errors.EmptyDataError:
            record["rows"] = 0; record["columns"] = []
    return record


def build_handoff_package(
    *,
    master_path: str | Path,
    quality_dir: str | Path,
    output_dir: str | Path,
    repair_dir: str | Path | None = None,
    max_file_bytes: int = 5_000_000,
    sample_data: bool = False,
) -> dict[str, Any]:
    master = Path(master_path)
    quality = Path(quality_dir)
    repair = Path(repair_dir) if repair_dir else None
    root = Path(output_dir)
    if root.exists() and any(root.iterdir()):
        raise FileExistsError(f"Handoff output must be a new or empty directory: {root}")
    summary_dir = root / "handoff_summary"
    details_dir = root / "handoff_details"
    summary_dir.mkdir(parents=True, exist_ok=True)
    details_dir.mkdir(parents=True, exist_ok=True)

    schema, master_meta = _master_schema(master)
    schema.to_csv(summary_dir / "schema.csv", index=False, encoding="utf-8")
    quality_summary = _read_csv_or_empty(quality / "quality_summary_by_ticker.csv", ["market", "ticker", "quality_status"])
    missing_year = _read_csv_or_empty(quality / "missing_summary_by_year.csv", ["market", "ticker", "year", "missing_cell_count", "missing_date_count"])
    quality_summary.to_csv(summary_dir / "quality_summary_by_ticker.csv", index=False, encoding="utf-8")
    missing_year.to_csv(summary_dir / "missing_summary_by_year.csv", index=False, encoding="utf-8")

    validation_source = quality / "validation_results.json"
    validation = json.loads(validation_source.read_text(encoding="utf-8")) if validation_source.exists() else {
        "status": "NOT_RUN", "reason": "validation_results.json was not supplied"
    }
    validation["handoff_sample_data"] = sample_data
    (summary_dir / "validation_results.json").write_text(json.dumps(validation, ensure_ascii=False, indent=2), encoding="utf-8")

    repair_summary_path = repair / "repair_summary.csv" if repair else None
    repair_json_path = repair / "repair_summary.json" if repair else None
    if repair_summary_path and repair_summary_path.exists():
        repair_summary = pd.read_csv(repair_summary_path)
    elif repair_json_path and repair_json_path.exists():
        repair_summary = pd.DataFrame([json.loads(repair_json_path.read_text(encoding="utf-8"))])
    else:
        repair_summary = pd.DataFrame([{"repair_status": "NOT_RUN", "reason": "No verified repair inputs were supplied"}])
    repair_summary.to_csv(summary_dir / "repair_summary.csv", index=False, encoding="utf-8")

    detail_sources = {
        "anomaly_dates.csv": quality / "anomaly_dates.csv",
        "repair_audit.csv": repair / "repair_audit.csv" if repair else None,
        "source_comparison.csv": repair / "source_comparison.csv" if repair else None,
        "unresolved_cases.csv": repair / "unresolved_cases.csv" if repair else None,
    }
    split_validation: dict[str, Any] = {}
    detail_paths: list[Path] = []
    missing_detail = pd.concat([
        _read_csv_or_empty(quality / "missing_dates.csv", DETAIL_SPECS["missing_dates.csv"]),
        _read_csv_or_empty(quality / "missing_cells.csv", DETAIL_SPECS["missing_dates.csv"]),
    ], ignore_index=True, sort=False)
    paths, split = _write_detail_parts(missing_detail, "missing_dates.csv", details_dir, max_file_bytes)
    split_validation["missing_dates.csv"] = split
    detail_paths.extend(paths)
    for name, source in detail_sources.items():
        frame = _read_csv_or_empty(source, DETAIL_SPECS[name])
        paths, split = _write_detail_parts(frame, name, details_dir, max_file_bytes)
        split_validation[name] = split
        detail_paths.extend(paths)

    statuses = quality_summary.get("quality_status", pd.Series(dtype=str)).value_counts().to_dict()
    facts = [
        f"- 마스터 행 수: {master_meta['rows']:,}",
        f"- 종목 상태: " + (", ".join(f"{key} {value}" for key, value in statuses.items()) or "품질 요약 없음"),
        f"- 실제 복구 실행: {'예' if repair is not None and (repair / 'repair_audit.csv').exists() else '아니오'}",
        f"- 데이터 성격: {'합성 표본(운영 결과 아님)' if sample_data else '사용자 제공 마스터 기반'}",
    ]
    decision = "# 판단 요약\n\n## 확인된 사실\n\n" + "\n".join(facts) + (
        "\n\n## 미확정 사항\n\n- 검증된 외부 원자료가 없으면 실제 복구는 완료로 판정하지 않는다.\n- Alpha V1의 실제 입력 계약과 운영 데이터로 통합 실행하기 전에는 연계를 완료로 판정하지 않는다.\n"
        "\n## 필요한 판단·입력\n\n- 거래일 출처가 명시된 검증 캘린더\n- 원자료 출처·조회 시각·해시·검증 상태를 포함한 복구 후보\n- 실제 Alpha V1 Feature Engine 및 백테스트 입력 규격\n"
    )
    (summary_dir / "decision_brief.md").write_text(decision, encoding="utf-8")
    summary_readme = """# ETF 데이터 인계 요약\n\n이 폴더만으로 품질 상태, 연도별 결측, 복구 여부와 미확정 사항을 우선 검토할 수 있습니다.\n상세 사례는 `../handoff_details/`에서 종목·연도별 파일을 선택해 확인합니다. 원본 마스터는 이 패키지에 복사하지 않았습니다.\n\n검토 순서: `decision_brief.md` → `quality_summary_by_ticker.csv` → `validation_results.json` → 필요한 상세 CSV.\n"""
    (summary_dir / "README_KR.md").write_text(summary_readme, encoding="utf-8")
    root_readme = """# ETF 무료 계정 전달 패키지\n\n1. `handoff_summary/README_KR.md`와 `handoff_summary/decision_brief.md`를 먼저 엽니다.\n2. 문제가 있는 종목·연도만 `handoff_details/`에서 추가 전달합니다.\n3. `handoff_manifest.json`의 행 수·해시·분할 검증을 확인합니다.\n4. 이 패키지는 원본을 대체하지 않으며, 원본 및 재현 자료는 기존 저장소에 유지합니다.\n"""
    (root / "HANDOFF_README_KR.md").write_text(root_readme, encoding="utf-8")

    pre_manifest_files = [path for path in root.rglob("*") if path.is_file() and path.name != "handoff_manifest.json"]
    file_records = [_file_record(path, root) for path in sorted(pre_manifest_files)]
    required_summary = {name: (summary_dir / name).exists() for name in SUMMARY_FILES if name != "manifest.json"}
    size_checks = {record["path"]: record["size_bytes"] <= max_file_bytes for record in file_records if record["path"].startswith("handoff_details/")}
    missing_frame = missing_detail
    missing_unique = not missing_frame.duplicated([column for column in ["market", "ticker", "date", "field"] if column in missing_frame.columns]).any()
    package_validation = {
        "required_summary_files_present": all(required_summary.values()), "required_summary_files": required_summary,
        "detail_file_size_limit_passed": all(size_checks.values()), "detail_file_size_checks": size_checks,
        "split_row_counts_match": all(item["row_count_match"] for item in split_validation.values()),
        "split_validation": split_validation, "missing_list_has_no_duplicates": bool(missing_unique),
        "unresolved_problems_explicit": (details_dir / "unresolved_cases.csv").exists() or any(path.name.startswith("unresolved_cases_") for path in detail_paths),
        "master_relationship": master_meta,
    }
    manifest = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(), "package_kind": "FREE_ACCOUNT_HANDOFF",
        "code_version": _git_version(),
        "sample_data": sample_data, "max_file_bytes": max_file_bytes, "source_master": master_meta,
        "files": file_records, "validation": package_validation,
        "source_to_summary_relationship": "Summary/detail reports are derived diagnostics; the original master is identified by path, row count, size, and SHA-256 and is not copied.",
    }
    (root / "handoff_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    summary_manifest = {"generated_at_utc": manifest["generated_at_utc"], "files": [record for record in file_records if record["path"].startswith("handoff_summary/")], "validation": package_validation}
    (summary_dir / "manifest.json").write_text(json.dumps(summary_manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    package_validation["required_summary_files"] = {name: (summary_dir / name).exists() for name in SUMMARY_FILES}
    package_validation["required_summary_files_present"] = all(package_validation["required_summary_files"].values())
    summary_manifest["validation"] = package_validation
    (summary_dir / "manifest.json").write_text(json.dumps(summary_manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    # A manifest cannot contain its own stable hash.  All payload files are
    # hashed; handoff_manifest.json is intentionally excluded from its list.
    final_files = [_file_record(path, root) for path in sorted(root.rglob("*")) if path.is_file() and path.name != "handoff_manifest.json"]
    final_manifest = dict(manifest)
    final_manifest["files"] = final_files
    (root / "handoff_manifest.json").write_text(json.dumps(final_manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return final_manifest
