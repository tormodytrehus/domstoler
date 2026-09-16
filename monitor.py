#!/usr/bin/env python3
"""Monitor public Norwegian court schedules and emit a privacy-conscious RSS feed."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from email.utils import format_datetime
from html import escape
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qs, urlencode, urljoin, urlparse
from xml.etree import ElementTree as ET
from zoneinfo import ZoneInfo


BASE_URL = "https://www.domstol.no/no/nar-gar-rettssaken/"
STATE_VERSION = 1


def compact(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def canonical_case_id(case_url: str, case_number: str, court: str) -> str:
    parsed = urlparse(case_url)
    query = {key.lower(): value for key, value in parse_qs(parsed.query).items()}
    for key in ("saksid", "caseid"):
        if query.get(key):
            return f"{court}:{query[key][0]}"
    return f"{court}:{case_number}"


@dataclass
class CourtCase:
    case_id: str
    case_number: str
    court: str
    subject: str
    parties: str
    url: str
    hearings: list[str] = field(default_factory=list)

    @property
    def searchable_text(self) -> str:
        return compact(" ".join((self.case_number, self.court, self.subject, self.parties)))

    @property
    def fingerprint(self) -> str:
        payload = json.dumps(
            {
                "case_number": self.case_number,
                "court": self.court,
                "subject": self.subject,
                "parties": self.parties,
                "url": self.url,
                "hearings": sorted(set(self.hearings)),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def safe_summary(self, matched_terms: list[str]) -> dict[str, Any]:
        # Deliberately omit the unfiltered parties field from persisted state/RSS.
        return {
            "case_id": self.case_id,
            "case_number": self.case_number,
            "court": self.court,
            "subject": self.subject,
            "url": self.url,
            "hearings": sorted(set(self.hearings)),
            "matched_terms": matched_terms,
        }


def merge_rows(rows: Iterable[dict[str, str]]) -> dict[str, CourtCase]:
    cases: dict[str, CourtCase] = {}
    for raw in rows:
        case_number = compact(raw.get("case_number", ""))
        court = compact(raw.get("court", ""))
        case_url = compact(raw.get("url", ""))
        case_id = canonical_case_id(case_url, case_number, court)
        hearing = compact(raw.get("hearing", ""))
        if case_id not in cases:
            cases[case_id] = CourtCase(
                case_id=case_id,
                case_number=case_number,
                court=court,
                subject=compact(raw.get("subject", "")),
                parties=compact(raw.get("parties", "")),
                url=case_url,
                hearings=[],
            )
        case = cases[case_id]
        if hearing and hearing not in case.hearings:
            case.hearings.append(hearing)
        # Preserve the most informative text if repeated rows differ.
        if len(compact(raw.get("subject", ""))) > len(case.subject):
            case.subject = compact(raw.get("subject", ""))
        if len(compact(raw.get("parties", ""))) > len(case.parties):
            case.parties = compact(raw.get("parties", ""))
    return cases


def matching_terms(case: CourtCase, terms: Iterable[str]) -> list[str]:
    haystack = case.searchable_text.casefold()
    matches = []

    for term in terms:
        pattern = rf"(?<!\w){re.escape(term.casefold())}(?!\w)"
        if re.search(pattern, haystack):
            matches.append(term)

    return matches


def _future_hearing(case_summary: dict[str, Any], today: date) -> bool:
    for value in case_summary.get("hearings", []):
        match = re.search(r"(\d{2})\.(\d{2})\.(\d{4})", value)
        if match:
            hearing_date = date(int(match.group(3)), int(match.group(2)), int(match.group(1)))
            if hearing_date >= today:
                return True
    return False


def compare_snapshots(
    current: dict[str, CourtCase],
    previous_state: dict[str, Any],
    config: dict[str, Any],
    now: datetime,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    terms = config["matching"]["terms"]
    monitor = config["monitor"]
    previous = previous_state.get("cases", {})
    first_run = not previous_state.get("initialized", False)
    new_index: dict[str, Any] = {}
    notifications: list[dict[str, Any]] = []

    for case_id, case in current.items():
        matches = matching_terms(case, terms)
        old = previous.get(case_id)
        safe = case.safe_summary(matches)
        new_index[case_id] = {
            "fingerprint": case.fingerprint,
            "interesting": bool(matches),
            "missing_count": 0,
            "summary": safe if matches else None,
        }
        if not matches:
            continue
        if first_run and monitor.get("silent_first_run", True):
            continue
        if old is None:
            notifications.append(make_event("new", safe, now))
        elif old.get("fingerprint") != case.fingerprint:
            notifications.append(make_event("changed", safe, now))

    missing_threshold = int(monitor.get("missing_runs_before_alert", 2))
    for case_id, old in previous.items():
        if case_id in current:
            continue
        missing_count = int(old.get("missing_count", 0)) + 1
        if missing_count < missing_threshold:
            retained = dict(old)
            retained["missing_count"] = missing_count
            new_index[case_id] = retained
            continue
        summary = old.get("summary")
        if old.get("interesting") and summary and _future_hearing(summary, now.date()):
            notifications.append(make_event("removed", summary, now))

    existing_events = previous_state.get("events", [])
    events = (notifications + existing_events)[: int(monitor.get("max_feed_items", 200))]
    state = {
        "version": STATE_VERSION,
        "initialized": True,
        "cases": new_index,
        "events": events,
    }
    return state, notifications


def make_event(kind: str, summary: dict[str, Any], now: datetime) -> dict[str, Any]:
    payload = json.dumps(summary, ensure_ascii=False, sort_keys=True)
    event_hash = hashlib.sha256(f"{kind}|{payload}|{now.isoformat()}".encode()).hexdigest()[:20]
    return {
        "id": event_hash,
        "kind": kind,
        "observed_at": now.astimezone(timezone.utc).isoformat(),
        **summary,
    }


def event_title(event: dict[str, Any]) -> str:
    prefix = {"new": "Ny", "changed": "Endret", "removed": "Forsvunnet"}.get(
        event.get("kind"), "Oppdatert"
    )
    subject = event.get("subject") or "Uspesifisert sak"
    return f"{prefix} beramming: {subject}"


def build_rss(events: list[dict[str, Any]], feed_config: dict[str, str]) -> bytes:
    rss = ET.Element("rss", {"version": "2.0"})
    channel = ET.SubElement(rss, "channel")
    ET.SubElement(channel, "title").text = feed_config["title"]
    ET.SubElement(channel, "link").text = feed_config["link"]
    ET.SubElement(channel, "description").text = feed_config["description"]
    ET.SubElement(channel, "language").text = "nb-NO"

    for event in events:
        item = ET.SubElement(channel, "item")
        ET.SubElement(item, "title").text = event_title(event)
        ET.SubElement(item, "link").text = event.get("url", feed_config["link"])
        guid = ET.SubElement(item, "guid", {"isPermaLink": "false"})
        guid.text = event["id"]
        observed = datetime.fromisoformat(event["observed_at"])
        ET.SubElement(item, "pubDate").text = format_datetime(observed)
        hearings = ", ".join(event.get("hearings", [])) or "Ikke oppgitt"
        matches = ", ".join(event.get("matched_terms", [])) or "Ikke oppgitt"
        description = (
            f"Domstol: {escape(event.get('court', ''))}<br>"
            f"Saksnummer: {escape(event.get('case_number', ''))}<br>"
            f"Rettsmøte: {escape(hearings)}<br>"
            f"Treff: {escape(matches)}"
        )
        ET.SubElement(item, "description").text = description
    ET.indent(rss)
    return ET.tostring(rss, encoding="utf-8", xml_declaration=True)


async def resolve_court_ids(page: Any, court_names: list[str]) -> dict[str, str]:
    """Resolve court IDs from the same public endpoint used by the search form."""
    endpoint = urljoin(BASE_URL, "/api/episerver/v3/beramming/GetCourts/no")
    response = await page.request.get(endpoint)
    if not response.ok:
        raise RuntimeError(f"Kunne ikke hente domstollisten (HTTP {response.status})")

    available = {
        compact(item.get("Text", "")).casefold(): compact(item.get("Value", ""))
        for item in await response.json()
        if item.get("Text") and item.get("Value")
    }
    missing = [name for name in court_names if name.casefold() not in available]
    if missing:
        raise RuntimeError(f"Fant ikke domstolvalg: {', '.join(missing)}")
    return {name: available[name.casefold()] for name in court_names}


async def extract_rows(page: Any) -> list[dict[str, str]]:
    table = page.locator("table").filter(has_text="Saksnr").first
    if await table.count() == 0:
        return []
    headers = [compact(value).casefold() for value in await table.locator("thead th").all_inner_texts()]
    rows: list[dict[str, str]] = []
    for tr in await table.locator("tbody tr").all():
        cells = [compact(value) for value in await tr.locator("td").all_inner_texts()]
        if not cells:
            continue
        mapped = dict(zip(headers, cells))
        link = tr.locator("a").first
        href = await link.get_attribute("href") if await link.count() else ""
        rows.append(
            {
                "hearing": mapped.get("rettsmøte", cells[0] if cells else ""),
                "case_number": mapped.get("saksnr", cells[1] if len(cells) > 1 else ""),
                "court": mapped.get("domstol", ""),
                "subject": mapped.get("saken gjelder", ""),
                "parties": mapped.get("parter", ""),
                "url": urljoin(BASE_URL, href or ""),
            }
        )
    return rows


async def scrape(config: dict[str, Any]) -> dict[str, CourtCase]:
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise RuntimeError("Installer avhengigheter med: pip install -r requirements.txt") from exc

    today = datetime.now(ZoneInfo("Europe/Oslo")).date()
    start = today - timedelta(days=int(config["date_window"]["days_back"]))
    end = today + timedelta(days=int(config["date_window"]["days_forward"]))
    all_rows: list[dict[str, str]] = []

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page(locale="nb-NO")
        court_ids = await resolve_court_ids(page, config["courts"])
        for court_name, court_id in court_ids.items():
            court_row_start = len(all_rows)
            params = {
                "fraDato": start.isoformat(),
                "tilDato": end.isoformat(),
                "domstolid": court_id,
                "sortTerm": "rettsmoete",
                "sortAscending": "true",
                "pageSize": str(config.get("page_size", 100)),
            }
            await page.goto(f"{BASE_URL}?{urlencode(params)}", wait_until="domcontentloaded")
            await page.get_by_text(re.compile(r"Antall treff", re.I)).first.wait_for(timeout=30_000)
            seen_page_signatures: set[str] = set()
            for _ in range(200):
                rows = await extract_rows(page)
                signature = hashlib.sha256(
                    json.dumps(rows, ensure_ascii=False, sort_keys=True).encode()
                ).hexdigest()
                if signature in seen_page_signatures:
                    break
                seen_page_signatures.add(signature)
                all_rows.extend(rows)

                next_control = page.get_by_role("link", name=re.compile(r"^Neste", re.I)).first
                if await next_control.count() == 0:
                    next_control = page.get_by_role("button", name=re.compile(r"^Neste", re.I)).first
                if await next_control.count() == 0 or await next_control.is_disabled():
                    break
                before = page.url
                await next_control.click()
                await page.wait_for_load_state("networkidle")
                if page.url == before:
                    await page.wait_for_timeout(750)
            print(f"{court_name}: {len(all_rows) - court_row_start} rader")
        await browser.close()
    if not all_rows:
        raise RuntimeError("Ingen beramminger ble hentet; tilstanden er ikke overskrevet")
    return merge_rows(all_rows)


def load_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(data)
    temporary.replace(path)


async def async_main(args: argparse.Namespace) -> int:
    config_path = Path(args.config)
    config = load_json(config_path, {})
    state_path = Path(args.state)
    feed_path = Path(args.feed)
    previous = load_json(state_path, {"version": STATE_VERSION, "initialized": False})
    current = await scrape(config)
    now = datetime.now(timezone.utc)
    state, notifications = compare_snapshots(current, previous, config, now)
    atomic_write(state_path, json.dumps(state, ensure_ascii=False, indent=2).encode("utf-8") + b"\n")
    atomic_write(feed_path, build_rss(state["events"], config["feed"]))
    print(f"Totalt {len(current)} saker; {len(notifications)} nye varsler")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--state", default="state/cases.json")
    parser.add_argument("--feed", default="public/feed.xml")
    return parser.parse_args()


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(async_main(parse_args())))
    except Exception as exc:
        print(f"Feil: {exc}", file=sys.stderr)
        raise
