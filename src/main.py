# src/main.py

import asyncio
import json
import os
import re
import time
from pathlib import Path
from urllib.parse import unquote, urlparse
from typing import Optional, Dict, List

import httpx
import typer

from api import Api, ScrapeError, cap_name, gather_all
from crawler import Crawler
from extras import CourseExtras, clean_name
from linked_files import LinkedFiles
from markdown_export import MarkdownExporter
from models import Directory, File, Link, Node, Assignment

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
DEFAULT_CONCURRENCY = 16


class Stopwatch:
    """Prints how long each phase of a run took."""

    def __init__(self):
        self.mark = time.perf_counter()

    def lap(self, label: str):
        now = time.perf_counter()
        print(f"[Time] {label}: {now - self.mark:.1f}s")
        self.mark = now


def _summary(coverage: Dict[str, str]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for status in coverage.values():
        kind = status.split(" ")[0].rstrip(":")
        counts[kind] = counts.get(kind, 0) + 1
    return counts


class BrightspaceParser:

    def __init__(self, base_url: str, auth_path: Path, markdown: bool = False, toc_only: bool = False,
                 reprobe: bool = False, linked_only: bool = False, concurrency: int = DEFAULT_CONCURRENCY):
        self.base_url = base_url
        self.toc_only = toc_only
        self.linked_only = linked_only
        self.html_pages: Dict[Path, str] = {}
        self.client = self._make_client(auth_path, concurrency)
        self.api = Api(self.client, base_url)
        self.api.reprobe = reprobe
        self.md: Optional[MarkdownExporter] = MarkdownExporter(self.api) if markdown else None
        self.user_id = 0

    async def start(self):
        await self.api.discover_versions()
        print(f"[OK] API versions: le {self.api.le_version}, lp {self.api.lp_version}")
        me = await self.api.json(self.api.lp("/users/whoami"))
        if me is None:
            raise ScrapeError("Brightspace refused the session: it has probably expired, run auth.py again.")
        self.user_id = int(me["Identifier"])

    async def aclose(self):
        if self.md:
            await self.md.aclose()
        await self.client.aclose()

    def _make_client(self, auth_path: Path, concurrency: int) -> httpx.AsyncClient:
        with open(auth_path, 'r') as f:
            data = json.load(f)

        client = httpx.AsyncClient(
            headers={"User-Agent": USER_AGENT},
            follow_redirects=True,
            timeout=httpx.Timeout(120.0, pool=None),  # requests wait for a free connection instead of failing
            limits=httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency),
        )
        for cookie in data.get('cookies', []):
            client.cookies.set(cookie['name'], cookie['value'], domain=cookie['domain'])

        token = None
        for origin in data.get('origins', []):
            if origin['origin'].rstrip("/") == self.base_url.rstrip("/"):
                for item in origin['localStorage']:
                    if item['name'] == "D2L.Fetch.Tokens":
                        token_data = json.loads(item['value'])
                        first_key = next(iter(token_data))
                        token = token_data[first_key].get("access_token")
                        break

        if token:
            client.headers["Authorization"] = f"Bearer {token}"
            print("[OK] Authenticated with Bearer Token.")
        else:
            print("[Warning] Could not find Bearer token in auth.json, proceeding with cookies...")
        return client

    async def save_user_feed(self, output_root: Path):
        """The account-wide activity feed (not tied to one course)."""
        feed = await self.api.json(self.api.lp("/feed/"))
        if feed is not None:
            output_root.mkdir(parents=True, exist_ok=True)
            with open(output_root / "user_feed.json", "w", encoding="utf-8") as f:
                json.dump(feed, f, indent=2, ensure_ascii=False)

    async def list_courses(self) -> List[Dict]:
        """All course offerings the logged-in user is enrolled in."""
        items = await self.api.paged(self.api.lp("/enrollments/myenrollments/?orgUnitTypeId=3")) or []
        courses = []
        for item in items:
            org, access = item['OrgUnit'], item.get('Access', {})
            courses.append({
                'id': str(org['Id']),
                'code': org.get('Code', ''),
                'name': org.get('Name', ''),
                'active': bool(access.get('IsActive')) and bool(access.get('CanAccess', True)),
            })
        return courses

    async def parse_course(self, course_url: str, output_root: Path) -> Directory:
        match = re.search(r"/content/(\d+)", course_url)
        if not match:
            raise ValueError("Could not find Course ID in URL.")
        return await self.parse_course_id(match.group(1), output_root)

    async def parse_course_id(self, course_id: str, output_root: Path) -> Directory:
        api = self.api
        api.reset_course()
        self.html_pages = {}
        watch = Stopwatch()
        print(f"[Search] Parsing Course ID: {course_id}")
        course_name, role = await gather_all(self._fetch_course_name(course_id), self._role(course_id))
        course_name = course_name or f"Course_{course_id}"
        api.scope = role

        # ignoreDateRestrictions ensures we see hidden content
        toc_data = await api.json(api.le(course_id, "/content/toc?ignoreDateRestrictions=true"))
        if toc_data is None:  # no permission (warning already recorded)
            toc_data = {"Modules": []}

        course_root = output_root / course_name
        course_root.mkdir(parents=True, exist_ok=True)

        # Keep the untouched TOC so topic types we don't model (quizzes, LTI, SCORM, ...) are not lost
        with open(course_root / "toc_raw.json", "w", encoding="utf-8") as f:
            json.dump(toc_data, f, indent=2, ensure_ascii=False)

        children = [c for c in await gather_all(
            *(self._process_module(module, course_root) for module in toc_data.get('Modules', []))) if c]

        root_node = Directory(name=course_name, children=children)
        root_node.save_json(course_root / "course_structure.json")

        watch.lap("table of contents and its files")
        crawler = None
        try:
            if self.linked_only:
                print("[Linked files] course files that pages link to")
                await LinkedFiles(api, course_root, toc_data, self.md is not None, self.html_pages).run()
            elif not self.toc_only:
                await CourseExtras(api, course_id, course_root, toc_data, self.user_id, self.md).run()
                watch.lap("extras")
                crawler = Crawler(api, course_root, self.user_id, course_id, toc_data)
                print("[Crawl] every other documented route")
                await crawler.run()
                watch.lap("route crawl")
                print("[Linked files] course files that pages link to")
                await LinkedFiles(api, course_root, toc_data, self.md is not None, self.html_pages).run()
                watch.lap("linked files")
        finally:
            self._save_report(course_root, crawler)
        if crawler and crawler.failures:
            raise ScrapeError(f"{len(crawler.failures)} route(s) failed, see coverage.json: "
                              + "; ".join(f"{p}: {e[:80]}" for p, e in list(crawler.failures.items())[:3]))

        print(f"\n[OK] Done! Exported to: {course_root}")
        return root_node

    async def _role(self, course_id: str) -> str:
        """The role the user has in this course: permissions (and so 403s) are shared by courses with the same role."""
        data = await self.api.json(self.api.lp(f"/enrollments/myenrollments/{course_id}")) or {}
        return str((data.get("Access") or {}).get("ClasslistRoleName") or "")

    async def crawl_account(self, output_root: Path):
        """The routes that belong to no course (profile, awards, notifications, ...)."""
        self.api.reset_course()
        self.api.scope = "account"
        root = output_root / "_account"
        watch = Stopwatch()
        org_ids = [c["id"] for c in await self.list_courses()]
        crawler = Crawler(self.api, root, self.user_id, org_ids=org_ids)
        print("[Crawl] account-level routes")
        await crawler.run(scope_course=False)
        watch.lap("account routes")
        coverage = crawler.coverage(scope_course=False)
        root.mkdir(parents=True, exist_ok=True)
        with open(root / "coverage.json", "w", encoding="utf-8") as f:
            json.dump({"summary": _summary(coverage), "routes": coverage, "failures": crawler.failures},
                      f, indent=2, ensure_ascii=False)
        self._save_report(root)
        print(f"[Coverage] account routes: {_summary(coverage)}")

    def _save_report(self, course_root: Path, crawler: Optional[Crawler] = None):
        if crawler:
            coverage = crawler.coverage()
            with open(course_root / "coverage.json", "w", encoding="utf-8") as f:
                json.dump({"summary": _summary(coverage), "routes": coverage, "failures": crawler.failures},
                          f, indent=2, ensure_ascii=False)
            print(f"[Coverage] {_summary(coverage)}")
        for name, items, label in (
            ("permission_warnings.json", self.api.warnings, "route(s) not permitted"),
            ("unavailable_content.json", self.api.unavailable, "item(s) advertised by the course but not retrievable"),
        ):
            path = course_root / name
            if self.linked_only:
                # an add-on run: keep what the full run recorded and add what this one found
                if path.exists() and items:
                    known = json.loads(path.read_text(encoding="utf-8"))
                    seen = {i["endpoint"] for i in known}
                    items = known + [i for i in items if i["endpoint"] not in seen]
                elif path.exists():
                    continue
            if items:
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(items, f, indent=2, ensure_ascii=False)
                print(f"[Report] {len(items)} {label}, see {name}")
            else:
                path.unlink(missing_ok=True)

    async def _fetch_course_name(self, course_id: str) -> Optional[str]:
        data = await self.api.json(self.api.lp(f"/enrollments/myenrollments/{course_id}"))
        name = ((data or {}).get('OrgUnit') or {}).get('Name')
        return f"{clean_name(name)} ({course_id})" if name else None

    async def _process_module(self, module_data: Dict, parent_path: Path) -> Optional[Directory]:
        title = self._clean_name(module_data.get('Title', 'Untitled Module'))

        # Extract Description (Rich Text is usually in 'Html')
        description = module_data.get('Description', {}).get('Html', None)

        current_path = parent_path / title
        current_path.mkdir(exist_ok=True)
        if self.md:
            await self.md.html_to_file(description, current_path / "_description.md", title=title)

        # Sub-modules and topics are processed side by side; the order of the children stays the same
        sub_modules, topics = await gather_all(
            gather_all(*(self._process_module(m, current_path) for m in module_data.get('Modules', []))),
            gather_all(*(self._process_topic(t, current_path) for t in module_data.get('Topics', []))),
        )
        children = [c for c in sub_modules if c] + [n for n in topics if n]

        # Pass description to Directory model
        return Directory(name=title, description=description, children=children)

    async def _process_topic(self, topic_data: Dict, parent_path: Path) -> Optional[Node]:
        title = self._clean_name(topic_data.get('Title', 'Untitled Topic'))
        description = topic_data.get('Description', {}).get('Html', None)
        url = topic_data.get('Url', '')

        # 1 = File, 2 = Link, 3 = Dropbox (Assignments); other types are covered by the extras / toc_raw.json
        activity_type = topic_data.get('ActivityType', -1)

        if self.md and description:
            await self.md.html_to_file(description, parent_path / f"{title}.description.md", title=title)

        if activity_type == 1:  # File
            file_name = self._get_filename_from_url(url) or f"{title}.pdf"
            save_path = parent_path / file_name
            download_url = f"{self.base_url}{url}"

            if url.startswith("d2l:"):
                # Content Service media (e.g. lecture videos): the URL is an identifier, not a path
                self.api.note_unavailable(url, f"Content Service media is not downloadable through the public API: {title}")
                return File(name=title, description=description, file=save_path)

            if not save_path.exists():
                print(f"  [Download] Downloading: {title}")
            await self.api.download(download_url, save_path,
                                    absent_reason=f"file does not exist on the server (404): {title}")

            if save_path.suffix.lower() in ('.html', '.htm') and save_path.exists():
                self.html_pages[save_path] = download_url  # its relative links resolve against this URL
                if self.md:
                    await self.md.convert_file(save_path, download_url)

            return File(name=title, description=description, file=save_path)

        elif activity_type == 2:  # Link
            return Link(name=title, description=description, url=url)

        elif activity_type == 3:  # DropBox (Assignment)
            print(f"  [Assignment] Found: {title}")
            return Assignment(
                name=title,
                description=description,
                url=url,
                due_date=topic_data.get('DueDate')
            )

        print(f"  [Info] Topic type {activity_type} not downloaded from the TOC: {title} (see toc_raw.json)")
        return None

    def _clean_name(self, name: str) -> str:
        return cap_name(re.sub(r'[<>:"/\\|?*]', '_', name).strip())

    def _get_filename_from_url(self, url: str) -> str:
        path = unquote(url.split('?')[0])
        return cap_name(path.split('/')[-1])


def _base_url_from_auth(auth_path: Path) -> Optional[str]:
    with open(auth_path, 'r') as f:
        origins = [o['origin'] for o in json.load(f).get('origins', [])]
    return origins[0].rstrip("/") if len(origins) == 1 else None


async def run(base_url: str, output: Path, auth: Path, course_url: Optional[str], match: Optional[str],
              active_only: bool, dry_run: bool, markdown: bool, toc_only: bool, linked_only: bool,
              reprobe: bool, concurrency: int):
    scraper = BrightspaceParser(base_url=base_url, auth_path=auth, markdown=markdown, toc_only=toc_only,
                                reprobe=reprobe, linked_only=linked_only, concurrency=concurrency)
    try:
        await scraper.start()
        if course_url:
            await scraper.save_user_feed(output)
            if not (linked_only or toc_only):
                await scraper.crawl_account(output)
            await scraper.parse_course(course_url, output)
            return

        courses = await scraper.list_courses()
        if match:
            pattern = re.compile(match, re.IGNORECASE)
            courses = [c for c in courses if pattern.search(c['code']) or pattern.search(c['name'])]
        if active_only:
            courses = [c for c in courses if c['active']]
        print(f"Selected {len(courses)} course(s).")

        if dry_run:
            for c in courses:
                print(f"  {c['id']}  {c['code']}  {c['name']}{'' if c['active'] else '  [inactive]'}")
            return

        if not (linked_only or toc_only):
            await scraper.save_user_feed(output)
            await scraper.crawl_account(output)
        failed = []
        for i, c in enumerate(courses, 1):  # one course at a time: everything inside a course runs concurrently
            print(f"\n===== [{i}/{len(courses)}] {c['code']} - {c['name']} =====")
            try:
                await scraper.parse_course_id(c['id'], output)
            except (ScrapeError, httpx.HTTPError, OSError, KeyError, ValueError) as e:
                print(f"[FAILED] {c['id']}: {type(e).__name__}: {e}")
                failed.append((c, e))

        print(f"\n[Summary] {len(courses) - len(failed)} succeeded, {len(failed)} failed.")
        for c, e in failed:
            print(f"  failed: {c['id']} {c['code']} {c['name']}: {e}")
        if failed:
            raise typer.Exit(code=1)
    finally:
        await scraper.aclose()


def main(
    course_url: Optional[str] = typer.Argument(None, help="The URL of the course content (e.g., https://brightspace.tudelft.nl/d2l/le/content/12345/Home). Not needed with --all."),
    output: Path = typer.Option(Path("./downloads"), "--output", "-o", help="Output directory for downloaded content"),
    auth: Path = typer.Option(Path("auth.json"), "--auth", "-a", help="Path to the auth file"),
    all_courses: bool = typer.Option(False, "--all", help="Scrape every course you are enrolled in"),
    base_url: Optional[str] = typer.Option(None, "--base-url", help="Brightspace URL (e.g. https://brightspace.tudelft.nl). Defaults to the one stored in auth.json."),
    match: Optional[str] = typer.Option(None, "--match", "-m", help="With --all: only courses whose code or name matches this regex (case-insensitive)"),
    active_only: bool = typer.Option(False, "--active-only", help="With --all: skip courses that are inactive or not accessible"),
    dry_run: bool = typer.Option(False, "--dry-run", help="With --all: list the selected courses without downloading"),
    markdown: bool = typer.Option(False, "--markdown", "--md", help="Also write .md copies of HTML pages and rich-text fields, and download the images they use"),
    toc_only: bool = typer.Option(False, "--toc-only", help="Only walk the content table of contents; skip dropbox, news, discussions, grades, ..."),
    linked_only: bool = typer.Option(False, "--linked-only", help="Only download the course files that saved pages link to (on top of an earlier backup)"),
    reprobe: bool = typer.Option(False, "--reprobe", help="Ask routes again even if they were forbidden every time before for your role"),
    concurrency: int = typer.Option(DEFAULT_CONCURRENCY, "--concurrency", "-j", min=1, help="Number of requests in flight at the same time"),
):
    """
    Scrape a Brightspace course (or, with --all, every enrolled course) and download its content.

    Requests run concurrently (asyncio). Routes you are not permitted to read (HTTP 403) are reported as
    warnings and skipped; any other failure stops that course and makes the exit code non-zero.
    """
    if all_courses == (course_url is not None):
        raise typer.BadParameter("Give either a course URL or --all.")

    if course_url:
        parsed_uri = urlparse(course_url)
        base_url = f"{parsed_uri.scheme}://{parsed_uri.netloc}"
    else:
        base_url = (base_url or _base_url_from_auth(auth) or "").rstrip("/")
        if not base_url:
            raise typer.BadParameter("Could not determine the Brightspace URL, pass --base-url.")
    print(f"Base URL: {base_url}")
    if os.name == "nt":  # lift the 260 character path limit
        output = Path("\\\\?\\" + str(output.resolve()))

    try:
        asyncio.run(run(base_url, output, auth, course_url, match, active_only, dry_run, markdown, toc_only,
                        linked_only, reprobe, concurrency))
    except ScrapeError as e:
        print(f"[FAILED] {e}")
        raise typer.Exit(code=1)


if __name__ == "__main__":
    typer.run(main)
