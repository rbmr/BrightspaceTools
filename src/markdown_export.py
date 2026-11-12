# src/markdown_export.py
"""Turns the HTML Brightspace hands out into Markdown files, downloading the images they reference."""

import asyncio
import re
from pathlib import Path
from typing import Dict, Optional
from urllib.parse import unquote, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from markdownify import markdownify

from api import Api, ScrapeError, cap_name


def _clean_name(name: str) -> str:
    return cap_name(re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', name).strip().rstrip('.')) or "unnamed"


class MarkdownExporter:

    def __init__(self, api: Api):
        self.api = api
        self.base_url = api.base_url
        self._host = urlparse(self.base_url).netloc
        self._downloaded: Dict[str, Path] = {}  # absolute image URL -> saved path
        self._url_locks: Dict[str, asyncio.Lock] = {}  # one download per image URL at a time
        # third-party hosts: not part of the Brightspace API, and we never send our tokens there
        self._plain = httpx.AsyncClient(timeout=60, follow_redirects=True)

    async def aclose(self):
        await self._plain.aclose()

    # ---------- public API ----------

    async def convert_file(self, html_path: Path, page_url: Optional[str] = None) -> Optional[Path]:
        """Write <name>.md next to a saved .html file. Returns the .md path."""
        md_path = html_path.with_suffix(".md")
        if md_path.exists():
            return md_path
        html = html_path.read_bytes().decode("utf-8", errors="replace")
        return await self.html_to_file(html, md_path, page_url=page_url)

    async def html_to_file(self, html: Optional[str], md_path: Path, page_url: Optional[str] = None,
                           title: Optional[str] = None) -> Optional[Path]:
        if not html or not html.strip():
            return None
        if md_path.exists():
            return md_path
        text = await self._convert(html, md_path, page_url or f"{self.base_url}/", title)
        md_path.parent.mkdir(parents=True, exist_ok=True)
        md_path.write_text(text, encoding="utf-8")
        return md_path

    # ---------- conversion ----------

    async def _convert(self, html: str, md_path: Path, page_url: str, title: Optional[str]) -> str:
        soup = BeautifulSoup(html, "html.parser")
        page_title = title or (soup.title.get_text(strip=True) if soup.title else None)

        for tag in soup(["script", "style", "noscript", "head", "link", "meta", "iframe"]):
            tag.decompose()

        for a in soup.find_all("a", href=True):
            a["href"] = self._absolute(a["href"], page_url)

        images_dir = md_path.parent / f"{md_path.stem}_images"
        images = soup.find_all("img", src=True)
        links = await asyncio.gather(*(self._image_link(img["src"], page_url, images_dir, md_path.parent)
                                       for img in images))
        for img, link in zip(images, links):
            img["src"] = link

        body = soup.body or soup
        text = markdownify(str(body), heading_style="ATX", bullets="-").strip()
        text = re.sub(r"\n{3,}", "\n\n", text)
        if page_title and not text.lstrip().startswith("#"):
            text = f"# {page_title}\n\n{text}"
        return text + "\n"

    def _absolute(self, href: str, page_url: str) -> str:
        if href.startswith(("#", "mailto:", "tel:", "javascript:", "data:")):
            return href
        return urljoin(page_url, href)

    # ---------- images ----------

    async def _image_link(self, src: str, page_url: str, images_dir: Path, md_dir: Path) -> str:
        if src.startswith("data:"):
            return src
        url = urljoin(page_url, src)
        saved = await self._download_image(url, images_dir)
        if saved is None:
            return url  # keep a working absolute link when the download fails
        return saved.relative_to(md_dir).as_posix().replace(" ", "%20")

    async def _download_image(self, url: str, images_dir: Path) -> Optional[Path]:
        async with self._url_locks.setdefault(url, asyncio.Lock()):
            if url in self._downloaded and self._downloaded[url].exists():
                return self._copy_into(self._downloaded[url], images_dir)
            if urlparse(url).netloc == self._host:
                # 403 -> permission warning; 404 -> the page links to an image that no longer exists
                resp = await self.api.get(url, absent_reason="image referenced by a page does not exist (404)")
                if resp is None:
                    return None
                if not resp.headers.get("Content-Type", "").startswith("image/"):
                    raise ScrapeError(f"{url}: expected an image, got {resp.headers.get('Content-Type')}")
            else:
                # a broken image on a third-party host is only a warning
                try:
                    resp = await self._plain.get(url)
                except httpx.HTTPError as e:
                    print(f"    [Warn] external image {url}: {e}")
                    return None
                if resp.status_code != 200 or not resp.headers.get("Content-Type", "").startswith("image/"):
                    print(f"    [Warn] external image {url}: HTTP {resp.status_code}")
                    return None
            content_type = resp.headers.get("Content-Type", "")

            images_dir.mkdir(parents=True, exist_ok=True)
            path = self._unique_path(images_dir, self._filename(url, content_type))
            path.write_bytes(resp.content)
            self._downloaded[url] = path
            print(f"    [Image] {path.name}")
            return path

    def _copy_into(self, existing: Path, images_dir: Path) -> Path:
        """The same image used by another page: copy it so each .md stays self-contained."""
        if existing.parent == images_dir:
            return existing
        images_dir.mkdir(parents=True, exist_ok=True)
        target = self._unique_path(images_dir, existing.name)
        target.write_bytes(existing.read_bytes())
        return target

    @staticmethod
    def _filename(url: str, content_type: str) -> str:
        name = _clean_name(unquote(urlparse(url).path.split("/")[-1])) or "image"
        if "." not in name:
            ext = content_type.split("/")[-1].split(";")[0].replace("jpeg", "jpg").replace("svg+xml", "svg")
            name = f"{name}.{ext}"
        return name

    @staticmethod
    def _unique_path(directory: Path, name: str) -> Path:
        path = directory / name
        stem, suffix, n = path.stem, path.suffix, 1
        while path.exists():
            path = directory / f"{stem}_{n}{suffix}"
            n += 1
        return path
