"""Offline parser for the current ProCyclingStats rider profile results table.

This module only parses HTML supplied by the caller. It performs no network
requests and does not write to the Activity Monitor database.
"""
from __future__ import annotations

import re
import hashlib
import json
import unicodedata
from datetime import date, datetime
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit


PCS_ORIGIN = "https://www.procyclingstats.com"
RESULTS_TABLE_CLASS = "rdrResults"
_DATE = re.compile(r"^(\d{1,2})\.(\d{1,2})(?:\.(\d{2,4}))?$")
_DATE_RANGE = re.compile(r"^\s*\d{1,2}\.\d{1,2}(?:\.\d{2,4})?\s*[–—-]\s*\d{1,2}\.\d{1,2}(?:\.\d{2,4})?\s*$")
_POSITION = re.compile(r"^\d{1,4}$")
_PARTICIPATION_STATUSES = {"DNF", "OOT", "OTL", "DSQ"}
_NON_PARTICIPATION_STATUSES = {"AB", "HD"}
_CATEGORY = re.compile(r"\s*\((NAT|NC|C[1-4]|CX|\d(?:\.\d)?(?:\.\d)?|\d\.[A-Z]{2,4})\)\s*$", re.I)
_YEAR = re.compile(r"(?:^|[-_/])(20\d{2})(?=[-_/]|$)")
_STAGE = re.compile(r"(?:^|[-_/])stage-(\d+)(?:[-_/]|$)", re.I)


def _normal(value: str) -> str:
    value = unicodedata.normalize("NFKD", value or "")
    value = "".join(c for c in value if not unicodedata.combining(c))
    return " ".join(re.findall(r"[a-z0-9]+", value.casefold()))


def _country_key(value: str) -> str:
    key = _normal(value)
    aliases = {
        "czech republic": "czechia",
        "usa": "united states",
        "united states of america": "united states",
        # WorldDB uses the ISO-style country name while PCS uses its cycling
        # federation label for Great Britain.
        "united kingdom": "great britain",
        "uk": "great britain",
        "turkey": "turkiye",
    }
    return aliases.get(key, key)


def _ordered_tokens_match(expected: list[str], actual: list[str]) -> bool:
    """Require the same given name and all expected name parts in order.

    PCS sometimes shows a longer official surname (for example, an additional
    family name) or includes a middle name omitted by the WorldDB roster.
    This accepts those additions while still rejecting a different given or
    family name.
    """
    if not expected or not actual or expected[0] != actual[0]:
        return False
    actual_index = 0
    for token in expected:
        try:
            actual_index = actual.index(token, actual_index) + 1
        except ValueError:
            return False
    return True


def _slug_compatible(slug: str, expected: list[str], actual: list[str]) -> bool:
    tokens = _normal(slug).split()
    if not tokens:
        return False
    return any(tokens == candidate[:len(tokens)] or candidate == tokens[:len(candidate)]
               for candidate in (expected, actual) if candidate)


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", _normal(value)).strip("-")


class _Page(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title_parts: list[str] = []
        self.h1_parts: list[str] = []
        self.visible_parts: list[str] = []
        self.canonical: str = ""
        self.in_title = self.in_h1 = False
        self.skip = 0
        self.tables: list[dict] = []
        self.total_tables = 0
        self.table: dict | None = None
        self.row: dict | None = None
        self.cell: dict | None = None
        self.active_anchor: dict | None = None

    def handle_starttag(self, tag, attrs_list):
        attrs = dict(attrs_list)
        classes = set((attrs.get("class") or "").split())
        if tag in {"script", "style"}:
            self.skip += 1
        if tag == "title": self.in_title = True
        if tag == "h1": self.in_h1 = True
        if tag == "link" and "canonical" in (attrs.get("rel") or "").split():
            self.canonical = attrs.get("href") or ""
        if tag == "table":
            self.total_tables += 1
            if RESULTS_TABLE_CLASS in classes and self.table is None:
                self.table = {"attrs": attrs, "rows": []}
        elif tag == "tr" and self.table is not None:
            self.row = {"attrs": attrs, "cells": []}
        elif tag in {"td", "th"} and self.row is not None:
            self.cell = {"tag": tag, "text": [], "links": []}
        elif tag == "a" and self.cell is not None:
            self.active_anchor = {"href": attrs.get("href") or "", "text": []}
            self.cell["links"].append(self.active_anchor)

    def handle_endtag(self, tag):
        if tag == "title": self.in_title = False
        if tag == "h1": self.in_h1 = False
        if tag == "a": self.active_anchor = None
        if tag in {"td", "th"} and self.cell is not None and self.row is not None:
            self.cell["text"] = " ".join(" ".join(self.cell["text"]).split())
            for link in self.cell["links"]:
                link["text"] = " ".join(" ".join(link["text"]).split())
            self.row["cells"].append(self.cell)
            self.cell = None
        elif tag == "tr" and self.row is not None and self.table is not None:
            self.table["rows"].append(self.row)
            self.row = None
        elif tag == "table" and self.table is not None:
            self.tables.append(self.table)
            self.table = None
        if tag in {"script", "style"} and self.skip:
            self.skip -= 1

    def handle_data(self, data):
        if self.in_title: self.title_parts.append(data)
        if self.in_h1: self.h1_parts.append(data)
        if not self.skip: self.visible_parts.append(data)
        if self.cell is not None: self.cell["text"].append(data)
        if self.active_anchor is not None: self.active_anchor["text"].append(data)


def _date_from_cell(raw: str, year: int | None) -> date | None:
    match = _DATE.fullmatch(raw.strip())
    if not match or year is None:
        return None
    day, month, explicit_year = match.groups()
    parsed_year = int(explicit_year) if explicit_year else year
    if explicit_year and len(explicit_year) == 2:
        parsed_year += 2000 if parsed_year < 70 else 1900
    try:
        return date(parsed_year, int(month), int(day))
    except ValueError:
        return None


def _race_parts(text: str, href: str) -> tuple[str, str]:
    text = " ".join(text.split())
    category = ""
    match = _CATEGORY.search(text)
    if match:
        category = match.group(1).upper()
        text = text[:match.start()].strip()
    return text, category


def _race_group(href: str) -> tuple[str, int | None, int | None]:
    path = urlsplit(urljoin(PCS_ORIGIN + "/", href)).path
    slug = path.rsplit("/", 1)[-1]
    year_match = _YEAR.search(slug)
    year = int(year_match.group(1)) if year_match else None
    stage_match = _STAGE.search(slug)
    stage = int(stage_match.group(1)) if stage_match else None
    group = re.sub(r"-stage-\d+(?=-|$)", "", slug, flags=re.I)
    group = re.sub(r"-gc(?=-|$)", "", group, flags=re.I)
    group = re.sub(r"-result$", "", group, flags=re.I)
    return group, year, stage


def _profile_facts(page: _Page) -> dict:
    title = " ".join(" ".join(page.title_parts).split())
    heading = " ".join(" ".join(page.h1_parts).split())
    visible = " ".join(" ".join(page.visible_parts).split())
    # Some profiles insert an alternate-script name (e.g. Tigrinya) between
    # the displayed name and birth date. Stop at the next known info label.
    name_match = re.search(
        r"\bName:\s*(.+?)(?=\s+(?:Tigrinya|Date of birth|Nationality|Weight|Height|Place of birth):|$)",
        visible, re.I)
    displayed_name = name_match.group(1).strip() if name_match else heading or title
    dob_match = re.search(
        r"Date of birth:\s*(?:"
        r"(\d{4}-\d{2}-\d{2})|"
        r"(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]+)\s+(\d{4})"
        r")", visible, re.I)
    dob = ""
    if dob_match:
        if dob_match.group(1):
            try:
                dob = date.fromisoformat(dob_match.group(1)).isoformat()
            except ValueError:
                pass
        else:
            try:
                dob = datetime.strptime(" ".join(dob_match.groups()[1:]), "%d %B %Y").date().isoformat()
            except ValueError:
                pass
    nation_match = re.search(r"\bNationality:\s*(.+?)\s+(?:Weight:|Height:|Place of birth:)", visible, re.I)
    nationality = " ".join(nation_match.group(1).split()) if nation_match else ""
    canonical = urljoin(PCS_ORIGIN + "/", page.canonical) if page.canonical else ""
    canonical_slug = ""
    segments = [part for part in urlsplit(canonical).path.split("/") if part]
    if len(segments) >= 2 and segments[0] == "rider":
        canonical_slug = segments[1]
    return {"title": title, "heading": heading, "display_name": displayed_name,
            "birthdate": dob, "nationality": nationality,
            "canonical_url": canonical, "canonical_slug": canonical_slug}


def verify_identity(page_facts: dict, profile_url: str, expected: dict, *, confirmed_rows=(), as_of=None) -> dict:
    """Verify identity only when the WorldDB and PCS birthdates match exactly.

    Nationality and race participation are reported as context, but never used
    to turn a missing birthdate into an identity match.
    """
    expected_dob = str(expected.get("birthdate") or "")[:10]
    page_dob = str(page_facts.get("birthdate") or "")[:10]
    dob_ok = (expected_dob == page_dob) if expected_dob and page_dob else None
    reasons = []
    if dob_ok is False:
        reasons.append(f"birthdate mismatch: WorldDB '{expected_dob}', PCS '{page_dob}'")
    elif dob_ok is None:
        reasons.append("PCS date of birth missing" if expected_dob else "WorldDB date of birth missing")
    current_day = as_of or date.today()
    nation = _country_key(page_facts.get('nationality') or '')
    expected_nation = _country_key(expected.get('nationality') or '')
    nationality_ok = bool(nation and expected_nation and nation == expected_nation)
    current_races = any(row.get('confirmed_participation')
                        and str(row.get('date') or '')[:4] == str(current_day.year)
                        and str(row.get('date')) <= current_day.isoformat()
                        for row in confirmed_rows)
    status = "verified" if dob_ok is True else ("mismatch" if dob_ok is False else "ambiguous")
    return {"identity_status": status, "identity_matches": dob_ok is True,
            "identity_checks": {"birthdate": dob_ok, "nationality": nationality_ok, "current_year_participation": current_races},
            "identity_reasons": reasons}


def _is_profile_not_found(page: _Page, facts: dict) -> bool:
    """Recognize PCS's HTTP-200 not-found document, not a rider identity conflict."""
    labels = [_normal(" ".join(page.h1_parts)), _normal(" ".join(page.title_parts))]
    page_not_found = any(label in {"page not found", "404 not found", "not found"}
                         for label in labels)
    has_profile_identity = bool(facts.get("birthdate") or facts.get("nationality"))
    return page_not_found and not facts.get("canonical_slug") and not has_profile_identity


def parse_pcs_html(html: str | bytes, *, rider_id: int, profile_url: str,
                   expected_identity: dict, year_override: int | None = None, as_of=None) -> dict:
    """Parse PCS ``rdrResults`` rows, preserving non-participation rows for audit.

    A missing/unrecognized results table is returned as an explicit status and
    never means the rider had no racing activity.
    """
    if isinstance(html, bytes):
        try:
            html = html.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            html = html.decode("cp1252", errors="replace")
    page = _Page()
    page.feed(html)
    facts = _profile_facts(page)
    if _is_profile_not_found(page, facts):
        identity = {
            "identity_status": "unavailable", "identity_matches": False,
            "identity_checks": {"birthdate": None},
            "identity_reasons": ["PCS returned a page-not-found response; no rider profile was present."],
        }
        return {"status": "profile_not_found", "identity": {**facts, **identity},
                "rows": [], "latest_participation": None,
                "table_count": page.total_tables}
    identity = verify_identity(facts, profile_url, expected_identity)
    result_table = next((table for table in page.tables
                         if table["rows"] and any(
                             any(cell["tag"] == "th" and cell["text"].casefold() == "date"
                                 for cell in row["cells"]) for row in table["rows"])
                         and any("race" in " ".join(c["text"].casefold() for c in row["cells"])
                                 for row in table["rows"][:2])), None)
    if result_table is None:
        return {"status": "missing_results_table", "identity": {**facts, **identity},
                "rows": [], "latest_participation": None, "table_count": page.total_tables}

    header_row = next(row for row in result_table["rows"]
                      if any(c["tag"] == "th" and c["text"].casefold() == "date" for c in row["cells"]))
    headers = [cell["text"].casefold() for cell in header_row["cells"]]
    date_idx = next(i for i, h in enumerate(headers) if h == "date")
    result_idx = next((i for i, h in enumerate(headers) if "result" in h), 1)
    race_idx = next(i for i, h in enumerate(headers) if "race" in h)
    parsed_rows: list[dict] = []
    excluded_future_rows = 0
    effective_as_of = as_of or date.today()
    context_name = ""
    context_category = ""
    context_group = ""
    for source_row in result_table["rows"]:
        if source_row is header_row:
            continue
        cells = source_row["cells"]
        if max(date_idx, result_idx, race_idx) >= len(cells):
            continue
        date_text = cells[date_idx]["text"].strip()
        result_text = cells[result_idx]["text"].strip()
        race_cell = cells[race_idx]
        anchor = next((link for link in race_cell["links"]
                       if urlsplit(urljoin(PCS_ORIGIN + "/", link["href"])).path.startswith("/race/")), None)
        if anchor is None:
            continue
        race_href = anchor["href"]
        race_text = anchor["text"] or race_cell["text"]
        own_name, own_category = _race_parts(race_text, race_href)
        group, link_year, stage_from_link = _race_group(race_href)
        year = link_year or year_override
        range_heading = bool(_DATE_RANGE.fullmatch(date_text))
        is_classification = "-gc" in urlsplit(urljoin(PCS_ORIGIN + "/", race_href)).path.casefold() or "general classification" in race_text.casefold()
        row_classes = set((source_row["attrs"].get("class") or "").split())
        stage_number = stage_from_link
        if stage_number is None:
            stage_match = re.search(r"\bStage\s+(\d+)\b", race_text, re.I)
            stage_number = int(stage_match.group(1)) if stage_match else None
        is_stage = stage_number is not None or "stage" in row_classes

        if range_heading or (not result_text and not is_classification):
            context_name = own_name or context_name
            context_category = own_category or context_category
            context_group = group
            row_type = "race_heading"
        elif is_classification:
            row_type = "classification"
        elif is_stage:
            row_type = "stage_result"
        else:
            row_type = "one_day_result"

        date_value = _date_from_cell(date_text, year)
        if date_value:
            race_name = own_name if row_type == "one_day_result" else (context_name or own_name)
            category = own_category or context_category
            group_id = group or context_group
        else:
            race_name = own_name or context_name
            category = own_category or context_category
            group_id = group or context_group

        # A PCS profile may list an announced result beyond the check's date.
        # Keep it out of evidence entirely so it cannot appear as activity.
        if date_value and date_value > effective_as_of:
            excluded_future_rows += 1
            continue

        result_norm = result_text.upper()
        if result_norm == "DNS":
            participation = "dns_only"
        elif result_norm == "DSQ":
            participation = "confirmed_participation"
        elif row_type == "classification":
            participation = "classification_only"
        elif date_value and (_POSITION.fullmatch(result_text) or result_norm in _PARTICIPATION_STATUSES):
            participation = "confirmed_participation"
        elif result_norm in _NON_PARTICIPATION_STATUSES:
            participation = "non_finisher_status_not_counted"
        else:
            participation = "not_confirmed"

        category_norm = category.upper()
        if category_norm == "NAT":
            race_type = "non-UCI"
        elif category_norm == "NC":
            race_type = "national championship; UCI status unknown"
        elif re.fullmatch(r"[12]\.[A-Z0-9]{1,4}", category_norm):
            race_type = "UCI"
        else:
            race_type = "unknown"
        full_url = urljoin(PCS_ORIGIN + "/", race_href)
        stage_name = ""
        if row_type == "stage_result":
            stage_name = re.sub(r"^\s*\d+\s+", "", own_name).strip()
        parsed_rows.append({
            "rider_id": rider_id,
            "year": year,
            "date": date_value.isoformat() if date_value else "",
            "race_name": race_name,
            "race_category": category,
            "race_type": race_type,
            "race_id": group_id or full_url,
            "race_url": full_url,
            "stage_number": stage_number if is_stage else None,
            "stage_name": stage_name,
            "row_type": row_type,
            "result": result_text,
            "participation_status": participation,
            "confirmed_participation": participation == "confirmed_participation",
        })

    identity = verify_identity(facts, profile_url, expected_identity, confirmed_rows=parsed_rows, as_of=as_of)
    confirmed = [row for row in parsed_rows if row["confirmed_participation"]]
    latest = max(confirmed, key=lambda row: row["date"]) if confirmed else None
    data_rows = [row for row in result_table["rows"] if row is not header_row]
    # A recognized header-only table is a valid empty result set. Rows with
    # unknown markup must remain a parsing failure rather than a false zero.
    status = ("results_parsed" if parsed_rows else
              "results_table_empty" if not data_rows or excluded_future_rows else
              "results_table_empty_or_unrecognized")
    if not identity["identity_matches"]:
        latest = None
        status = "identity_not_verified"
    return {"status": status, "identity": {**facts, **identity},
            "rows": parsed_rows, "latest_participation": latest,
            "table_count": page.total_tables, "result_table_rows": len(result_table["rows"]) - 1,
            "excluded_future_rows": excluded_future_rows}


def to_activity_monitor_evidence(parsed: dict, *, rider_id: int,
                                 source_worlddb_rider_id: int | None,
                                 identity_generation: int,
                                 first_cycling_id: int | None,
                                 source_check_id: int | None = None,
                                 imported_at: datetime | None = None) -> list[dict]:
    """Map parser output to ActivityMonitorResult-shaped dictionaries.

    This is a pure conversion helper: it performs no database writes. Rows
    remain available for audit even when profile identity is unverified, while
    ``identity_verified`` is true only for the parser's exact-DOB match.
    """
    identity_verified = bool((parsed.get("identity") or {}).get("identity_matches"))
    converted = []
    for row in parsed.get("rows") or []:
        season = row.get("year")
        if not season:
            # ActivityMonitorResult requires a season. Keep undated/unassigned
            # parser evidence in its original output rather than inventing one.
            continue
        race_id = row.get("race_id")
        try:
            numeric_race_id = int(race_id) if str(race_id).isdigit() else None
        except (TypeError, ValueError):
            numeric_race_id = None
        result_date = row.get("date") or None
        result_identity = (
            str(first_cycling_id)
            if first_cycling_id is not None
            else f"worlddb:{source_worlddb_rider_id}"
            if source_worlddb_rider_id is not None
            else f"monitor:{rider_id}"
        )
        key_fields = {
            "fc_id": first_cycling_id,
            "season": int(season),
            "date": result_date,
            "race_url": row.get("race_url") or "",
            "row_type": row.get("row_type") or "",
            "stage_number": row.get("stage_number"),
            "stage_name": row.get("stage_name") or "",
            "result": row.get("result") or "",
        }
        if first_cycling_id is None:
            key_fields["source_identity"] = result_identity
            key_fields["identity_generation"] = identity_generation
        digest = hashlib.sha256(
            json.dumps(key_fields, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        result_key = (
            f"pcs-profile:{first_cycling_id}:{digest}"
            if first_cycling_id is not None
            else f"pcs-profile:{result_identity}:{identity_generation}:{digest}"
        )
        converted.append({
            "source_result_key": result_key,
            "rider_id": rider_id,
            "source_worlddb_rider_id": source_worlddb_rider_id,
            "identity_generation": identity_generation,
            "first_cycling_id": first_cycling_id,
            "season": int(season),
            "race_id": numeric_race_id,
            "edition_id": None,
            "race_name": row.get("race_name") or "",
            "race_category": row.get("race_category") or "",
            "race_url": row.get("race_url") or "",
            "result_date": date.fromisoformat(result_date) if result_date else None,
            "stage_id": None,
            "stage_number": row.get("stage_number"),
            "stage_name": row.get("stage_name") or "",
            "row_type": row.get("row_type") or "unknown",
            "result_position": row.get("result") or "",
            "gc_position": "",
            "participation_status": row.get("participation_status") or "not_confirmed",
            "source": "pcs",
            "source_check_id": source_check_id,
            "identity_verified": identity_verified,
            "imported_at": imported_at or datetime.utcnow(),
            "source_payload": row,
        })
    return converted
