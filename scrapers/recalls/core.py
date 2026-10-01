"""Strict recall catalog validation, projection, and atomic file helpers."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
import unicodedata
from copy import deepcopy
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from scrapers.core import schema

PACKAGE_DIR = Path(__file__).resolve().parent
REPO_ROOT = PACKAGE_DIR.parents[1]
CATALOG_PATH = PACKAGE_DIR / "catalog.json"
ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,99}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
ALIGNMENT_DIMENSIONS = (
    "brand",
    "model_name",
    "production_period",
    "generation_or_sales_period",
    "powertrain_or_variant",
    "import_domestic_status",
)
DISPLAY_KEYS = (
    "event_id",
    "notice_id",
    "source_id",
    "issuer",
    "title",
    "official_url",
    "announcement_date",
    "recall_start_date",
    "recall_number",
    "scope",
    "reason",
    "remedy",
    "verified_on",
)
OFFICIAL_SOURCES = {
    "samr": {
        "issuer": "国家市场监督管理总局",
        "purposes": ("recall_publication", "vehicle_identity"),
        "hosts": ("www.samr.gov.cn",),
    },
    "xiaomi": {
        "issuer": "小米汽车科技有限公司",
        "purposes": ("vehicle_identity",),
        "hosts": ("www.xiaomiev.com",),
    },
}


class RecallDataError(ValueError):
    def __init__(self, errors):
        self.errors = tuple(errors)
        super().__init__("\n".join(self.errors))


def normalize_text(value):
    """NFKC, trim, then collapse all Unicode whitespace to one ASCII space."""
    return " ".join(unicodedata.normalize("NFKC", value).strip().split())


def _pairs_no_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise RecallDataError([f"catalog: duplicate JSON key {key!r}"])
        result[key] = value
    return result


def load_catalog(path=CATALOG_PATH):
    path = Path(path)
    if path.stat().st_size > 5 * 1024 * 1024:
        raise RecallDataError([f"{path}: exceeds 5 MiB"])
    try:
        return json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_pairs_no_duplicates,
            parse_constant=lambda value: (_ for _ in ()).throw(
                RecallDataError([f"catalog: non-finite constant {value}"])
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RecallDataError([f"{path}: invalid UTF-8 JSON: {exc}"]) from exc


SNAPSHOT_PATH_RE = re.compile(r"^scrapers/recalls/snapshots/([a-z0-9][a-z0-9._-]{0,99})\.json$")


def audit_repository_path(value: str) -> Path:
    """Resolve only audited repository-relative snapshot paths."""
    if not isinstance(value, str) or Path(value).is_absolute() or ".." in Path(value).parts:
        raise RecallDataError(["candidate snapshot path must be a safe repository-relative path"])
    if not SNAPSHOT_PATH_RE.fullmatch(value):
        raise RecallDataError(["candidate snapshot path is outside scrapers/recalls/snapshots"])
    return REPO_ROOT / value
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def canonical_snapshot_digest(snapshot):
    payload = {key: value for key, value in snapshot.items() if key != "snapshot_sha256"}
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _load_snapshot(path):
    try:
        if path.stat().st_size > 5 * 1024 * 1024:
            raise RecallDataError([f"{path}: exceeds 5 MiB"])
        return json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_pairs_no_duplicates,
            parse_constant=lambda value: (_ for _ in ()).throw(
                RecallDataError([f"{path}: non-finite constant {value}"])
            ),
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RecallDataError([f"{path}: invalid UTF-8 JSON: {exc}"]) from exc


def _date(value, path, errors, allow_empty=False):
    if allow_empty and value == "":
        return None
    if not isinstance(value, str) or not DATE_RE.fullmatch(value):
        errors.append(f"{path}: expected YYYY-MM-DD")
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        errors.append(f"{path}: invalid calendar date {value!r}")
        return None


def _exact_keys(obj, expected, path, errors):
    if not isinstance(obj, dict):
        errors.append(f"{path}: expected object")
        return False
    missing = sorted(set(expected) - set(obj))
    extra = sorted(set(obj) - set(expected))
    if missing:
        errors.append(f"{path}: missing keys {missing}")
    if extra:
        errors.append(f"{path}: unknown keys {extra}")
    return not missing and not extra


def _string(value, path, errors, *, empty=False, maximum=2000):
    if not isinstance(value, str):
        errors.append(f"{path}: expected string")
        return False
    if not empty and not value:
        errors.append(f"{path}: must not be empty")
    if len(value) > maximum:
        errors.append(f"{path}: exceeds {maximum} characters")
    return isinstance(value, str) and (empty or bool(value)) and len(value) <= maximum


def _valid_id(value, path, errors):
    if not isinstance(value, str) or not ID_RE.fullmatch(value):
        errors.append(f"{path}: invalid stable ID")
        return False
    return True


def normalize_url(value):
    parsed = urlsplit(value)
    host = (parsed.hostname or "").lower()
    netloc = host if parsed.port in (None, 443) else f"{host}:{parsed.port}"
    return urlunsplit((parsed.scheme.lower(), netloc, parsed.path, parsed.query, ""))


def _official_url(value, source_id, purpose, path, errors):
    if not isinstance(value, str) or len(value) > 2048:
        errors.append(f"{path}: expected URL string up to 2048 characters")
        return False
    source = OFFICIAL_SOURCES.get(source_id)
    if not source or purpose not in source["purposes"]:
        errors.append(f"{path}: source {source_id!r} is not allowed for {purpose}")
        return False
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        errors.append(f"{path}: invalid URL: {exc}")
        return False
    if (
        parsed.scheme != "https"
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or parsed.hostname not in source["hosts"]
        or parsed.path in ("", "/")
    ):
        errors.append(f"{path}: URL is outside exact official allowlist")
        return False
    return True


def _validate_candidate_snapshot(source_row, source_path, cutoff, errors, snapshot_override=None):
    snapshot_id = source_row["candidate_snapshot_id"]
    snapshot_path = source_row["candidate_snapshot_path"]
    expected_path = f"scrapers/recalls/snapshots/{snapshot_id}.json"
    if not isinstance(snapshot_id, str) or not ID_RE.fullmatch(snapshot_id):
        errors.append(f"{source_path}.candidate_snapshot_id: invalid stable ID")
        return {}
    if snapshot_path != expected_path or not SNAPSHOT_PATH_RE.fullmatch(snapshot_path):
        errors.append(f"{source_path}.candidate_snapshot_path: must be {expected_path!r}")
        return {}
    declared_digest = source_row["candidate_snapshot_sha256"]
    if not isinstance(declared_digest, str) or not SHA256_RE.fullmatch(declared_digest):
        errors.append(f"{source_path}.candidate_snapshot_sha256: expected SHA-256 hex digest")
        return {}
    try:
        snapshot = _load_snapshot(audit_repository_path(snapshot_path)) if snapshot_override is None else snapshot_override
    except RecallDataError as exc:
        errors.extend(exc.errors)
        return {}

    snapshot_keys = {
        "schema_version", "snapshot_id", "source_id", "official_index_url", "retrieved_on",
        "publication_searched_from", "publication_searched_through", "query_description",
        "scope", "candidates", "snapshot_sha256",
    }
    if not _exact_keys(snapshot, snapshot_keys, snapshot_path, errors):
        return {}
    if snapshot["schema_version"] != 1:
        errors.append(f"{snapshot_path}.schema_version: expected 1")
    if snapshot["snapshot_id"] != snapshot_id:
        errors.append(f"{snapshot_path}.snapshot_id: differs from coverage reference")
    if snapshot["source_id"] != source_row["source_id"]:
        errors.append(f"{snapshot_path}.source_id: differs from coverage source")
    if snapshot["official_index_url"] != source_row["index_url"]:
        errors.append(f"{snapshot_path}.official_index_url: differs from coverage index URL")
    if (
        snapshot["publication_searched_from"] != source_row["publication_searched_from"]
        or snapshot["publication_searched_through"] != source_row["publication_searched_through"]
    ):
        errors.append(f"{snapshot_path}: publication range differs from coverage")
    actual_digest = canonical_snapshot_digest(snapshot)
    if snapshot["snapshot_sha256"] != actual_digest:
        errors.append(f"{snapshot_path}.snapshot_sha256: does not match canonical snapshot payload")
    if declared_digest != actual_digest:
        errors.append(f"{source_path}.candidate_snapshot_sha256: differs from snapshot digest")

    retrieved = _date(snapshot["retrieved_on"], f"{snapshot_path}.retrieved_on", errors)
    start = _date(snapshot["publication_searched_from"], f"{snapshot_path}.publication_searched_from", errors)
    through = _date(snapshot["publication_searched_through"], f"{snapshot_path}.publication_searched_through", errors)
    if retrieved and cutoff and retrieved > cutoff:
        errors.append(f"{snapshot_path}.retrieved_on: exceeds audit cutoff")
    if start and through and start > through:
        errors.append(f"{snapshot_path}: publication range is reversed")
    for key in ("query_description", "scope"):
        _string(snapshot[key], f"{snapshot_path}.{key}", errors, maximum=2000)
    _official_url(snapshot["official_index_url"], snapshot["source_id"], "recall_publication", f"{snapshot_path}.official_index_url", errors)

    candidate_keys = {
        "candidate_id", "official_url", "title", "announcement_date", "official_scope",
        "decision", "investigation_id", "decision_reason",
    }
    if not isinstance(snapshot["candidates"], list) or not snapshot["candidates"]:
        errors.append(f"{snapshot_path}.candidates: expected non-empty array")
        return {}
    candidates = {}
    urls = {}
    for index, candidate in enumerate(snapshot["candidates"]):
        path = f"{snapshot_path}.candidates[{index}]"
        if not _exact_keys(candidate, candidate_keys, path, errors):
            continue
        cid = candidate["candidate_id"]
        _valid_id(cid, f"{path}.candidate_id", errors)
        if cid in candidates:
            errors.append(f"{path}.candidate_id: duplicate {cid!r}")
        candidates[cid] = candidate
        if _official_url(candidate["official_url"], snapshot["source_id"], "recall_publication", f"{path}.official_url", errors):
            normalized = normalize_url(candidate["official_url"])
            if normalized in urls:
                errors.append(f"{path}.official_url: duplicate snapshot candidate URL")
            urls[normalized] = cid
        announcement = _date(candidate["announcement_date"], f"{path}.announcement_date", errors)
        if announcement and start and through and not start <= announcement <= through:
            errors.append(f"{path}.announcement_date: outside snapshot publication range")
        if announcement and retrieved and announcement > retrieved:
            errors.append(f"{path}.announcement_date: later than snapshot retrieval")
        for key in ("title", "official_scope", "decision_reason"):
            _string(candidate[key], f"{path}.{key}", errors, maximum=2000)
        if candidate["decision"] not in {"included_matched", "included_unmatched", "excluded"}:
            errors.append(f"{path}.decision: invalid candidate decision")
        _valid_id(candidate["investigation_id"], f"{path}.investigation_id", errors)
    return candidates


def _overlap(a_start, a_end, b_start, b_end):
    return max(a_start, b_start) <= min(a_end, b_end)


def validate_catalog(catalog, *, vehicle_keys=None, today=None, snapshot_overrides=None):
    errors = []
    snapshot_overrides = {} if snapshot_overrides is None else snapshot_overrides
    top_keys = {
        "schema_version",
        "coverage",
        "investigations",
        "notices",
        "events",
        "duplicate_reviews",
        "matches",
        "skips",
    }
    if not _exact_keys(catalog, top_keys, "catalog", errors):
        return errors
    if catalog["schema_version"] != 3:
        errors.append("catalog.schema_version: expected 3")
    for collection in (
        "investigations",
        "notices",
        "events",
        "duplicate_reviews",
        "matches",
        "skips",
    ):
        if not isinstance(catalog[collection], list):
            errors.append(f"catalog.{collection}: expected array")

    coverage_keys = {"audit_cutoff", "known_gaps", "sources_checked"}
    coverage = catalog["coverage"]
    if not _exact_keys(coverage, coverage_keys, "coverage", errors):
        return errors
    cutoff = _date(coverage["audit_cutoff"], "coverage.audit_cutoff", errors)
    utc_today = date.today() if today is None else today
    if cutoff and cutoff > utc_today:
        errors.append("coverage.audit_cutoff: must not be in the future UTC date")
    gaps = coverage["known_gaps"]
    if not isinstance(gaps, list) or not gaps or not all(isinstance(v, str) and v for v in gaps):
        errors.append("coverage.known_gaps: expected non-empty string array")

    source_rows = {}
    source_keys = {
        "source_id", "name", "index_url", "publication_searched_from",
        "publication_searched_through", "checked_on", "method", "limitations",
        "candidate_snapshot_id", "candidate_snapshot_path", "candidate_snapshot_sha256",
    }
    snapshot_candidates_by_source = {}
    if not isinstance(coverage["sources_checked"], list):
        errors.append("coverage.sources_checked: expected array")
    else:
        for index, row in enumerate(coverage["sources_checked"]):
            path = f"coverage.sources_checked[{index}]"
            if not _exact_keys(row, source_keys, path, errors):
                continue
            source_id = row["source_id"]
            if source_id in source_rows:
                errors.append(f"{path}.source_id: duplicate {source_id!r}")
            source_rows[source_id] = row
            source = OFFICIAL_SOURCES.get(source_id)
            if not source or "recall_publication" not in source["purposes"]:
                errors.append(f"{path}.source_id: not a recall publication source")
            _official_url(row["index_url"], source_id, "recall_publication", f"{path}.index_url", errors)
            start = _date(row["publication_searched_from"], f"{path}.publication_searched_from", errors)
            through = _date(row["publication_searched_through"], f"{path}.publication_searched_through", errors)
            checked = _date(row["checked_on"], f"{path}.checked_on", errors)
            if start and through and start > through:
                errors.append(f"{path}: publication range is reversed")
            if through and cutoff and through > cutoff:
                errors.append(f"{path}: publication range exceeds audit cutoff")
            if checked and cutoff and checked > cutoff:
                errors.append(f"{path}.checked_on: exceeds audit cutoff")
            for key in ("name", "method", "limitations"):
                _string(row[key], f"{path}.{key}", errors)
            snapshot_candidates_by_source[source_id] = _validate_candidate_snapshot(
                row, path, cutoff, errors, snapshot_overrides.get(row["candidate_snapshot_path"])
            )

    investigation_keys = {
        "investigation_id", "discovered_url", "discovered_on", "candidate_brand",
        "candidate_model", "status", "notice_id", "reason", "evidence_excerpt",
    }
    investigations = {}
    for index, item in enumerate(catalog["investigations"] if isinstance(catalog["investigations"], list) else []):
        path = f"investigations[{index}]"
        if not _exact_keys(item, investigation_keys, path, errors):
            continue
        iid = item["investigation_id"]
        _valid_id(iid, f"{path}.investigation_id", errors)
        if iid in investigations:
            errors.append(f"{path}.investigation_id: duplicate {iid!r}")
        investigations[iid] = item
        discovered = _date(item["discovered_on"], f"{path}.discovered_on", errors)
        if discovered and cutoff and discovered > cutoff:
            errors.append(f"{path}.discovered_on: exceeds audit cutoff")
        if item["status"] not in {"verified_notice", "unreachable", "incomplete_body", "unconfirmed_source", "rejected"}:
            errors.append(f"{path}.status: invalid terminal status")
        if item["status"] == "verified_notice" and not item["notice_id"]:
            errors.append(f"{path}.notice_id: required for verified_notice")
        if item["status"] != "verified_notice" and item["notice_id"]:
            errors.append(f"{path}.notice_id: must be empty unless verified_notice")
        for key in ("discovered_url", "candidate_brand", "candidate_model", "evidence_excerpt"):
            _string(item[key], f"{path}.{key}", errors, empty=True, maximum=4000)
        _string(item["reason"], f"{path}.reason", errors)

    notice_keys = {
        "notice_id", "event_id", "source_id", "issuer", "title", "official_url",
        "announcement_date", "recall_start_date", "recall_number_provided", "recall_number",
        "scope", "reason", "remedy", "verified_on", "coverage_date", "coverage_date_basis",
        "coverage_note", "recall_subject", "affected_series", "production_start",
        "production_end", "fact_evidence",
    }
    evidence_keys = {
        "announcement_date", "recall_start_date", "recall_number", "scope", "reason",
        "remedy", "coverage_date",
    }
    notices = {}
    normalized_urls = {}
    used_publication_sources = set()
    for index, item in enumerate(catalog["notices"] if isinstance(catalog["notices"], list) else []):
        path = f"notices[{index}]"
        if not _exact_keys(item, notice_keys, path, errors):
            continue
        nid = item["notice_id"]
        _valid_id(nid, f"{path}.notice_id", errors)
        _valid_id(item["event_id"], f"{path}.event_id", errors)
        if nid in notices:
            errors.append(f"{path}.notice_id: duplicate {nid!r}")
        notices[nid] = item
        source_id = item["source_id"]
        source = OFFICIAL_SOURCES.get(source_id)
        used_publication_sources.add(source_id)
        if not source or item["issuer"] != source["issuer"]:
            errors.append(f"{path}.issuer: does not match source")
        if _official_url(item["official_url"], source_id, "recall_publication", f"{path}.official_url", errors):
            norm = normalize_url(item["official_url"])
            if norm in normalized_urls:
                errors.append(f"{path}.official_url: duplicates {normalized_urls[norm]}")
            normalized_urls[norm] = nid
        announcement = _date(item["announcement_date"], f"{path}.announcement_date", errors, allow_empty=True)
        recall_start = _date(item["recall_start_date"], f"{path}.recall_start_date", errors, allow_empty=True)
        verified = _date(item["verified_on"], f"{path}.verified_on", errors)
        coverage_date = _date(item["coverage_date"], f"{path}.coverage_date", errors, allow_empty=True)
        production_start = _date(item["production_start"], f"{path}.production_start", errors)
        production_end = _date(item["production_end"], f"{path}.production_end", errors)
        if not announcement and not recall_start:
            errors.append(f"{path}: announcement_date or recall_start_date required")
        if verified and cutoff and verified > cutoff:
            errors.append(f"{path}.verified_on: exceeds audit cutoff")
        for value, key in ((announcement, "announcement_date"), (coverage_date, "coverage_date")):
            if value and verified and value > verified:
                errors.append(f"{path}.{key}: later than verified_on")
        if production_start and production_end and production_start > production_end:
            errors.append(f"{path}: production period is reversed")
        basis = item["coverage_date_basis"]
        if basis not in {"announcement_date", "official_index_date", ""}:
            errors.append(f"{path}.coverage_date_basis: invalid value")
        if basis == "announcement_date" and item["coverage_date"] != item["announcement_date"]:
            errors.append(f"{path}.coverage_date: must equal announcement_date")
        if basis and item["coverage_note"]:
            errors.append(f"{path}.coverage_note: must be empty when basis is known")
        if not basis and (item["coverage_date"] or not item["coverage_note"]):
            errors.append(f"{path}: unknown coverage date requires empty date and a note")
        source_row = source_rows.get(source_id)
        if source_row and coverage_date:
            lo = date.fromisoformat(source_row["publication_searched_from"])
            hi = date.fromisoformat(source_row["publication_searched_through"])
            if not lo <= coverage_date <= hi:
                errors.append(f"{path}.coverage_date: outside declared source range")
        if not isinstance(item["recall_number_provided"], bool):
            errors.append(f"{path}.recall_number_provided: expected bool")
        elif item["recall_number_provided"] != bool(item["recall_number"]):
            errors.append(f"{path}.recall_number: inconsistent provided flag")
        if not isinstance(item["affected_series"], list) or not item["affected_series"] or not all(isinstance(v, str) and v for v in item["affected_series"]):
            errors.append(f"{path}.affected_series: expected non-empty string array")
        elif len({normalize_text(v) for v in item["affected_series"]}) != len(item["affected_series"]):
            errors.append(f"{path}.affected_series: duplicate normalized value")
        for key in ("title", "scope", "reason", "remedy", "recall_subject"):
            _string(item[key], f"{path}.{key}", errors, maximum=2000 if key in {"scope", "reason", "remedy"} else 300)
        _string(item["recall_number"], f"{path}.recall_number", errors, empty=True, maximum=100)
        if _exact_keys(item["fact_evidence"], evidence_keys, f"{path}.fact_evidence", errors):
            for key in evidence_keys:
                evidence = item["fact_evidence"][key]
                _string(evidence, f"{path}.fact_evidence.{key}", errors, empty=True, maximum=4000)
                value = item[key]
                required = bool(value) if key not in {"scope", "reason", "remedy"} else True
                if bool(evidence) != required:
                    errors.append(f"{path}.fact_evidence.{key}: evidence/value state mismatch")

    number_events = {}
    title_date_events = {}
    for notice in notices.values():
        if notice["recall_number"]:
            number_key = (notice["issuer"], normalize_text(notice["recall_number"]))
            previous = number_events.get(number_key)
            if previous and previous != notice["event_id"]:
                errors.append(
                    f"notice {notice['notice_id']}: official recall number appears in distinct events"
                )
            number_events[number_key] = notice["event_id"]
        title_date_key = (
            normalize_text(notice["title"]),
            notice["announcement_date"],
        )
        previous = title_date_events.get(title_date_key)
        if previous and previous != notice["event_id"]:
            errors.append(
                f"notice {notice['notice_id']}: normalized title/date appears in distinct events"
            )
        title_date_events[title_date_key] = notice["event_id"]

    if used_publication_sources != set(source_rows):
        errors.append("coverage.sources_checked: must contain exactly one row per used recall publication source")
    for notice in notices.values():
        refs = [i for i in investigations.values() if i.get("notice_id") == notice["notice_id"] and i.get("status") == "verified_notice"]
        if not refs:
            errors.append(f"notice {notice['notice_id']}: no verified investigation")
    for iid, investigation in investigations.items():
        if investigation.get("status") == "verified_notice" and investigation.get("notice_id") not in notices:
            errors.append(f"investigation {iid}: unknown notice_id")

    event_keys = {"event_id", "primary_notice_id", "notice_ids"}
    events = {}
    notice_event_refs = {}
    for index, item in enumerate(catalog["events"] if isinstance(catalog["events"], list) else []):
        path = f"events[{index}]"
        if not _exact_keys(item, event_keys, path, errors):
            continue
        eid = item["event_id"]
        _valid_id(eid, f"{path}.event_id", errors)
        if eid in events:
            errors.append(f"{path}.event_id: duplicate {eid!r}")
        events[eid] = item
        if not isinstance(item["notice_ids"], list) or not item["notice_ids"] or len(set(item["notice_ids"])) != len(item["notice_ids"]):
            errors.append(f"{path}.notice_ids: expected non-empty unique array")
            continue
        if item["primary_notice_id"] not in item["notice_ids"]:
            errors.append(f"{path}.primary_notice_id: not in notice_ids")
        for nid in item["notice_ids"]:
            if nid not in notices or notices[nid]["event_id"] != eid:
                errors.append(f"{path}.notice_ids: inconsistent notice {nid!r}")
            if nid in notice_event_refs:
                errors.append(f"{path}.notice_ids: notice {nid!r} belongs to multiple events")
            notice_event_refs[nid] = eid
    if set(notices) != set(notice_event_refs):
        errors.append("events: notice/event references are not closed")

    review_keys = {"review_id", "notice_ids", "decision", "reason"}
    reviews = {}
    review_pairs = {}
    for index, item in enumerate(catalog["duplicate_reviews"] if isinstance(catalog["duplicate_reviews"], list) else []):
        path = f"duplicate_reviews[{index}]"
        if not _exact_keys(item, review_keys, path, errors):
            continue
        rid = item["review_id"]
        _valid_id(rid, f"{path}.review_id", errors)
        if rid in reviews:
            errors.append(f"{path}.review_id: duplicate {rid!r}")
        reviews[rid] = item
        pair = item["notice_ids"]
        if not isinstance(pair, list) or len(pair) != 2 or pair != sorted(pair) or len(set(pair)) != 2:
            errors.append(f"{path}.notice_ids: expected two distinct sorted IDs")
            continue
        pair_key = tuple(pair)
        if pair_key in review_pairs:
            errors.append(f"{path}.notice_ids: duplicate reviewed pair")
        review_pairs[pair_key] = item
        if any(nid not in notices for nid in pair):
            errors.append(f"{path}.notice_ids: unknown notice")
            continue
        same = notices[pair[0]]["event_id"] == notices[pair[1]]["event_id"]
        if item["decision"] not in {"same_event", "distinct_event"}:
            errors.append(f"{path}.decision: invalid value")
        elif same != (item["decision"] == "same_event"):
            errors.append(f"{path}.decision: inconsistent with events")
        _string(item["reason"], f"{path}.reason", errors)

    notice_list = list(notices.values())
    required_candidate_pairs = set()
    for left_index, left in enumerate(notice_list):
        for right in notice_list[left_index + 1:]:
            same_subject = normalize_text(left["recall_subject"]) == normalize_text(right["recall_subject"])
            same_series = set(map(normalize_text, left["affected_series"])) & set(map(normalize_text, right["affected_series"]))
            overlap = _overlap(left["production_start"], left["production_end"], right["production_start"], right["production_end"])
            if same_subject and same_series and overlap:
                pair = tuple(sorted((left["notice_id"], right["notice_id"])))
                required_candidate_pairs.add(pair)
                if left["event_id"] != right["event_id"] and pair not in review_pairs:
                    errors.append(f"duplicate_reviews: candidate pair {pair} is not reviewed")
    for pair in review_pairs:
        if pair not in required_candidate_pairs:
            errors.append(f"duplicate_reviews: pair {pair} is not generated by the candidate rule")

    alignment_keys = {"notice_constraint", "status", "evidence_excerpt", "rationale"}
    target_doc_keys = {"evidence_id", "source_id", "issuer", "official_vehicle_url", "verified_on", "supports", "evidence_excerpt"}
    match_keys = {
        "match_id", "event_id", "brand", "model", "basis_notice_ids",
        "announcement_model_text", "notice_evidence_excerpt", "match_reason",
        "alignment", "target_evidence",
    }
    matches = {}
    match_relations = set()
    for index, item in enumerate(catalog["matches"] if isinstance(catalog["matches"], list) else []):
        path = f"matches[{index}]"
        if not _exact_keys(item, match_keys, path, errors):
            continue
        mid = item["match_id"]
        _valid_id(mid, f"{path}.match_id", errors)
        if mid in matches:
            errors.append(f"{path}.match_id: duplicate {mid!r}")
        matches[mid] = item
        relation = (item["event_id"], item["brand"], item["model"])
        if relation in match_relations:
            errors.append(f"{path}: duplicate (event_id, brand, model)")
        match_relations.add(relation)
        if item["event_id"] not in events:
            errors.append(f"{path}.event_id: unknown event")
        if vehicle_keys is not None and (item["brand"], item["model"]) not in vehicle_keys:
            errors.append(f"{path}: target vehicle does not exist uniquely")
        basis = item["basis_notice_ids"]
        if not isinstance(basis, list) or not basis:
            errors.append(f"{path}.basis_notice_ids: expected non-empty array")
        elif any(nid not in notices or notices[nid]["event_id"] != item["event_id"] for nid in basis):
            errors.append(f"{path}.basis_notice_ids: notice outside event")
        if not _exact_keys(item["alignment"], set(ALIGNMENT_DIMENSIONS), f"{path}.alignment", errors):
            continue
        required_dimensions = {"brand", "model_name"}
        for dimension in ALIGNMENT_DIMENSIONS:
            value = item["alignment"][dimension]
            dim_path = f"{path}.alignment.{dimension}"
            if not _exact_keys(value, alignment_keys, dim_path, errors):
                continue
            if value["notice_constraint"] not in {"limited", "not_limited", "not_stated"}:
                errors.append(f"{dim_path}.notice_constraint: invalid value")
            if value["status"] not in {"confirmed", "not_applicable"}:
                errors.append(f"{dim_path}.status: invalid value")
            if dimension in {"brand", "model_name"} and (value["notice_constraint"], value["status"]) != ("limited", "confirmed"):
                errors.append(f"{dim_path}: brand/model must be limited and confirmed")
            if value["status"] == "confirmed":
                required_dimensions.add(dimension)
                _string(value["evidence_excerpt"], f"{dim_path}.evidence_excerpt", errors, maximum=4000)
            else:
                _string(value["evidence_excerpt"], f"{dim_path}.evidence_excerpt", errors, empty=True, maximum=4000)
            _string(value["rationale"], f"{dim_path}.rationale", errors)
        target_docs = item["target_evidence"]
        covered = set()
        evidence_ids = set()
        if not isinstance(target_docs, list) or not target_docs:
            errors.append(f"{path}.target_evidence: expected non-empty array")
        else:
            for doc_index, doc in enumerate(target_docs):
                doc_path = f"{path}.target_evidence[{doc_index}]"
                if not _exact_keys(doc, target_doc_keys, doc_path, errors):
                    continue
                _valid_id(doc["evidence_id"], f"{doc_path}.evidence_id", errors)
                if doc["evidence_id"] in evidence_ids:
                    errors.append(f"{doc_path}.evidence_id: duplicate")
                evidence_ids.add(doc["evidence_id"])
                source = OFFICIAL_SOURCES.get(doc["source_id"])
                if not source or doc["issuer"] != source["issuer"]:
                    errors.append(f"{doc_path}.issuer: does not match source")
                _official_url(doc["official_vehicle_url"], doc["source_id"], "vehicle_identity", f"{doc_path}.official_vehicle_url", errors)
                checked = _date(doc["verified_on"], f"{doc_path}.verified_on", errors)
                if checked and cutoff and checked > cutoff:
                    errors.append(f"{doc_path}.verified_on: exceeds cutoff")
                supports = doc["supports"]
                if not isinstance(supports, list) or not supports or len(set(supports)) != len(supports) or any(v not in ALIGNMENT_DIMENSIONS for v in supports):
                    errors.append(f"{doc_path}.supports: invalid alignment dimension array")
                else:
                    covered.update(supports)
                _string(doc["evidence_excerpt"], f"{doc_path}.evidence_excerpt", errors, maximum=4000)
        missing_dimensions = sorted(required_dimensions - covered)
        if missing_dimensions:
            errors.append(f"{path}.target_evidence: missing dimension support {missing_dimensions}")
        for key in ("brand", "model", "announcement_model_text", "notice_evidence_excerpt", "match_reason"):
            _string(item[key], f"{path}.{key}", errors, maximum=4000)

    skip_keys = {
        "skip_id", "event_id", "investigation_id", "candidate_brand", "candidate_model",
        "decision_subject", "reason_code", "reason", "evidence_excerpt",
    }
    allowed_skip_codes = {
        "vin_only", "chassis_only", "brand_only", "ambiguous_series", "ambiguous_generation",
        "variant_unconfirmed", "production_period_unconfirmed", "import_domestic_ambiguous",
        "model_not_in_catalog", "outside_current_generation", "other",
    }
    skip_ids = set()
    skip_relations = set()
    subject_relations = set()
    for index, item in enumerate(catalog["skips"] if isinstance(catalog["skips"], list) else []):
        path = f"skips[{index}]"
        if not _exact_keys(item, skip_keys, path, errors):
            continue
        sid = item["skip_id"]
        _valid_id(sid, f"{path}.skip_id", errors)
        if sid in skip_ids:
            errors.append(f"{path}.skip_id: duplicate {sid!r}")
        skip_ids.add(sid)
        if item["event_id"] not in events:
            errors.append(f"{path}.event_id: unknown event")
        investigation = investigations.get(item["investigation_id"])
        if not investigation or investigation.get("status") != "verified_notice" or notices.get(investigation.get("notice_id"), {}).get("event_id") != item["event_id"]:
            errors.append(f"{path}.investigation_id: not a verified investigation for event")
        if item["reason_code"] not in allowed_skip_codes:
            errors.append(f"{path}.reason_code: invalid or deprecated value")
        _string(item["reason"], f"{path}.reason", errors)
        _string(item["evidence_excerpt"], f"{path}.evidence_excerpt", errors, maximum=4000)
        brand, model, subject = item["candidate_brand"], item["candidate_model"], item["decision_subject"]
        if model and not brand:
            errors.append(f"{path}: model cannot exist without brand")
        if brand and model:
            if vehicle_keys is not None and (brand, model) not in vehicle_keys:
                errors.append(f"{path}: explicit skip target vehicle does not exist uniquely")
            if subject:
                errors.append(f"{path}.decision_subject: must be empty for explicit target")
            relation = (item["event_id"], brand, model)
            if relation in skip_relations:
                errors.append(f"{path}: duplicate explicit skip relation")
            skip_relations.add(relation)
            if relation in match_relations:
                errors.append(f"{path}: target conflicts with match")
        else:
            if model or not subject:
                errors.append(f"{path}: incomplete target requires non-empty decision_subject")
            relation = (item["event_id"], item["investigation_id"], item["reason_code"], normalize_text(subject))
            if relation in subject_relations:
                errors.append(f"{path}: duplicate decision_subject relation")
            subject_relations.add(relation)
        if item["reason_code"] == "outside_current_generation" and not (brand and model and item["evidence_excerpt"]):
            errors.append(f"{path}: outside_current_generation requires explicit target and evidence")

    decided_events = {row[0] for row in match_relations} | {item["event_id"] for item in catalog["skips"] if isinstance(item, dict) and "event_id" in item}
    for eid in events:
        if eid not in decided_events:
            errors.append(f"event {eid}: requires a match or skip decision")

    snapshot_candidates = {}
    snapshot_urls = {}
    snapshot_investigations = set()
    snapshot_notices = set()
    snapshot_events = set()
    matched_events = {row[0] for row in match_relations}
    skipped_events = {item["event_id"] for item in catalog["skips"] if isinstance(item, dict) and "event_id" in item}
    for source_id, candidates in snapshot_candidates_by_source.items():
        for candidate_id, candidate in candidates.items():
            candidate_path = f"candidate snapshot {source_id}/{candidate_id}"
            if candidate_id in snapshot_candidates:
                errors.append(f"{candidate_path}: duplicate candidate ID across snapshots")
            snapshot_candidates[candidate_id] = candidate
            try:
                normalized_url = normalize_url(candidate["official_url"])
            except (TypeError, ValueError):
                errors.append(f"{candidate_path}.official_url: cannot normalize invalid URL")
                continue
            if normalized_url in snapshot_urls:
                errors.append(f"{candidate_path}: duplicate candidate URL across snapshots")
            snapshot_urls[normalized_url] = candidate_id
            investigation_id = candidate["investigation_id"]
            if investigation_id in snapshot_investigations:
                errors.append(f"{candidate_path}.investigation_id: candidate handling is not one-to-one")
            snapshot_investigations.add(investigation_id)
            investigation = investigations.get(investigation_id)
            if not investigation:
                errors.append(f"{candidate_path}.investigation_id: missing terminal investigation")
                continue
            if candidate["decision"] == "excluded":
                if investigation["status"] == "verified_notice" or investigation["notice_id"]:
                    errors.append(f"{candidate_path}: excluded candidate must not create a notice")
                continue
            if investigation["status"] != "verified_notice" or not investigation["notice_id"]:
                errors.append(f"{candidate_path}: included candidate must have a verified notice")
                continue
            notice = notices.get(investigation["notice_id"])
            if not notice:
                errors.append(f"{candidate_path}: investigation references an unknown notice")
                continue
            snapshot_notices.add(notice["notice_id"])
            if notice["source_id"] != source_id or normalize_url(notice["official_url"]) != normalized_url:
                errors.append(f"{candidate_path}: notice does not exactly close candidate official source and URL")
            event_id = notice["event_id"]
            snapshot_events.add(event_id)
            if candidate["decision"] == "included_matched" and event_id not in matched_events:
                errors.append(f"{candidate_path}: included_matched candidate has no catalog match")
            if candidate["decision"] == "included_unmatched":
                if event_id in matched_events or event_id not in skipped_events:
                    errors.append(f"{candidate_path}: included_unmatched candidate requires skips and no match")
    if set(investigations) != snapshot_investigations:
        errors.append("investigations: catalog investigation is outside candidate snapshots")
    if set(notices) != snapshot_notices:
        errors.append("notices: catalog notice is outside candidate snapshots")
    if set(events) != snapshot_events:
        errors.append("events: catalog event is outside candidate snapshots")
    return errors


def validate_catalog_targets(catalog, vehicle_keys, *, brands=None):
    """Validate only explicit catalog targets in the caller's vehicle scope."""
    errors = validate_catalog(catalog)
    if errors:
        return errors
    scoped = explicit_target_keys(catalog, brands=brands)
    for target in sorted(scoped):
        if target not in vehicle_keys:
            errors.append(f"catalog target vehicle does not exist uniquely: {target}")
    return errors


def explicit_target_keys(catalog, *, brands=None):
    """Return all match and explicit skip targets, optionally limited by display brand."""
    result = set()
    for match in catalog["matches"]:
        target = (match["brand"], match["model"])
        if brands is None or target[0] in brands:
            result.add(target)
    for skip in catalog["skips"]:
        target = (skip["candidate_brand"], skip["candidate_model"])
        if target[0] and target[1] and (brands is None or target[0] in brands):
            result.add(target)
    return result


def require_valid_catalog(catalog, *, vehicle_keys=None):
    errors = validate_catalog(catalog, vehicle_keys=vehicle_keys)
    if errors:
        raise RecallDataError(errors)


def expected_items(catalog):
    require_valid_catalog(catalog)
    notices = {item["notice_id"]: item for item in catalog["notices"]}
    events = {item["event_id"]: item for item in catalog["events"]}
    result = {}
    seen = set()
    for match in catalog["matches"]:
        relation = (match["event_id"], match["brand"], match["model"])
        if relation in seen:
            raise RecallDataError([f"matches: duplicate relation {relation}"])
        seen.add(relation)
        event = events[match["event_id"]]
        notice = notices[event["primary_notice_id"]]
        item = {key: notice[key] for key in DISPLAY_KEYS if key not in {"event_id", "notice_id"}}
        item["event_id"] = event["event_id"]
        item["notice_id"] = notice["notice_id"]
        result.setdefault((match["brand"], match["model"]), []).append(item)
    return result


def serialize_items(items):
    if not items:
        return ""
    ordered = sorted(
        items,
        key=lambda item: (
            item["recall_start_date"] or item["announcement_date"],
            item["event_id"],
            item["notice_id"],
        ),
        reverse=True,
    )
    for item in ordered:
        for key in DISPLAY_KEYS:
            if not isinstance(item.get(key), str) or len(item[key]) > 2048:
                raise RecallDataError([f"display item {item.get('event_id', '?')}.{key}: must be string up to 2048 characters"])
    return json.dumps(ordered, ensure_ascii=False, separators=(",", ":"), sort_keys=True, allow_nan=False)


def validate_vehicle_records(records, *, allow_legacy=False):
    errors = []
    expected = set(schema.FIELDS)
    legacy = expected - {"recall_history"}
    seen = set()
    if not isinstance(records, list):
        return ["records: expected array"]
    numeric = {"range_cltc_km", "battery_kwh", "ac_charge_kw"}
    prices = {"price_min", "price_max"}
    for index, record in enumerate(records):
        path = f"records[{index}]"
        if not isinstance(record, dict):
            errors.append(f"{path}: expected object")
            continue
        keys = set(record)
        if keys != expected and not (allow_legacy and keys == legacy):
            errors.append(f"{path}: field set does not match schema")
            continue
        key = (record["brand"], record["model"])
        if key in seen:
            errors.append(f"{path}: duplicate vehicle key {key}")
        seen.add(key)
        for field, value in record.items():
            if field in prices:
                if value != "" and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
                    errors.append(f"{path}.{field}: expected non-negative integer or empty string")
            elif field in numeric:
                numeric_string = isinstance(value, str) and value != "" and re.fullmatch(r"\d+(?:\.\d+)?", value)
                if value != "" and not numeric_string and (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0):
                    errors.append(f"{path}.{field}: expected finite non-negative number, numeric string, or empty string")
            elif not isinstance(value, str):
                errors.append(f"{path}.{field}: expected string")
    return errors


def apply_recall_history(records, catalog=None):
    catalog = load_catalog() if catalog is None else catalog
    require_valid_catalog(catalog)
    errors = validate_vehicle_records(records, allow_legacy=True)
    if errors:
        raise RecallDataError(errors)
    projection = expected_items(catalog)
    output = deepcopy(records)
    for record in output:
        record["recall_history"] = serialize_items(projection.get((record["brand"], record["model"]), []))
    return output


def atomic_write_many(payloads):
    """Replace same-filesystem targets with rollback to exact previous bytes."""
    snapshots = {}
    temporary = {}
    replaced = []
    try:
        for target in payloads:
            snapshots[target] = (target.exists(), target.read_bytes() if target.exists() else b"")
        for target, content in payloads.items():
            target.parent.mkdir(parents=True, exist_ok=True)
            fd, name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
            temp = Path(name)
            temporary[target] = temp
            with os.fdopen(fd, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
        for target, temp in temporary.items():
            os.replace(temp, target)
            replaced.append(target)
        for directory in {target.parent for target in payloads}:
            fd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
    except Exception as original:
        rollback_errors = []
        for target in reversed(replaced):
            existed, content = snapshots[target]
            try:
                if existed:
                    fd, name = tempfile.mkstemp(prefix=f".{target.name}.rollback.", dir=target.parent)
                    with os.fdopen(fd, "wb") as handle:
                        handle.write(content)
                        handle.flush()
                        os.fsync(handle.fileno())
                    os.replace(name, target)
                elif target.exists():
                    target.unlink()
            except Exception as exc:
                rollback_errors.append(f"{target}: {exc}")
        for directory in {target.parent for target in replaced}:
            try:
                fd = os.open(directory, os.O_RDONLY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
            except Exception as exc:
                rollback_errors.append(f"{directory} rollback fsync: {exc}")
        detail = f"atomic write failed: {original}"
        if rollback_errors:
            detail += "; disk state unknown; rollback failures: " + "; ".join(rollback_errors)
        raise RecallDataError([detail]) from original
    finally:
        for temp in temporary.values():
            if temp.exists():
                temp.unlink()
