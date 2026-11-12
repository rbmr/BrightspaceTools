# src/linked_files.py
"""Course files that pages, descriptions and posts link to but that are not topics of the table of contents.

A page can link to any file in the course's file area (/content/enforced/<course>/<file>, or a quickLink of type
coursefile that redirects there). The TOC only lists the files a teacher turned into topics, so those linked
files would otherwise be missing from the backup. Everything found is downloaded into linked_files/ and
indexed in linked_files/_index.json. Images are saved too. With --markdown the images of a page (<img>) are
already saved next to its .md file, so only linked images are fetched here; the links in the .md files are
then rewritten to point at the local copies.
"""

import html
import json
import os
import re
from pathlib import Path
from typing import Dict, List, Optional, Set
from urllib.parse import quote, unquote, urljoin, urlparse

from bs4 import BeautifulSoup

from api import Api, ScrapeError, gather_all
from extras import clean_name

IMAGE_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "svg", "webp", "bmp", "ico"}
TEXT_SUFFIXES = {".json", ".html", ".htm"}  # .md files are derived from these, so scanning them would only duplicate
# a link: inside quotes (an attribute or a JSON string), so file names may contain spaces and parentheses
# (in JSON the closing quote is escaped, hence the optional backslash)
ENFORCED = re.compile(r"""(?:\b(href|src)=\\?)?["'](?:https?://[^/"'<>\\]+)?(/content/enforced/[^"'<>\\]+)\\?["']""")
QUICKLINK = re.compile(
    r"""(?:\b(href|src)=\\?)?["'](?:https?://[^/"'<>\\]+)?(/d2l/common/dialogs/quickLink/quickLink\.d2l\?[^"'<>\\]*type=coursefile[^"'<>\\]*)\\?["']""",
    re.I)


class LinkedFiles:

    def __init__(self, api: Api, root: Path, toc: Dict, markdown: bool, pages: Optional[Dict[Path, str]] = None):
        self.api = api
        self.pages = dict(pages or {})  # saved HTML page -> the URL it came from
        self.root = root
        self.out = root / "linked_files"
        self.markdown = markdown  # --markdown already downloads the <img> images of a page
        self.only_img: Dict[str, bool] = {}  # link -> every reference to it is an <img src>
        self.in_toc: Set[str] = set()
        self._collect_toc(toc.get("Modules", []))

    def _collect_toc(self, modules: List[Dict]):
        for m in modules:
            for t in m.get("Topics", []):
                url = t.get("Url") or ""
                if t.get("ActivityType") == 1 and url.startswith("/content/"):
                    self.in_toc.add(unquote(url.split("?")[0]).lower())
            self._collect_toc(m.get("Modules", []))

    # ---------- finding links ----------

    def _find(self) -> Dict[str, Set[str]]:
        """link -> the files it was found in"""
        found: Dict[str, Set[str]] = {}
        self.only_img = {}

        def note(link: str, attr: str, source: str):
            found.setdefault(link, set()).add(source)
            self.only_img[link] = self.only_img.get(link, True) and attr == "src"

        for path in self.root.rglob("*"):
            if path.suffix.lower() not in TEXT_SUFFIXES or self.out in path.parents or not path.is_file():
                continue
            text = html.unescape(path.read_text(encoding="utf-8", errors="ignore"))
            text = text.replace("\\/", "/")
            for pattern in (ENFORCED, QUICKLINK):
                for attr, link in pattern.findall(text):
                    note(link, attr.lower(), str(path.relative_to(self.root)))
        for page, url in self.pages.items():
            if not page.exists():
                continue
            soup = BeautifulSoup(page.read_bytes().decode("utf-8", errors="replace"), "html.parser")
            for tag, attr in (("a", "href"), ("img", "src")):
                for element in soup.find_all(tag, **{attr: True}):
                    absolute = urlparse(urljoin(url, element[attr].strip()))
                    if absolute.netloc == urlparse(self.api.base_url).netloc and absolute.path.startswith("/content/enforced/"):
                        note(absolute.path, attr, str(page.relative_to(self.root)))
        return found

    @staticmethod
    def _extension(path: str) -> str:
        name = path.rsplit("/", 1)[-1]
        return name.rsplit(".", 1)[-1].lower() if "." in name else ""

    # ---------- downloading ----------

    async def run(self):
        index: Dict[str, Dict] = {}
        for _ in range(3):  # a linked page can link to more files
            todo = [(link, sources) for link, sources in sorted(self._find().items()) if link not in index]
            entries = await gather_all(*(self._handle(link, sources) for link, sources in todo))
            new_pages = False
            for (link, _), entry in zip(todo, entries):
                if not entry.get("status"):
                    continue
                index[link] = entry
                saved = entry.get("saved_as")
                if saved and saved.lower().endswith((".html", ".htm")) and not link.startswith("/d2l/"):
                    self.pages[self.root / saved] = self.api.base_url + quote(unquote(link), safe="/+")
                    new_pages = True
            if not new_pages:
                break
        if index:
            self.out.mkdir(parents=True, exist_ok=True)
            with open(self.out / "_index.json", "w", encoding="utf-8") as f:
                json.dump(index, f, indent=2, ensure_ascii=False)
            print(f"[Linked files] {len(index)} link(s) handled, see linked_files/_index.json")
            if self.markdown:
                self._relink_markdown(index)

    async def _handle(self, link: str, sources: Set[str]) -> Dict:
        entry: Dict = {"found_in": sorted(sources)[:5], "found_in_count": len(sources)}
        if link.startswith("/d2l/"):
            await self._quicklink(link, entry)
        else:
            await self._enforced(link, entry)
        return entry

    def _relink_markdown(self, index: Dict[str, Dict]):
        """Point the links in the .md files at the local copies in linked_files/."""
        replacements = []
        host = self.api.base_url
        for link, entry in index.items():
            if entry.get("status") != "saved":
                continue
            target = self.root / entry["saved_as"]
            forms = {link, unquote(link), quote(unquote(link), safe="/+")}
            for form in forms:
                replacements += [(host + form, target), (form, target)]
        replacements.sort(key=lambda r: -len(r[0]))
        changed = 0
        for md in self.root.rglob("*.md"):
            if self.out in md.parents:
                continue
            text = md.read_text(encoding="utf-8")
            new = text
            for old, target in replacements:
                if old in new:
                    rel = os.path.relpath(target, md.parent).replace(os.sep, "/")
                    new = new.replace(old, quote(rel, safe="/+"))
            # the original links carried Brightspace query strings (?isCourseFile=true); a local file has none
            new = re.sub(r"(\]\([^)\s]*linked_files/[^)\s?]*)\?[^)\s]*\)", r"\1)", new)
            if new != text:
                md.write_text(new, encoding="utf-8")
                changed += 1
        if changed:
            print(f"[Linked files] {changed} .md file(s) now link to the local copies")

    def _target(self, path: str) -> Path:
        """/content/enforced/<course>/<sub/path/file.pdf> -> linked_files/<course>/<sub>/<path>/<file.pdf>"""
        parts = [clean_name(p) for p in unquote(path.split("?")[0]).split("/") if p][2:]
        return self.out.joinpath(*parts)

    async def _enforced(self, link: str, entry: Dict):
        path = unquote(re.split(r"[?#]", link)[0])
        ext = self._extension(path)
        if path.endswith("/") or not ext or path.lower() in self.in_toc:
            return  # a folder, a link without a file, or a file the TOC step already saved
        if self.markdown and ext in IMAGE_EXTENSIONS and self.only_img.get(link):
            return  # the .md export saved this <img> next to its page
        target = self._target(path)
        reason = "a page links to a course file that does not exist on the server (404)"
        saved = await self.api.download(quote(path, safe="/+"), target, absent_reason=reason)
        entry.update(status="saved" if saved else "unavailable",
                     saved_as=str(target.relative_to(self.root)) if saved else None)

    async def _quicklink(self, link: str, entry: Dict):
        """quickLink.d2l?type=coursefile&fileId=<name> redirects to the file in /content/enforced/."""
        resp = await self.api.get(link, stream=True,
                                  absent_reason="a page links to a course file that does not exist on the server (404)")
        if resp is None:
            entry["status"] = "unavailable"
            return
        final_url = str(resp.url)
        await resp.aclose()
        final = urlparse(final_url).path
        if "/content/enforced/" not in final:
            raise ScrapeError(f"{link}: expected a redirect to a course file, ended at {final_url}")
        target = self._target(final)
        saved = await self.api.download(final_url, target)
        entry.update(status="saved" if saved else "unavailable",
                     saved_as=str(target.relative_to(self.root)) if saved else None)
