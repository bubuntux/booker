#!/usr/bin/env python3
"""Calendly automated booking via Playwright."""

import os
import random
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

SCREENSHOT_ROOT = Path(__file__).parent / "screenshots"

from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth


def log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"[{ts}] {msg}", flush=True)


def collect_booking_fields() -> dict[str, str]:
    """Collect all BOOKING_* env vars except BOOKING_URL.

    Converts env var suffixes to lowercase label fragments:
        BOOKING_PHONE_NUMBER -> "phone number"
        BOOKING_NAME         -> "name"
        BOOKING_EMAIL        -> "email"
    """
    fields = {}
    for key, value in os.environ.items():
        if key.startswith("BOOKING_") and key != "BOOKING_URL" and value:
            label = key[len("BOOKING_"):].lower().replace("_", " ")
            fields[label] = value
    return fields


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

    run_dir = SCREENSHOT_ROOT / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir.mkdir(parents=True, exist_ok=True)

    log(f"Booking URL: {booking_url}")
    log(f"Headless: {headless}")
    log(f"Fields to fill: {list(fields.keys())}")
    log(f"Screenshots: {run_dir}")

    if not fields:
        log("ERROR: No BOOKING_* form field env vars found (need at least BOOKING_NAME, BOOKING_EMAIL, etc.)")
        return 1

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

            # Wait for form fields to appear
            log("Waiting for booking form...")
            page.wait_for_selector(
                "input:visible, textarea:visible",
                timeout=15000,
            )
            human_delay(1.0, 2.0)

            # Banner can appear late; retry once now that the form is present.
            _dismiss_cookie_banner(page, total_timeout_ms=2000)

            shot = run_dir / "03_before_fill.png"
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
            page.screenshot(path=str(run_dir / "04_after_fill.png"), full_page=True)

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
            shot = run_dir / "05_dry_run.png"
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
