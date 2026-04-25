#!/usr/bin/env python3
"""Calendly automated booking via Playwright."""

import os
import random
import sys
import time
from datetime import datetime, timezone

from playwright.sync_api import sync_playwright
from playwright_stealth import stealth_sync


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


def match_and_fill(page, fields: dict[str, str]) -> list[str]:
    """Find form fields on the page and fill them based on BOOKING_* env vars.

    Returns list of field labels that were successfully filled.
    """
    filled = []

    # Gather all visible input/textarea/select elements
    form_elements = page.query_selector_all(
        "input:visible, textarea:visible, select:visible"
    )

    for element in form_elements:
        input_type = (element.get_attribute("type") or "").lower()
        if input_type in ("hidden", "submit", "button", "checkbox", "radio"):
            continue

        # Try to determine the field's label text
        label_text = _get_label_for_element(page, element)
        if not label_text:
            continue

        label_lower = label_text.lower()

        # Match against collected BOOKING_* fields
        for field_key, field_value in fields.items():
            if field_key in label_lower or label_lower in field_key:
                if field_key not in [f.lower() for f in filled]:
                    log(f"  Filling '{label_text}' with BOOKING_{field_key.upper().replace(' ', '_')}")
                    tag = element.evaluate("el => el.tagName.toLowerCase()")
                    if tag == "select":
                        element.select_option(label=field_value)
                    else:
                        # Use the Playwright locator API for typing
                        element.click()
                        human_delay(0.2, 0.5)
                        element.fill("")
                        page.keyboard.type(field_value, delay=random.randint(30, 120))
                    filled.append(field_key)
                    human_delay(0.5, 1.2)
                break

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

    log(f"Booking URL: {booking_url}")
    log(f"Headless: {headless}")
    log(f"Fields to fill: {list(fields.keys())}")

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
        stealth_sync(page)

        try:
            log("Navigating to booking page...")
            page.goto(booking_url, wait_until="networkidle", timeout=30000)
            human_delay(1.0, 2.0)

            # Wait for form fields to appear
            log("Waiting for booking form...")
            page.wait_for_selector(
                "input:visible, textarea:visible",
                timeout=15000,
            )
            human_delay(1.0, 2.0)

            # Take a screenshot for debugging
            page.screenshot(path="/tmp/before_fill.png")
            log("Screenshot saved: /tmp/before_fill.png")

            # Fill in the form fields
            log("Filling form fields...")
            filled = match_and_fill(page, fields)

            if not filled:
                log("WARNING: No form fields matched. Available labels on page:")
                _debug_form_fields(page)
                page.screenshot(path="/tmp/no_match.png")
                return 1

            log(f"Filled {len(filled)}/{len(fields)} fields: {filled}")
            unfilled = set(fields.keys()) - set(filled)
            if unfilled:
                log(f"WARNING: Unfilled fields: {list(unfilled)}")

            human_delay(1.0, 2.0)
            page.screenshot(path="/tmp/after_fill.png")

            # Click submit button
            log("Submitting booking...")
            submit_button = _find_submit_button(page)
            if not submit_button:
                log("ERROR: Could not find submit button")
                page.screenshot(path="/tmp/no_submit.png")
                return 1

            submit_button.click()
            log("Submit clicked, waiting for confirmation...")

            # Wait for confirmation
            try:
                page.wait_for_selector(
                    "text=confirmed, text=scheduled, text=You are scheduled",
                    timeout=15000,
                )
                log("SUCCESS: Booking confirmed!")
            except Exception:
                # Check if URL changed to a confirmation page
                human_delay(3.0, 5.0)
                if "invitees" in page.url or "confirmed" in page.url.lower():
                    log("SUCCESS: Redirected to confirmation page")
                else:
                    log(f"WARNING: Uncertain confirmation. Current URL: {page.url}")
                    page.screenshot(path="/tmp/uncertain.png")

            page.screenshot(path="/tmp/confirmation.png")
            log("Screenshot saved: /tmp/confirmation.png")
            return 0

        except Exception as e:
            log(f"ERROR: {e}")
            try:
                page.screenshot(path="/tmp/error.png")
                log("Error screenshot saved: /tmp/error.png")
            except Exception:
                pass
            return 1

        finally:
            context.close()
            browser.close()


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
