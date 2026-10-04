"""Offline release gate for recall catalog, generated data, docs, and audit text."""

from __future__ import annotations

import argparse
import json
import math
import re
import tempfile
from copy import deepcopy
from html.parser import HTMLParser
from pathlib import Path

from scrapers.recalls.core import (
    DISPLAY_KEYS,
    OFFICIAL_SOURCES,
    RecallDataError,
    canonical_snapshot_digest,
    expected_items,
    load_catalog,
    serialize_items,
    validate_catalog,
)
from scrapers.core.records import validate_vehicle_records
from scrapers.core import schema
from scrapers.core.transactions import TransactionSession, write_bytes_many
from scrapers.core.writer import csv_bytes, json_bytes
import scrapers.run_all as run_all
from scrapers.run_all import BRAND_REGISTRY, BRAND_KEYS, REGISTRY

ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT / "data"
DOCS_DATA = ROOT / "docs" / "data.json"
RECALL_VIEWER_HTML = ROOT / "docs" / "models.html"
README = ROOT / "README.md"
REPORT = ROOT / ".agents" / "tasks" / "vehicle-recall-history-report.md"


def load_json(path):
    return json.loads(
        path.read_text(encoding="utf-8"),
        parse_constant=lambda value: (_ for _ in ()).throw(
            ValueError(f"non-finite constant {value}")
        ),
    )


def validate_recall_value(value, expected, location):
    errors = []
    if value == "":
        if expected != "":
            errors.append(f"{location}: missing catalog projection")
        return errors
    if not isinstance(value, str):
        return [f"{location}: recall_history must be a scalar string"]
    try:
        items = json.loads(value, parse_constant=lambda v: (_ for _ in ()).throw(ValueError(v)))
    except (json.JSONDecodeError, ValueError) as exc:
        return [f"{location}: malformed recall_history JSON: {exc}"]
    if not isinstance(items, list) or not 1 <= len(items) <= 100:
        errors.append(f"{location}: recall_history must contain 1..100 items")
    else:
        for index, item in enumerate(items):
            if not isinstance(item, dict) or set(item) != set(DISPLAY_KEYS):
                errors.append(f"{location}[{index}]: display keys differ from schema")
            elif any(not isinstance(item[key], str) or len(item[key]) > 2048 for key in DISPLAY_KEYS):
                errors.append(f"{location}[{index}]: display values must be strings up to 2048 characters")
    if value != expected:
        errors.append(f"{location}: value differs from audited catalog projection")
    return errors


class RecallHTMLParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.capture = None
        self.host_payloads = []
        self.coverage = []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "script" and values.get("id") == "recall-source-hosts":
            self.capture = []
        if values.get("id") == "recall-coverage-note":
            self.coverage.append(values.get("data-audit-cutoff"))

    def handle_data(self, data):
        if self.capture is not None:
            self.capture.append(data)

    def handle_endtag(self, tag):
        if tag == "script" and self.capture is not None:
            self.host_payloads.append("".join(self.capture))
            self.capture = None


def validate_equal(actual, expected, location):
    return [] if actual == expected else [f"{location}: generated content differs from expected"]


def validate_html_text(html, cutoff):
    errors = []
    parser = RecallHTMLParser()
    parser.feed(html)
    expected_hosts = {
        source_id: list(item["hosts"])
        for source_id, item in OFFICIAL_SOURCES.items()
    }
    if len(parser.host_payloads) != 1:
        errors.append("docs/models.html: expected one #recall-source-hosts")
    else:
        try:
            actual_hosts = json.loads(parser.host_payloads[0])
            if actual_hosts != expected_hosts:
                errors.append(
                    "docs/models.html: official source host mapping differs from Python source"
                )
        except json.JSONDecodeError as exc:
            errors.append(f"docs/models.html: invalid source host JSON: {exc}")
    if parser.coverage != [cutoff]:
        errors.append(
            "docs/models.html: coverage cutoff element missing, duplicated, or stale"
        )
    for required in (
        "召回信息",
        "暂无已匹配记录",
        "不代表从未召回",
        "textContent",
        "noopener noreferrer",
        "width: 320px",
        "min-width: 320px",
        "max-width: 320px",
        "-webkit-line-clamp: 2",
        "overflow-wrap: anywhere",
        "word-break: break-word",
    ):
        if required not in html:
            errors.append(
                f"docs/models.html: missing required UI/security token {required!r}"
            )
    return errors


def _validate_workspace():
    errors = []
    catalog = load_catalog()
    keys = [entry.key for entry in BRAND_REGISTRY]
    expected_files = {DATA_DIR / f"{key}.json" for key in keys}
    actual_files = set(DATA_DIR.glob("*.json"))
    if actual_files != expected_files:
        errors.append(f"data/: JSON files differ from REGISTRY: {sorted(map(str, actual_files ^ expected_files))}")

    all_rows = []
    rows_by_key = {}
    vehicle_keys = set()
    for key in sorted(keys):
        path = DATA_DIR / f"{key}.json"
        try:
            rows = load_json(path)
        except Exception as exc:
            errors.append(f"{path}: invalid JSON: {exc}")
            continue
        rows_by_key[key] = rows
        row_errors = validate_vehicle_records(rows)
        errors.extend(f"{path}: {error}" for error in row_errors)
        for row in rows:
            pair = (row.get("brand"), row.get("model"))
            if pair in vehicle_keys:
                errors.append(f"{path}: duplicate global vehicle {pair}")
            vehicle_keys.add(pair)
        all_rows.extend(rows)
        csv_path = DATA_DIR / f"{key}.csv"
        actual_csv = csv_path.read_bytes() if csv_path.exists() else None
        errors.extend(validate_equal(actual_csv, csv_bytes(rows), str(csv_path)))

    errors.extend(validate_catalog(catalog, vehicle_keys=vehicle_keys))
    try:
        projection = expected_items(catalog)
    except RecallDataError as exc:
        errors.extend(exc.errors)
        projection = {}
    for row in all_rows:
        pair = (row["brand"], row["model"])
        expected = serialize_items(projection.get(pair, []))
        errors.extend(validate_recall_value(row.get("recall_history"), expected, f"{pair[0]}/{pair[1]}"))

    try:
        docs_rows = load_json(DOCS_DATA)
        errors.extend(validate_equal(docs_rows, all_rows, str(DOCS_DATA)))
    except Exception as exc:
        errors.append(f"{DOCS_DATA}: invalid JSON: {exc}")

    if len(schema.FIELDS) != 18 or schema.FIELDS[-1] != "recall_history" or schema.LABELS.get("recall_history") != "召回信息":
        errors.append("scrapers/core/schema.py: recall_history must be field 18 labeled 召回信息")

    html = RECALL_VIEWER_HTML.read_text(encoding="utf-8")
    cutoff = catalog["coverage"]["audit_cutoff"]
    errors.extend(validate_html_text(html, cutoff))

    readme = README.read_text(encoding="utf-8")
    for required in (
        "18 个规范字段",
        "recall_history",
        "scrapers/recalls/snapshots/samr-su7-2026-10-01.json",
        cutoff,
        "不代表从未召回",
        "不保证历史绝对完整",
        "2025-01-24",
        "2025-09-19",
        "scrapers/recalls/snapshots/samr-su7-2026-10-01.json",
        "可审计调查候选集",
    ):
        if required not in readme:
            errors.append(f"README.md: missing required recall documentation {required!r}")

    if not REPORT.exists():
        errors.append(f"{REPORT}: report is missing")
    else:
        report = REPORT.read_text(encoding="utf-8")
        marker_groups = {
            "investigation": catalog["investigations"],
            "notice": catalog["notices"],
            "event": catalog["events"],
            "match": catalog["matches"],
            "skip": catalog["skips"],
        }
        known = set()
        for kind, records in marker_groups.items():
            key = f"{kind}_id"
            for record in records:
                marker = f"[{kind}:{record[key]}]"
                known.add(marker)
                if report.count(marker) != 1:
                    errors.append(f"report: marker {marker} must appear exactly once")
        present = set(re.findall(r"\[(?:investigation|notice|event|match|skip):[^\]]+\]", report))
        for unknown in sorted(present - known):
            errors.append(f"report: unknown audit marker {unknown}")
        for source in catalog["coverage"]["sources_checked"]:
            for key in ("candidate_snapshot_id", "candidate_snapshot_path"):
                if source[key] not in report:
                    errors.append(f"report: missing candidate snapshot reference {source[key]!r}")
    return errors, catalog, len(all_rows)


class _SimulatedCrash(BaseException):
    """Test-only process-death surrogate that bypasses transaction rollback."""


def _transaction_fixture(root: Path):
    """Copy the real eight-brand shape into an isolated 16-file transaction."""
    old = {}
    new = {}
    payloads = {}
    all_rows = []
    for entry in BRAND_REGISTRY:
        key = entry.key
        source_json = DATA_DIR / f"{key}.json"
        source_csv = DATA_DIR / f"{key}.csv"
        rows = load_json(source_json)
        row_errors = validate_vehicle_records(rows)
        if row_errors:
            raise AssertionError(f"transaction fixture has invalid {key} rows: {row_errors}")
        all_rows.extend(rows)
        changed = deepcopy(rows)
        changed[0]["recall_history"] = "__transaction_crash_recovery_probe__"
        json_target = root / "data" / source_json.name
        csv_target = root / "data" / source_csv.name
        json_target.parent.mkdir(parents=True, exist_ok=True)
        old[json_target] = source_json.read_bytes()
        old[csv_target] = source_csv.read_bytes()
        json_target.write_bytes(old[json_target])
        csv_target.write_bytes(old[csv_target])
        new[json_target] = json_bytes(changed)
        new[csv_target] = csv_bytes(changed)
        payloads[json_target] = new[json_target]
        payloads[csv_target] = new[csv_target]
    if len(all_rows) != 91 or validate_vehicle_records(all_rows):
        raise AssertionError("transaction fixture must contain 91 valid vehicle records")
    return payloads, old, new


def _assert_transaction_batch(root: Path, old: dict[Path, bytes], new: dict[Path, bytes], expected: str) -> None:
    actual = {target: target.read_bytes() for target in old}
    expected_bytes = old if expected == "old" else new
    if actual != expected_bytes:
        old_matches = all(actual[target] == old[target] for target in old)
        new_matches = all(actual[target] == new[target] for target in new)
        raise AssertionError(f"transaction left a mixed batch (old={old_matches}, new={new_matches})")
    rows = []
    for entry in BRAND_REGISTRY:
        json_path = root / "data" / f"{entry.key}.json"
        csv_path = root / "data" / f"{entry.key}.csv"
        brand_rows = load_json(json_path)
        errors = validate_vehicle_records(brand_rows)
        if errors:
            raise AssertionError(f"transaction recovery invalidated {entry.key}: {errors}")
        if not csv_path.read_bytes().startswith(b"\xef\xbb\xbf"):
            raise AssertionError(f"transaction recovery removed CSV BOM: {csv_path}")
        if csv_path.read_bytes() != csv_bytes(brand_rows):
            raise AssertionError(f"transaction recovery changed CSV fields: {csv_path}")
        rows.extend(brand_rows)
    if len(rows) != 91 or validate_vehicle_records(rows):
        raise AssertionError("transaction recovery must preserve 91 valid records")
    artifacts = {path.name for path in (root / ".scraper-transactions").iterdir()}
    if artifacts != {".lock"}:
        raise AssertionError(f"transaction artifacts leaked: {sorted(artifacts)}")


def run_transaction_recovery_self_test() -> int:
    """Exercise every durable transaction boundary with uncaught crash semantics."""
    with tempfile.TemporaryDirectory(prefix="recall-transaction-") as directory:
        root = Path(directory)
        payloads, old, new = _transaction_fixture(root)
        ordered_count = len(payloads)
        scenarios = [
            ("first backup", lambda point, ordinal: point == "after_backup" and ordinal == 0, "old"),
            ("before prepared", lambda point, ordinal: point == "before_prepared", "old"),
            ("after prepared", lambda point, ordinal: point == "after_prepared", "old"),
        ]
        scenarios.extend(
            (
                f"replace {ordinal + 1}/{ordered_count}",
                lambda point, index, expected=ordinal: point == "after_replace" and index == expected,
                "old",
            )
            for ordinal in range(ordered_count)
        )
        scenarios.extend(
            [
                ("before committed", lambda point, ordinal: point == "before_committed", "old"),
                ("before cleanup", lambda point, ordinal: point == "before_cleanup", "new"),
            ]
        )
        for name, should_crash, expected in scenarios:
            def injector(point, ordinal, should_crash=should_crash):
                if should_crash(point, ordinal):
                    raise _SimulatedCrash(name)

            try:
                with TransactionSession(root) as session:
                    write_bytes_many(payloads, session, fault_injector=injector)
            except _SimulatedCrash:
                pass
            else:
                raise AssertionError(f"transaction crash point was not reached: {name}")
            # A new process/session recovers while holding the same exclusive lock.
            with TransactionSession(root):
                pass
            _assert_transaction_batch(root, old, new, expected)

        with TransactionSession(root) as session:
            write_bytes_many(payloads, session)
        _assert_transaction_batch(root, old, new, "new")
    return len(scenarios) + 1


def run_registry_integration_self_test() -> int:
    """Run the exact registry scheduler offline without writing repository data."""
    original_call = run_all._call
    source_records = {}
    try:
        for entry in BRAND_REGISTRY:
            rows = load_json(DATA_DIR / f"{entry.key}.json")
            source_records[entry.key] = deepcopy(rows)
            for row in rows:
                row["recall_history"] = ""

        # Use the real projection adapter once; it is intentionally the first
        # registry entry, while all brand payloads are deterministic fixtures.
        projection = run_all._recalls()

        def fake_call(entry):
            return projection if entry.kind == "enricher" else deepcopy(source_records[entry.key])

        run_all._call = fake_call
        pending = run_all.collect_batch()
    finally:
        run_all._call = original_call
    if tuple(entry.key for entry in REGISTRY) != ("recalls", "byd", "xiaomi", "tesla", "nio", "xpeng", "lixiang", "zeekr", "aito"):
        raise AssertionError("registry order or enrichment membership changed")
    if set(pending) != set(BRAND_KEYS):
        raise AssertionError("registry integration produced unauthorized brand keys")
    merged = [row for rows in pending.values() for row in rows]
    if len(merged) != 91 or validate_vehicle_records(merged):
        raise AssertionError("registry integration did not preserve 91 valid records")
    expected = expected_items(projection.catalog)
    for entry in BRAND_REGISTRY:
        before = source_records[entry.key]
        after = pending[entry.key]
        if len(before) != len(after):
            raise AssertionError(f"registry integration changed record count: {entry.key}")
        for original, result in zip(before, after):
            if {key: value for key, value in original.items() if key != "recall_history"} != {key: value for key, value in result.items() if key != "recall_history"}:
                raise AssertionError(f"registry integration changed non-recall fields: {entry.key}")
            expected_value = serialize_items(expected.get((result["brand"], result["model"]), []))
            if result["recall_history"] != expected_value:
                raise AssertionError(f"registry integration produced wrong projection: {result['brand']}/{result['model']}")
    return 1


def run_self_test():
    catalog = load_catalog()
    cases = []

    def case(name, mutate, needle):
        candidate = deepcopy(catalog)
        mutate(candidate)
        found = validate_catalog(candidate, vehicle_keys={("小米", "SU7"), ("小米", "SU7 Ultra")})
        if not found or not any(needle in error for error in found):
            cases.append(f"{name}: expected failure containing {needle!r}, got {found}")

    def snapshot_case(name, mutate, needle):
        candidate = deepcopy(catalog)
        snapshot_path = candidate["coverage"]["sources_checked"][0]["candidate_snapshot_path"]
        snapshot = load_json(ROOT / snapshot_path)
        mutate(candidate, snapshot)
        snapshot["snapshot_sha256"] = canonical_snapshot_digest(snapshot)
        candidate["coverage"]["sources_checked"][0]["candidate_snapshot_sha256"] = snapshot["snapshot_sha256"]
        found = validate_catalog(
            candidate,
            vehicle_keys={("小米", "SU7"), ("小米", "SU7 Ultra")},
            snapshot_overrides={snapshot_path: snapshot},
        )
        if not found or not any(needle in error for error in found):
            cases.append(f"{name}: expected failure containing {needle!r}, got {found}")

    case("illegal URL", lambda c: c["notices"][0].__setitem__("official_url", "https://example.com/a"), "allowlist")
    case("URL credentials", lambda c: c["notices"][0].__setitem__("official_url", "https://user@www.samr.gov.cn/a"), "allowlist")
    case("URL non-default port", lambda c: c["notices"][0].__setitem__("official_url", "https://www.samr.gov.cn:444/a"), "allowlist")
    case("URL root path", lambda c: c["notices"][0].__setitem__("official_url", "https://www.samr.gov.cn/"), "allowlist")
    case("invalid date", lambda c: c["notices"][0].__setitem__("announcement_date", "2025-02-30"), "invalid calendar date")
    case("bad coverage basis", lambda c: c["notices"][0].__setitem__("coverage_date_basis", "recall_start_date"), "invalid value")
    case("future cutoff", lambda c: c["coverage"].__setitem__("audit_cutoff", "2999-01-01"), "future")
    case("duplicate source row", lambda c: c["coverage"]["sources_checked"].append(deepcopy(c["coverage"]["sources_checked"][0])), "duplicate")
    case("notice later than verification", lambda c: c["notices"][0].__setitem__("verified_on", "2024-01-01"), "later than verified_on")
    case("missing field evidence", lambda c: c["notices"][0]["fact_evidence"].__setitem__("scope", ""), "evidence/value state mismatch")
    case("number flag conflict", lambda c: c["notices"][1].__setitem__("recall_number_provided", False), "inconsistent provided flag")
    case("duplicate notice ID", lambda c: c["notices"][1].__setitem__("notice_id", c["notices"][0]["notice_id"]), "duplicate")
    case("duplicate notice URL", lambda c: c["notices"][1].__setitem__("official_url", c["notices"][0]["official_url"]), "duplicates")
    case("event reference mismatch", lambda c: c["events"][0].__setitem__("primary_notice_id", c["notices"][1]["notice_id"]), "not in notice_ids")
    case("unclosed investigation", lambda c: c["investigations"][0].update({"status": "unreachable", "notice_id": c["notices"][0]["notice_id"]}), "must be empty")
    case("unreviewed candidate pair", lambda c: c.__setitem__("duplicate_reviews", []), "not reviewed")
    case("target dimension unbound", lambda c: [doc.__setitem__("supports", [v for v in doc["supports"] if v != "production_period"]) for doc in c["matches"][0]["target_evidence"]], "missing dimension support")
    case("duplicate match relation", lambda c: c["matches"].append({**deepcopy(c["matches"][0]), "match_id": "duplicate-match"}), "duplicate (event_id, brand, model)")
    case("missing target", lambda c: c["matches"][0].__setitem__("model", "不存在车型"), "does not exist")
    case("missing explicit skip target", lambda c: c["skips"][0].__setitem__("candidate_model", "不存在车型"), "skip target vehicle does not exist")
    case("match skip conflict", lambda c: c["skips"][0].update({"candidate_model": "SU7", "skip_id": "conflicting-skip"}), "conflicts with match")
    case("skip fixed shape", lambda c: c["skips"][0].pop("decision_subject"), "missing keys")
    case("deprecated coverage skip", lambda c: c["skips"][0].__setitem__("reason_code", "outside_coverage"), "invalid or deprecated")
    case("removed snapshot exception", lambda c: c.__setitem__("snapshot_exceptions", []), "unknown keys")

    def add_snapshot_outside_catalog_chain(candidate):
        notice = deepcopy(candidate["notices"][0])
        notice.update(
            {
                "notice_id": "samr-su7-2025-06-01",
                "event_id": "su7-external-2025-06",
                "official_url": "https://www.samr.gov.cn/zw/zh/art/2025/art_external.html",
                "title": "快照外测试公告",
                "announcement_date": "2025-06-01",
                "recall_start_date": "2025-06-01",
                "coverage_date": "2025-06-01",
                "recall_subject": "快照外测试主体",
                "affected_series": ["快照外测试车系"],
            }
        )
        notice["fact_evidence"].update(
            {
                "announcement_date": "发布时间：2025-06-01 00:00",
                "recall_start_date": "决定自2025-06-01起",
                "coverage_date": "发布时间：2025-06-01 00:00",
            }
        )
        investigation = deepcopy(candidate["investigations"][0])
        investigation.update(
            {
                "investigation_id": "inv-samr-su7-2025-06",
                "discovered_url": notice["official_url"],
                "notice_id": notice["notice_id"],
            }
        )
        event = {
            "event_id": notice["event_id"],
            "primary_notice_id": notice["notice_id"],
            "notice_ids": [notice["notice_id"]],
        }
        match = deepcopy(candidate["matches"][0])
        match.update(
            {
                "match_id": "match-su7-external-2025-06",
                "event_id": notice["event_id"],
                "basis_notice_ids": [notice["notice_id"]],
            }
        )
        candidate["investigations"].append(investigation)
        candidate["notices"].append(notice)
        candidate["events"].append(event)
        candidate["matches"].append(match)

    case(
        "snapshot outside catalog notice",
        add_snapshot_outside_catalog_chain,
        "catalog notice is outside candidate snapshots",
    )

    snapshot_case(
        "deleted snapshot candidate",
        lambda c, s: s["candidates"].pop(0),
        "catalog notice is outside candidate snapshots",
    )

    def delete_first_candidate_catalog_chain(candidate):
        notice_id = "samr-su7-2025-01-24"
        event_id = "su7-parking-2025-01"
        investigation_id = "inv-samr-su7-2025-01"
        candidate["investigations"] = [item for item in candidate["investigations"] if item["investigation_id"] != investigation_id]
        candidate["notices"] = [item for item in candidate["notices"] if item["notice_id"] != notice_id]
        candidate["events"] = [item for item in candidate["events"] if item["event_id"] != event_id]
        candidate["matches"] = [item for item in candidate["matches"] if item["event_id"] != event_id]
        candidate["skips"] = [item for item in candidate["skips"] if item["event_id"] != event_id]
        candidate["duplicate_reviews"] = []

    case(
        "deleted candidate catalog chain",
        delete_first_candidate_catalog_chain,
        "missing terminal investigation",
    )

    bad_records = [{field: "" for field in schema.FIELDS}]
    bad_records[0].update({"brand": "测试", "model": "布尔", "price_min": True})
    if not validate_vehicle_records(bad_records):
        cases.append("bool number: expected validation failure")
    bad_records[0].update({"model": "非有限", "price_min": "", "battery_kwh": math.nan})
    if not validate_vehicle_records(bad_records):
        cases.append("NaN number: expected validation failure")
    bad_records[0].update({"model": "负价格", "battery_kwh": "", "price_min": -1})
    if not validate_vehicle_records(bad_records):
        cases.append("negative price: expected validation failure")
    if not validate_recall_value("not-json", "", "test/model"):
        cases.append("malformed recall string: expected validation failure")
    if not validate_recall_value("", "expected", "test/model"):
        cases.append("missing projection: expected validation failure")

    html = RECALL_VIEWER_HTML.read_text(encoding="utf-8")
    cutoff = catalog["coverage"]["audit_cutoff"]
    broken_host_html = re.sub(
        r'(<script id="recall-source-hosts" type="application/json">).*?(</script>)',
        r'\1{"samr":["example.com"]}\2',
        html,
        count=1,
        flags=re.DOTALL,
    )
    if not any("host mapping differs" in error for error in validate_html_text(broken_host_html, cutoff)):
        cases.append("host mapping drift: expected validation failure")
    broken_cutoff_html = html.replace(
        f'data-audit-cutoff="{cutoff}"', 'data-audit-cutoff="1999-01-01"'
    )
    if not any("cutoff" in error for error in validate_html_text(broken_cutoff_html, cutoff)):
        cases.append("page cutoff drift: expected validation failure")
    broken_css_html = html.replace("max-width: 320px", "max-width: 2000px")
    if not validate_html_text(broken_css_html, cutoff):
        cases.append("recall CSS drift: expected validation failure")
    if not validate_equal(b"tampered", csv_bytes([]), "test.csv"):
        cases.append("CSV mismatch: expected validation failure")
    if not validate_equal([{"brand": "tampered"}], [], "docs/data.json"):
        cases.append("docs mismatch: expected validation failure")

    try:
        registry_scenarios = run_registry_integration_self_test()
    except Exception as exc:
        cases.append(f"registry integration: {exc}")
        registry_scenarios = 0
    try:
        transaction_scenarios = run_transaction_recovery_self_test()
    except Exception as exc:
        cases.append(f"transaction crash recovery: {exc}")
        transaction_scenarios = 0

    if cases:
        print("SELF-TEST FAILED")
        for error in cases:
            print(f"ERROR {error}")
        return 1
    scenario_count = 37
    print(f"OK: {scenario_count} negative validation scenarios rejected; {registry_scenarios} registry integration scenario and {transaction_scenarios} crash-recovery scenarios passed")
    return 0


def validate_workspace():
    with TransactionSession(ROOT):
        return _validate_workspace()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        return run_self_test()
    try:
        errors, catalog, count = validate_workspace()
    except (RecallDataError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"ERROR {exc}")
        return 1
    if errors:
        for error in errors:
            print(f"ERROR {error}")
        return 1
    print(
        "OK: "
        f"{len(catalog['investigations'])} investigations, "
        f"{len(catalog['notices'])} notices, {len(catalog['events'])} events, "
        f"{len(catalog['matches'])} matches, {len(catalog['skips'])} skips, "
        f"{count} vehicles, cutoff {catalog['coverage']['audit_cutoff']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
