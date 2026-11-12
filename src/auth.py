import argparse
import json
import time
from pathlib import Path

import typer
from playwright.sync_api import sync_playwright

TOKEN_KEY = "D2L.Fetch.Tokens"


def _read_fetch_token(page) -> str | None:
    """Return the D2L fetch access token if the SPA has written it yet."""
    try:
        raw = page.evaluate(
            "() => { for (const k of Object.keys(localStorage)) {"
            " if (k === arguments[0]) return localStorage.getItem(k); }"
            " return null; }",
            TOKEN_KEY,
        )
    except Exception:
        return None
    if not raw:
        return None
    try:
        data = json.loads(raw)
        first_key = next(iter(data))
        return data[first_key].get("access_token")
    except Exception:
        return None


def save_auth_state(
    url: str = typer.Argument(..., help="The login URL for your Brightspace instance (e.g., https://brightspace.tudelft.nl)"),
    output: Path = typer.Option(Path("auth.json"), "--output", "-o", help="Path to save the auth file"),
    course_content_url: str = typer.Option("", "--content", "-c", help="Optional course Content URL to visit if the dashboard does not issue a token")
):
    with sync_playwright() as p:
        print(f"Launching browser...")
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()

        print(f"Navigate to {url} and log in.")
        page.goto(url)

        # Wait until we see a specific element that confirms login
        # Generic check: waiting until "d2l/home" is in the URL (typical dashboard)
        try:
            page.wait_for_url("**/d2l/home**", timeout=300_000)
        except Exception:
            print("Timed out waiting for login. Did you reach the homepage?")
            browser.close()
            raise typer.Exit(code=1)

        print("Login detected. Waiting for the SPA to issue its fetch token...")

        # The dashboard sets D2L.Fetch.Tokens in localStorage lazily, so poll
        # for it rather than sleeping a fixed second and hoping it is there.
        token = _read_fetch_token(page)
        waited = 0
        while token is None and waited < 60:
            time.sleep(2)
            waited += 2
            token = _read_fetch_token(page)

        if token is None and course_content_url:
            print("No token on the dashboard. Visiting the course Content page to trigger it...")
            page.goto(course_content_url)
            page.wait_for_load_state("networkidle", timeout=120_000)
            waited = 0
            while token is None and waited < 120:
                time.sleep(2)
                waited += 2
                token = _read_fetch_token(page)

        if token is None:
            print("Could not find the fetch token in localStorage. Saving what is there anyway.")
        else:
            print("Fetch token found.")

        context.storage_state(path=output)
        print("Auth state saved to 'auth.json'.")

        browser.close()


if __name__ == "__main__":
    typer.run(save_auth_state)