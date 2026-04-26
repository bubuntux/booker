#!/usr/bin/env python3
"""Calendly automated booking via Playwright."""

import os
import random
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

SCREENSHOT_ROOT = Path(__file__).parent / "screenshots"

from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth


def log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"[{ts}] {msg}", flush=True)


WEEKDAY_NAMES = [
    "monday", "tuesday", "wednesday", "thursday",
    "friday", "saturday", "sunday",
]

# BOOKING_* vars that configure the bot rather than supplying form-field values.
NON_FIELD_BOOKING_VARS = {
    "BOOKING_URL",
    "BOOKING_LOOKAHEAD_DAYS",
    "BOOKING_SKIP_DATES",
}


def collect_booking_fields() -> dict[str, str]:
    """Collect BOOKING_* env vars that map to form fields.

    Skips bot-config vars (BOOKING_URL, BOOKING_PREF_*, BOOKING_LOOKAHEAD_DAYS,
    BOOKING_SKIP_DATES). Suffixes become lowercase label fragments:
        BOOKING_PHONE_NUMBER -> "phone number"
    """
    fields = {}
    for key, value in os.environ.items():
        if not key.startswith("BOOKING_") or not value:
            continue
        if key in NON_FIELD_BOOKING_VARS or key.startswith("BOOKING_PREF_"):
            continue
        label = key[len("BOOKING_"):].lower().replace("_", " ")
        fields[label] = value
    return fields


def collect_time_preferences() -> dict[int, list[str]]:
    """Read BOOKING_PREF_<WEEKDAY> env vars into {weekday_idx: ["HH:MM", ...]}.

    Weekday index follows datetime.date.weekday(): 0 = Monday, 6 = Sunday.
    Times are kept in the order given (= priority order).
    """
    prefs: dict[int, list[str]] = {}
    for idx, name in enumerate(WEEKDAY_NAMES):
        raw = os.environ.get(f"BOOKING_PREF_{name.upper()}", "").strip()
        if not raw:
            continue
        times = [t.strip() for t in raw.split(",") if t.strip()]
        if times:
            prefs[idx] = times
    return prefs


def parse_skip_dates(raw: str) -> set[date]:
    out: set[date] = set()
    for piece in (raw or "").split(","):
        piece = piece.strip()
        if not piece:
            continue
        try:
            out.add(date.fromisoformat(piece))
        except ValueError:
            log(f"WARNING: Ignoring invalid BOOKING_SKIP_DATES entry: {piece!r}")
    return out


def candidate_dates(
    today: date, lookahead_days: int, weekday_idx: int, skip: set[date]
) -> list[date]:
    """Same-weekday dates from today+lookahead going backward to today (inclusive).

    Furthest-out first (matches user's preference: lock the slot in early).
    """
    end = today + timedelta(days=lookahead_days)
    delta = (end.weekday() - weekday_idx) % 7
    cursor = end - timedelta(days=delta)
    out: list[date] = []
    while cursor >= today:
        if cursor not in skip:
            out.append(cursor)
        cursor -= timedelta(days=7)
    return out


def time_to_calendly_strings(hhmm: str) -> list[str]:
    """Map '07:00' / '18:30' to candidate Calendly button texts to match against."""
    h, m = hhmm.split(":")
    h_int = int(h)
    m_int = int(m)
    suffix_lower = "am" if h_int < 12 else "pm"
    suffix_upper = suffix_lower.upper()
    h12 = h_int % 12 or 12
    return [
        f"{h12}:{m_int:02d}{suffix_lower}",      # 7:00am
        f"{h12}:{m_int:02d} {suffix_lower}",     # 7:00 am
        f"{h12}:{m_int:02d}{suffix_upper}",      # 7:00AM
        f"{h12}:{m_int:02d} {suffix_upper}",     # 7:00 AM
        f"{h_int:02d}:{m_int:02d}",              # 07:00 (24h)
    ]


def human_delay(lo: float = 0.3, hi: float = 1.0) -> None:
    time.sleep(random.uniform(lo, hi))


def _normalize(s: str) -> str:
    """Lowercase, drop required-marker `*`, collapse non-alphanumerics to single spaces."""
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def _tokens(s: str) -> set[str]:
    return set(_normalize(s).split())


def _score(field_key: str, label_text: str) -> int:
    """Higher = better match. 0 means no match.

    Substring matches beat token overlap; more shared tokens beats fewer.
    """
    key_norm = _normalize(field_key)
    label_norm = _normalize(label_text)
    if not key_norm or not label_norm:
        return 0
    if key_norm == label_norm:
        return 1000
    if key_norm in label_norm or label_norm in key_norm:
        # Score by length of the contained string so "phone number" beats "phone".
        return 500 + min(len(key_norm), len(label_norm))
    overlap = _tokens(field_key) & _tokens(label_text)
    return len(overlap)


def match_and_fill(page, fields: dict[str, str]) -> list[str]:
    """Find form fields on the page and fill them based on BOOKING_* env vars.

    For each form element, picks the highest-scoring unfilled field, so e.g.
    "Phone Number" gets BOOKING_PHONE_NUMBER even though "House Number and Street"
    also shares the token "number".
    """
    filled: list[str] = []

    form_elements = page.query_selector_all(
        "input:visible, textarea:visible, select:visible"
    )

    for element in form_elements:
        input_type = (element.get_attribute("type") or "").lower()
        if input_type in ("hidden", "submit", "button", "checkbox", "radio"):
            continue

        label_text = _get_label_for_element(page, element)
        if not label_text:
            continue

        # Pick best unfilled field for this label.
        candidates = [
            (_score(k, label_text), k, v)
            for k, v in fields.items()
            if k not in filled
        ]
        candidates.sort(key=lambda c: c[0], reverse=True)
        if not candidates or candidates[0][0] <= 0:
            continue

        _, field_key, field_value = candidates[0]
        log(f"  Filling '{label_text.strip()}' with BOOKING_{field_key.upper().replace(' ', '_')}")
        tag = element.evaluate("el => el.tagName.toLowerCase()")
        if tag == "select":
            element.select_option(label=field_value)
        else:
            element.click()
            human_delay(0.2, 0.5)
            element.fill("")
            page.keyboard.type(field_value, delay=random.randint(30, 120))
        filled.append(field_key)
        human_delay(0.5, 1.2)

    return filled


def _get_label_for_element(page, element) -> str | None:
    """Determine the label text for a form element.

    Tries multiple strategies:
    1. aria-label attribute
    2. placeholder attribute
    3. Associated <label> via 'for' attribute matching element id
    4. Closest parent label
    5. data-component with preceding heading/label sibling
    """
    # aria-label
    aria = element.get_attribute("aria-label")
    if aria and aria.strip():
        return aria.strip()

    # placeholder
    placeholder = element.get_attribute("placeholder")
    if placeholder and placeholder.strip():
        return placeholder.strip()

    # Label via 'for' attribute
    el_id = element.get_attribute("id")
    if el_id:
        label_el = page.query_selector(f'label[for="{el_id}"]')
        if label_el:
            text = label_el.inner_text().strip()
            if text:
                return text

    # Closest parent label
    parent_label = element.evaluate(
        """el => {
            let parent = el.closest('label');
            return parent ? parent.innerText.trim() : null;
        }"""
    )
    if parent_label:
        return parent_label

    # Look for a preceding label-like sibling or parent container text
    preceding_text = element.evaluate(
        """el => {
            // Walk up to find a container with a label/span/p/h* child before the input
            let container = el.parentElement;
            for (let i = 0; i < 3 && container; i++) {
                let children = Array.from(container.children);
                let idx = children.indexOf(el) !== -1 ? children.indexOf(el) : -1;
                // Check previous siblings
                for (let j = children.length - 1; j >= 0; j--) {
                    let sib = children[j];
                    if (sib === el || sib.contains(el)) break;
                    let tag = sib.tagName.toLowerCase();
                    if (['label', 'span', 'p', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'div'].includes(tag)) {
                        let text = sib.innerText.trim();
                        if (text && text.length < 100) return text;
                    }
                }
                container = container.parentElement;
            }
            return null;
        }"""
    )
    if preceding_text:
        return preceding_text

    # name attribute as last resort
    name = element.get_attribute("name")
    if name and name.strip():
        return name.strip().replace("_", " ").replace("-", " ")

    return None


def run() -> int:
    booking_url = os.environ.get("BOOKING_URL")
    if not booking_url:
        log("ERROR: BOOKING_URL environment variable is required")
        return 1

    headless = os.environ.get("HEADLESS", "true").lower() != "false"
    fields = collect_booking_fields()
    prefs = collect_time_preferences()
    skip_dates = parse_skip_dates(os.environ.get("BOOKING_SKIP_DATES", ""))
    try:
        lookahead_days = int(os.environ.get("BOOKING_LOOKAHEAD_DAYS", "60"))
    except ValueError:
        log("WARNING: BOOKING_LOOKAHEAD_DAYS must be an integer; defaulting to 60")
        lookahead_days = 60

    run_dir = SCREENSHOT_ROOT / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir.mkdir(parents=True, exist_ok=True)

    log(f"Booking URL: {booking_url}")
    log(f"Headless: {headless}")
    log(f"Fields to fill: {list(fields.keys())}")
    log(f"Lookahead days: {lookahead_days}")
    log(f"Skip dates: {sorted(skip_dates)}")
    pref_summary = ", ".join(
        f"{WEEKDAY_NAMES[k]}={v}" for k, v in sorted(prefs.items())
    )
    log(f"Time preferences: {pref_summary}")
    log(f"Screenshots: {run_dir}")

    if not fields:
        log("ERROR: No BOOKING_* form field env vars found (need at least BOOKING_NAME, BOOKING_EMAIL, etc.)")
        return 1
    if not prefs:
        log("ERROR: No BOOKING_PREF_<WEEKDAY> env vars set (e.g. BOOKING_PREF_MONDAY=07:00,07:30)")
        return 1
    if date.today().weekday() not in prefs:
        log(f"INFO: No preferences for today ({WEEKDAY_NAMES[date.today().weekday()]}); nothing to book")
        return 0

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=headless,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
            ],
        )
        context = browser.new_context(
            viewport={"width": 1280, "height": 720},
            locale="en-US",
        )
        page = context.new_page()
        Stealth().apply_stealth_sync(page)

        try:
            log("Navigating to booking page...")
            page.goto(booking_url, wait_until="domcontentloaded", timeout=30000)
            # Calendly is a React SPA — `load` fires before the form renders.
            # Wait for *something* meaningful (cookie button or a form input) so
            # we don't screenshot a blank canvas.
            try:
                page.wait_for_selector(
                    ", ".join(COOKIE_BUTTON_SELECTORS) + ", input:visible, textarea:visible",
                    timeout=15000,
                )
            except Exception:
                log("Timed out waiting for SPA content; proceeding anyway")
            human_delay(0.5, 1.0)

            shot = run_dir / "01_before_cookie_dismiss.png"
            page.screenshot(path=str(shot), full_page=True)
            log(f"Screenshot saved: {shot}")

            _dismiss_cookie_banner(page)

            shot = run_dir / "02_after_cookie_dismiss.png"
            page.screenshot(path=str(shot), full_page=True)
            log(f"Screenshot saved: {shot}")

            log("Selecting date and time...")
            picked = select_date_and_time(page, prefs, lookahead_days, skip_dates, run_dir)
            if not picked:
                log("ERROR: Could not find any matching date/time slot")
                page.screenshot(path=str(run_dir / "no_slot.png"), full_page=True)
                return 1
            picked_date, picked_time = picked
            log(f"Booked slot: {picked_date.isoformat()} {picked_time}")
            shot = run_dir / "04_after_slot_selected.png"
            page.screenshot(path=str(shot), full_page=True)
            log(f"Screenshot saved: {shot}")

            # Wait for form fields to appear
            log("Waiting for booking form...")
            page.wait_for_selector(
                "input:visible, textarea:visible",
                timeout=15000,
            )
            human_delay(1.0, 2.0)

            # Banner can appear late; retry once now that the form is present.
            _dismiss_cookie_banner(page, total_timeout_ms=2000)

            shot = run_dir / "05_before_fill.png"
            page.screenshot(path=str(shot), full_page=True)
            log(f"Screenshot saved: {shot}")

            # Fill in the form fields
            log("Filling form fields...")
            filled = match_and_fill(page, fields)

            if not filled:
                log("WARNING: No form fields matched. Available labels on page:")
                _debug_form_fields(page)
                page.screenshot(path=str(run_dir / "no_match.png"), full_page=True)
                return 1

            log(f"Filled {len(filled)}/{len(fields)} fields: {filled}")
            unfilled = set(fields.keys()) - set(filled)
            if unfilled:
                log(f"WARNING: Unfilled fields: {list(unfilled)}")
                log("Available labels on page (rename your BOOKING_* vars to overlap):")
                _debug_form_fields(page)

            human_delay(1.0, 2.0)
            page.screenshot(path=str(run_dir / "06_after_fill.png"), full_page=True)

            # DRY RUN: submit disabled so you can verify field population.
            log("DRY RUN: skipping submit. Verify the filled form, then re-enable.")
            submit_button = _find_submit_button(page)
            if not submit_button:
                log("NOTE: submit button not located (would have failed at submit step)")
            else:
                btn_text = (submit_button.inner_text() or "").strip()
                btn_aria = submit_button.get_attribute("aria-label") or ""
                log(f"Submit button found but NOT clicked. text={btn_text!r} aria-label={btn_aria!r}")
                submit_button.scroll_into_view_if_needed()
                submit_button.evaluate("el => el.style.outline = '4px solid #ff3b3b'")
            human_delay(2.0, 4.0)
            shot = run_dir / "07_dry_run.png"
            page.screenshot(path=str(shot), full_page=True)
            log(f"Screenshot saved: {shot}")
            return 0

            # --- Original submit/confirmation logic (re-enable to actually book) ---
            # log("Submitting booking...")
            # submit_button = _find_submit_button(page)
            # if not submit_button:
            #     log("ERROR: Could not find submit button")
            #     page.screenshot(path=str(run_dir / "no_submit.png"))
            #     return 1
            #
            # submit_button.click()
            # log("Submit clicked, waiting for confirmation...")
            #
            # try:
            #     page.wait_for_selector(
            #         "text=confirmed, text=scheduled, text=You are scheduled",
            #         timeout=15000,
            #     )
            #     log("SUCCESS: Booking confirmed!")
            # except Exception:
            #     human_delay(3.0, 5.0)
            #     if "invitees" in page.url or "confirmed" in page.url.lower():
            #         log("SUCCESS: Redirected to confirmation page")
            #     else:
            #         log(f"WARNING: Uncertain confirmation. Current URL: {page.url}")
            #         page.screenshot(path=str(run_dir / "uncertain.png"))
            #
            # page.screenshot(path=str(run_dir / "confirmation.png"))
            # log("Screenshot saved: confirmation.png")
            # return 0

        except Exception as e:
            log(f"ERROR: {e}")
            try:
                shot = run_dir / "error.png"
                page.screenshot(path=str(shot), full_page=True)
                log(f"Error screenshot saved: {shot}")
            except Exception:
                pass
            return 1

        finally:
            context.close()
            browser.close()


_MONTH_NAMES = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]
_MONTH_HEADER_RE = re.compile(
    r"(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{4})"
)


def _read_current_month(page) -> tuple[int, int] | None:
    """Return (year, month) shown in the calendar header, or None if unreadable.

    Calendly puts the month label (e.g. 'May 2026') prominently in the
    calendar; we just read the first match in the page text.
    """
    try:
        body = page.evaluate("() => document.body.innerText")
    except Exception:
        return None
    m = _MONTH_HEADER_RE.search(body or "")
    if not m:
        return None
    return int(m.group(2)), _MONTH_NAMES.index(m.group(1)) + 1


def _find_month_nav_button(page, direction: str):
    """direction: 'next' or 'prev'. Returns a Locator or None."""
    if direction == "next":
        sels = [
            'button[aria-label*="Go to next month" i]',
            'button[aria-label*="Next month" i]',
            'button[aria-label="Next"]',
        ]
    else:
        sels = [
            'button[aria-label*="Go to previous month" i]',
            'button[aria-label*="Previous month" i]',
            'button[aria-label="Previous"]',
        ]
    for sel in sels:
        try:
            cand = page.locator(sel).first
            if cand.is_visible(timeout=500):
                return cand
        except Exception:
            continue
    return None


def _wait_for_calendar_availability(page, timeout_ms: int = 12000) -> bool:
    """Wait for Calendly to finish loading the current month's availability.

    Returns True if at least one bookable day appeared, False on timeout.
    Calendly shows a spinner while fetching availability and meanwhile every
    day button reads "No times available", so interacting too early always
    fails. We poll for any aria-label containing 'Times available'.
    """
    try:
        page.wait_for_selector(
            'button[aria-label*="Times available"]', timeout=timeout_ms
        )
        return True
    except Exception:
        return False


def _navigate_to_month(page, target: date, max_clicks: int = 24) -> bool:
    """Move the Calendly date picker to target's month, going either direction."""
    target_label = target.strftime("%B %Y")  # e.g. "May 2026"
    target_ym = (target.year, target.month)
    for _ in range(max_clicks):
        current = _read_current_month(page)
        if current is None:
            log(f"  Could not read current month while navigating to {target_label}")
            return False
        if current == target_ym:
            if not _wait_for_calendar_availability(page):
                log(f"  Availability did not load for {target_label}")
            return True
        direction = "next" if current < target_ym else "prev"
        nav_btn = _find_month_nav_button(page, direction)
        if not nav_btn:
            log(f"  Could not find {direction}-month control while seeking {target_label}")
            return False
        log(f"  Clicking {direction}-month: currently {current}, want {target_ym}")
        nav_btn.click()
        human_delay(0.4, 0.8)
    return False


def _click_day_cell(page, target: date) -> bool:
    """Click the calendar cell for `target`. Returns True iff the click landed.

    Calendly's day cell can render as <button> (older) or <td role="gridcell">
    with a <button> inside. The aria-label sometimes is "Friday, May 2",
    sometimes "Friday, May 2, 2026", sometimes "Saturday, May 2 - Times available".
    Try several patterns and fall back to dumping candidates for diagnostics.
    """
    long_date_no_year = target.strftime("%A, %B %-d")          # "Saturday, May 2"
    long_date_with_year = target.strftime("%A, %B %-d, %Y")    # "Saturday, May 2, 2026"
    iso = target.isoformat()                                    # "2026-05-02"
    day_num = str(target.day)

    selectors = [
        f'button[aria-label*="{long_date_with_year}"]',
        f'button[aria-label*="{long_date_no_year}"]',
        f'[role="gridcell"] button[aria-label*="{long_date_no_year}"]',
        f'button[data-date="{iso}"]',
        f'[data-date="{iso}"] button',
        # Last-resort: any button whose visible text is exactly the day number
        # AND lives inside a [role="gridcell"]/td (so we don't grab unrelated UI).
        f'[role="gridcell"] button:text-is("{day_num}")',
        f'td button:text-is("{day_num}")',
    ]

    for sel in selectors:
        try:
            locator = page.locator(sel)
            count = locator.count()
        except Exception:
            continue
        for i in range(count):
            btn = locator.nth(i)
            try:
                if not btn.is_visible(timeout=500):
                    continue
                aria_label = btn.get_attribute("aria-label") or ""
                klass = btn.get_attribute("class") or ""
                is_disabled = btn.get_attribute("aria-disabled") == "true" or btn.is_disabled()
                already_selected = "selected" in klass.lower()

                # Calendly disables the *currently selected* day to prevent
                # re-clicking; treat that as success — the time pane is already
                # showing slots for this date.
                if is_disabled and already_selected:
                    log(f"  Day already selected (matched {sel!r}); skipping click")
                    return True

                # If aria-label says "Times available" but button is disabled,
                # something else is going on — skip with a hint.
                if is_disabled:
                    label_lower = aria_label.lower()
                    if (
                        "times available" in label_lower
                        and "no times available" not in label_lower
                        and not already_selected
                    ):
                        log(f"  Day reports times available but is disabled? aria-label={aria_label!r}")
                    continue

                log(f"  Day cell matched via {sel!r}")
                btn.click()
                return True
            except Exception:
                continue

    # Nothing matched — dump every visible day-like button so we can see what
    # Calendly is actually rendering.
    _debug_dump_calendar_buttons(page)
    return False


def _debug_dump_calendar_buttons(page) -> None:
    """Log aria-label/text of likely day cells to help diagnose selector issues."""
    log("  Dumping candidate day cells for diagnostics:")
    locator = page.locator(
        '[role="gridcell"] button, table button, [data-date] button, button[aria-label]'
    )
    try:
        count = locator.count()
    except Exception:
        count = 0
    seen = 0
    for i in range(min(count, 80)):
        btn = locator.nth(i)
        try:
            if not btn.is_visible(timeout=200):
                continue
            aria = btn.get_attribute("aria-label") or ""
            data_date = btn.get_attribute("data-date") or ""
            txt = (btn.inner_text() or "").strip().replace("\n", " ")
            disabled = btn.get_attribute("aria-disabled") == "true" or btn.is_disabled()
            if not (aria or data_date or txt):
                continue
            log(f"    text={txt!r} aria-label={aria!r} data-date={data_date!r} disabled={disabled}")
            seen += 1
            if seen >= 30:
                break
        except Exception:
            continue
    if seen == 0:
        log("    (no day-like buttons visible on the page)")


def _click_time_slot(page, hhmm: str) -> bool:
    """Find a time-slot button matching hhmm in the right-hand pane and click it."""
    for text in time_to_calendly_strings(hhmm):
        try:
            btn = page.get_by_role(
                "button", name=re.compile(re.escape(text), re.IGNORECASE)
            ).first
            if btn.is_visible(timeout=1000):
                btn.click()
                return True
        except Exception:
            continue
    return False


def _confirm_time_selection(page) -> bool:
    """After picking a time, Calendly shows a 'Next' button to advance to the form."""
    selectors = [
        "button:has-text('Next')",
        "button:has-text('Confirm')",
        "button:has-text('Continue')",
    ]
    for selector in selectors:
        try:
            btn = page.locator(selector).first
            if btn.is_visible(timeout=2000):
                btn.click()
                return True
        except Exception:
            continue
    return False


def _shoot(page, run_dir: Path, name: str) -> None:
    """Save a full-page screenshot with `name` (no extension)."""
    path = run_dir / f"{name}.png"
    try:
        page.screenshot(path=str(path), full_page=True)
        log(f"  Screenshot: {path.name}")
    except Exception as e:
        log(f"  Screenshot failed for {name}: {e}")


def select_date_and_time(
    page,
    prefs: dict[int, list[str]],
    lookahead_days: int,
    skip_dates: set[date],
    run_dir: Path,
) -> tuple[date, str] | None:
    """Walk same-weekday candidates furthest-first; book the first available match.

    Returns (date, "HH:MM") on success, None if no slot was reachable. Captures
    a screenshot at every meaningful step into run_dir for troubleshooting.
    """
    today = date.today()
    weekday = today.weekday()
    times = prefs.get(weekday)
    if not times:
        log(f"No BOOKING_PREF_{WEEKDAY_NAMES[weekday].upper()} set; nothing to do")
        return None

    candidates = candidate_dates(today, lookahead_days, weekday, skip_dates)
    log(f"Candidate {WEEKDAY_NAMES[weekday]} dates (furthest first): "
        f"{[d.isoformat() for d in candidates]}")
    log(f"Time preferences: {times}")

    if _wait_for_calendar_availability(page, timeout_ms=15000):
        log("Calendar availability loaded for the current month")
    else:
        log("WARNING: No 'Times available' day appeared within 15s on the "
            "initial month view; will still try candidates")

    _shoot(page, run_dir, "03_slot_00_initial_calendar")

    for attempt_idx, d in enumerate(candidates, start=1):
        tag = f"03_slot_{attempt_idx:02d}_{d.isoformat()}"
        log(f"Trying {d.isoformat()}...")

        if not _navigate_to_month(page, d):
            _shoot(page, run_dir, f"{tag}_a_month_nav_failed")
            continue
        human_delay(0.3, 0.7)
        _shoot(page, run_dir, f"{tag}_a_month_view")

        if not _click_day_cell(page, d):
            log(f"  {d.isoformat()} not selectable, skipping")
            _shoot(page, run_dir, f"{tag}_b_day_not_selectable")
            continue
        _shoot(page, run_dir, f"{tag}_b_day_clicked")

        # Wait for the time-slot pane to appear.
        time_pane_loaded = False
        try:
            page.wait_for_selector(
                "button[data-container='time-button'], button[data-start-time]",
                timeout=8000,
            )
            time_pane_loaded = True
        except Exception:
            try:
                page.wait_for_selector("button:has-text(':')", timeout=4000)
                time_pane_loaded = True
            except Exception:
                pass
        if not time_pane_loaded:
            log("  Time slots didn't appear, skipping")
            _shoot(page, run_dir, f"{tag}_c_no_time_pane")
            continue
        human_delay(0.4, 0.9)
        _shoot(page, run_dir, f"{tag}_c_time_pane")

        for t in times:
            t_safe = t.replace(":", "")
            if _click_time_slot(page, t):
                log(f"  Selected {d.isoformat()} {t}")
                _shoot(page, run_dir, f"{tag}_d_time_{t_safe}_clicked")
                if _confirm_time_selection(page):
                    log("  Confirmed; advancing to form")
                    human_delay(0.5, 1.0)
                    _shoot(page, run_dir, f"{tag}_e_time_{t_safe}_confirmed")
                else:
                    _shoot(page, run_dir, f"{tag}_e_time_{t_safe}_no_confirm_btn")
                return d, t

        log(f"  No matching time slot on {d.isoformat()}")
        _shoot(page, run_dir, f"{tag}_d_no_time_match")
    return None


COOKIE_BUTTON_SELECTORS = [
    "button#onetrust-accept-btn-handler",
    "button:has-text('I understand')",
    "button:has-text('Accept all')",
    "button:has-text('Accept All')",
    "button:has-text('Accept')",
    "button:has-text('Allow all')",
    "button:has-text('Got it')",
    "button:has-text('I agree')",
]


def _dismiss_cookie_banner(page, total_timeout_ms: int = 8000) -> bool:
    """Best-effort: click an Accept/Allow cookie button if present. Never raises.

    The banner is injected asynchronously, so we poll up to total_timeout_ms.
    Returns True if a button was clicked.
    """
    deadline = time.monotonic() + total_timeout_ms / 1000
    while time.monotonic() < deadline:
        for selector in COOKIE_BUTTON_SELECTORS:
            try:
                btn = page.locator(selector).first
                if btn.is_visible(timeout=500):
                    log(f"Dismissing cookie banner via {selector!r}")
                    btn.click()
                    # Wait for the banner to actually go away so the next
                    # screenshot doesn't capture a transient re-render.
                    try:
                        page.locator(selector).first.wait_for(
                            state="hidden", timeout=5000
                        )
                    except Exception:
                        pass
                    try:
                        page.wait_for_load_state("load", timeout=5000)
                    except Exception:
                        pass
                    human_delay(0.8, 1.5)
                    return True
            except Exception:
                continue
        time.sleep(0.3)
    log("No cookie banner detected within timeout")
    return False


def _find_submit_button(page):
    """Try multiple strategies to locate the submit/schedule button."""
    selectors = [
        'button[type="submit"]',
        "button:has-text('Schedule Event')",
        "button:has-text('Confirm')",
        "button:has-text('Schedule')",
        "button:has-text('Book')",
        'input[type="submit"]',
    ]
    for selector in selectors:
        try:
            btn = page.locator(selector).first
            if btn.is_visible(timeout=2000):
                return btn
        except Exception:
            continue
    return None


def _debug_form_fields(page) -> None:
    """Print all visible form fields and their labels for debugging."""
    elements = page.query_selector_all(
        "input:visible, textarea:visible, select:visible"
    )
    for el in elements:
        input_type = (el.get_attribute("type") or "text").lower()
        if input_type in ("hidden", "submit", "button"):
            continue
        label = _get_label_for_element(page, el)
        el_id = el.get_attribute("id") or ""
        name = el.get_attribute("name") or ""
        log(f"  Field: label='{label}' id='{el_id}' name='{name}' type='{input_type}'")


if __name__ == "__main__":
    sys.exit(run())
