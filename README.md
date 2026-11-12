# Brightspace Scraper

Backs up a BrightSpace course, or every course you are enrolled in, maintaining proper file structure.

Standard downloads typically flatten the directory structure, ignore external links, or omit the rich text context provided in module descriptions.

This scraper retrieves all of it, and by default everything else a student account can read: assignments with your submissions and feedback, news, discussions, quizzes, grades, groups and group lockers, the calendar, and every other route of the Brightspace API. Requests run concurrently (asyncio), so a course takes about half a minute.

If you used an earlier version that only walked the table of contents, see [Getting the previous behaviour](#getting-the-previous-behaviour).

## Prerequisites

* Python 3.11 or higher
* [uv](https://github.com/astral-sh/uv) (recommended) or pip

## Installation

1.  **Clone the repository:**
    ```bash
    git clone https://github.com/rbmr/BrightspaceScraper
    cd BrightspaceScraper
    ```

2.  **Install dependencies:**
    
    Using **uv** (Recommended, uses `uv.lock`):
    ```bash
    uv sync
    ```

    Or using **pip**:
    ```bash
    pip install .
    ```

3.  **Install Playwright browsers:**
    This tool uses a real browser for authentication. You must install the browser binaries:
    ```bash
    playwright install chromium
    ```

## Authentication

Brightspace uses complex authentication (SSO, MFA) which is difficult to replicate with simple scripts. This project solves this by using a real browser to capture your session.

1.  Run the auth script with your university's Brightspace URL:
    ```bash
    # Example for TU Delft
    python src/auth.py https://brightspace.tudelft.nl
    ```

2.  A Chromium browser window will open.
3.  Log in manually using your credentials (including any 2FA/MFA steps).
4.  Once you land on the Brightspace homepage (e.g., URL contains `/d2l/home`), the script will detect the login, save your session cookies and tokens to `auth.json`, and close the browser.

> **Note:** The `auth.json` file contains your session tokens. Do not share this file.

## Usage

Once authenticated, you can scrape a specific course.

1.  **Find the Course URL:**
    Navigate to the course you want to download in your browser and go to the "Content" tab. The URL should look something like:
    `https://brightspace.tudelft.nl/d2l/le/content/12345/Home`

2.  **Run the Scraper:**
    ```bash
    python src/main.py <COURSE_URL>
    ```

    **Options:**
    * `--output` / `-o`: Specify the download directory (default: `./downloads`).
    * `--auth` / `-a`: Path to the auth file (default: `auth.json`).
    * `--base-url`: The Brightspace URL (default: the one stored in `auth.json`). Only needed with `--all` when `auth.json` holds more than one site.
    * `--all`: Scrape every course you are enrolled in (instead of giving a course URL). `--match <regex>`, `--active-only` and `--dry-run` select/preview courses.
    * `--markdown` / `--md`: Also write `.md` copies of HTML pages and rich-text fields, and download the images they use.
    * `--toc-only`: Only walk the content table of contents (skip dropbox, news, grades, ... and the route crawl).
    * `--concurrency` / `-j`: How many requests are in flight at once (default 16). Everything is downloaded asynchronously (asyncio + httpx); the courses themselves are scraped one after the other.
    * `--linked-only`: Only download the linked files (see `linked_files/` below) on top of an earlier backup. Fast, because files that already exist are skipped.
    * `--reprobe`: Ask routes again even if they were forbidden every time before for your role.

    **Example:**
    ```bash
    python src/main.py https://brightspace.tudelft.nl/d2l/le/content/54321/Home --output ./my_courses
    ```

3.  **Output:**
    The tool will create a folder named `<Course name> (<ID>)` inside your output directory. Inside, you will find:
    * A full directory structure matching the course modules.
    * Course files (PDFs, PPTs, etc.).
    * `course_structure.json`: A detailed JSON map of the entire course hierarchy, including descriptions and links, and `toc_raw.json`, the table of contents exactly as Brightspace returned it (it also lists topic types that are not downloaded as files, such as quizzes and LTI tools).
    * Folders for dropbox (assignments, submissions, feedback), news, discussions, quizzes, surveys, checklists, grades, calendar, groups (with group lockers), outcomes and content metadata.
    * `linked_files/`: course files and images that pages, descriptions or posts link to but that are not topics of the table of contents (`_index.json` lists them). With `--md`, the links in the `.md` files are rewritten to point at these local copies.
    * `raw/`: the JSON of every other documented API route that answered, one file per request.
    * `_account/` (in the output root): the same for account-level routes, plus `user_feed.json`.
    * `coverage.json`, `permission_warnings.json` and `unavailable_content.json`: what happened to every route and what could not be retrieved, see [Completeness](#completeness).

    **Good to know**
    * Your session in `auth.json` lasts about a day. When it has expired the run stops with a message and exit code 1: run `src/auth.py` again.
    * Files that already exist are skipped, so a run can safely be repeated, or continued after an interruption.
    * On Windows, paths longer than 260 characters are supported, and a single file or folder name is shortened to 180 characters (keeping its extension).
    * Each course prints how long its phases took (`[Time] ...`).

## Getting the previous behaviour

The first version of this tool walked only the content table of contents, downloading its files into the module folders and writing `course_structure.json`. That is what you get with:

```bash
python src/main.py <COURSE_URL> --toc-only
```

Differences from that first version, even with `--toc-only`:

| | First version | Now |
|---|---|---|
| Default | table of contents only | everything (use `--toc-only` for the old scope) |
| Output folder | `Course_<ID>` | `<Course name> (<ID>)` |
| Requests | one at a time | concurrent; `-j 1` makes them sequential again |
| Failed downloads | printed as `[Error]` and skipped | 403 is a warning, anything else stops the course (exit code 1) |
| Existing files | downloaded again on every run | skipped |
| API version | tried a fixed list (1.82, 1.74, ... 1.50) | the newest one the server offers |
| Extra files | none | `toc_raw.json`, `permission_warnings.json`, `unavailable_content.json` |
| `.md` copies | none | optional, with `--md` |
| Many courses | one URL at a time | `--all`, `--match`, `--active-only` |

`--toc-only` also skips the account-level crawl, so it starts straight away.

## Completeness

Every GET route of the [Brightspace API documentation](https://docs.valence.desire2learn.com/http-routingtable.html) is listed in `src/routes_manifest.json`. After a course is scraped, `coverage.json` says what happened to each of them:

* `ok`: fetched. `empty`: the list it belongs to has no items in this course. `absent`: the server answered 404 (nothing there).
* `forbidden`: HTTP 403. The branch is not followed any deeper, and a route family that is only ever forbidden for your role is not asked again (`--reprobe` overrides this). Also listed in `permission_warnings.json`.
* `needs-params`: the route wants query parameters that nothing in your account hands out. These are the only routes that are neither fetched nor refused, so check this list when you need certainty.
* `not-applicable`: the server says the feature is not set up.

Any other failure (a 5xx after retries, an unexpected 400, a broken download) stops the course, and with `--all` the run continues with the next course and exits non-zero. Content the course advertises but the server does not have (dangling file topics, Content Service videos) is listed in `unavailable_content.json`.

The manifest is a snapshot of the documentation: routes added to Brightspace later are not covered until it is regenerated.