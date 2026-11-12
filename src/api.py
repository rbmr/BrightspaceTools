# src/api.py
"""Asynchronous HTTP access to Brightspace with one error policy for the whole scraper.

* 403 (no permission)  -> warning, recorded, the call returns None
* 404 on a route where "not found" just means "not set" (absent_ok) -> None
* 404 on content the course itself advertises (absent_reason) -> recorded as unavailable, returns None
* everything else (400, 401, 404, 5xx, network errors, bad JSON) -> ScrapeError

Requests run concurrently; the number of connections open at once is the limit of the httpx client.
"""

import asyncio
import json as jsonlib
import re
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

import httpx

RETRIES = 4
TRANSIENT_STATUS = {429, 500, 502, 503, 504}
# a route family that answers 403 this many times without a single success is not asked again
FORBIDDEN_LIMIT = 3
MANIFEST = Path(__file__).with_name("routes_manifest.json")


class ScrapeError(Exception):
    pass


MAX_NAME = 180  # Windows allows 255 per path component, and we add suffixes like " [123]" and ".part"


def cap_name(name: str) -> str:
    """Shorten an overlong file or folder name, keeping its extension."""
    if len(name) <= MAX_NAME:
        return name
    stem, dot, ext = name.rpartition(".")
    if dot and 0 < len(ext) <= 10:
        return stem[:MAX_NAME - len(ext) - 1].rstrip() + "." + ext
    return name[:MAX_NAME].rstrip()


async def gather_all(*awaitables) -> List[Any]:
    """asyncio.gather that lets every task finish before it raises the first error, so no task is left running
    in the background while the caller moves on."""
    results = await asyncio.gather(*awaitables, return_exceptions=True)
    for result in results:
        if isinstance(result, BaseException):
            raise result
    return list(results)


def path_of(endpoint: str) -> str:
    """The path of an endpoint without host and query."""
    return urlsplit(endpoint).path.rstrip("/")


class RouteMatcher:
    """Maps a concrete path (/d2l/api/le/1.100/123/news/4) to its documented route template."""

    def __init__(self, templates: List[str]):
        self.templates = templates
        compiled = []
        for tpl in templates:
            segs = [s for s in tpl.replace("/d2l/api", "").strip("/").split("/")]
            parts = []
            for seg in segs:
                if seg == "(version)":
                    parts.append(r"\d+\.\d+")
                elif seg == "(path)":
                    parts.append(r".+")
                elif seg.startswith("("):
                    parts.append(r"[^/]+")
                else:
                    parts.append(re.escape(seg))
            literals = sum(1 for s in segs if not s.startswith("("))
            compiled.append((-literals, len(segs), re.compile("/".join(parts) + "$", re.IGNORECASE), tpl))
        self._compiled = sorted(compiled, key=lambda c: c[:2])
        self._cache: Dict[str, Optional[str]] = {}

    def match(self, path: str) -> Optional[str]:
        key = path.replace("/d2l/api", "").strip("/")
        if key not in self._cache:
            self._cache[key] = next((c[3] for c in self._compiled if c[2].match(key)), None)
        return self._cache[key]


class _Probe:
    """The first few requests of a route family run one after the other, so that a family that is forbidden
    is recognised before hundreds of identical requests are on their way."""

    def __init__(self):
        self.started = 0
        self.finished = 0
        self.lock = asyncio.Lock()
        self.verdict = asyncio.Event()


class Api:

    def __init__(self, client: httpx.AsyncClient, base_url: str):
        self.client = client
        self.base_url = base_url.rstrip("/")
        self.warnings: List[Dict[str, Any]] = []
        self.unavailable: List[Dict[str, Any]] = []
        self.le_version = "1.82"
        self.lp_version = "1.49"
        self.versions: Dict[str, str] = {}
        manifest = jsonlib.loads(MANIFEST.read_text(encoding="utf-8")) if MANIFEST.exists() else {"routes": []}
        self.matcher = RouteMatcher(manifest["routes"])
        # per course: concrete path -> status of the last request ("cached 403" for skipped ones), parsed JSON
        self.ledger: Dict[str, Any] = {}
        self.seen: Dict[str, Any] = {}
        # per course (permissions can depend on the object): route template -> [403 count, success count]
        self.scope = ""
        self.reprobe = False
        self._outcomes: Dict[Any, List[int]] = {}
        self._probes: Dict[Any, _Probe] = {}
        self._downloads: Dict[Path, "asyncio.Task[Optional[Path]]"] = {}

    def reset_course(self):
        self._outcomes.clear()
        self._probes.clear()
        self.warnings.clear()
        self.unavailable.clear()
        self.ledger.clear()
        self.seen.clear()

    # ---------- versions ----------

    async def discover_versions(self):
        """Use the newest LE/LP version this server supports."""
        data = await self.json("/d2l/api/versions/")
        self.versions = {v["ProductCode"]: v["LatestVersion"] for v in data or []}
        self.le_version = self.versions.get("le", self.le_version)
        self.lp_version = self.versions.get("lp", self.lp_version)

    def le(self, org_unit: Any, path: str = "") -> str:
        return f"/d2l/api/le/{self.le_version}/{org_unit}{path}"

    def lp(self, path: str) -> str:
        return f"/d2l/api/lp/{self.lp_version}{path}"

    # ---------- requests ----------

    def _url(self, endpoint: str) -> str:
        return endpoint if endpoint.startswith("http") else f"{self.base_url}{endpoint}"

    async def _request(self, endpoint: str, stream: bool = False, **kwargs) -> httpx.Response:
        """One GET with retries. The response body is read, unless stream is set (then the caller closes it)."""
        url = self._url(endpoint)
        last: Optional[str] = None
        for attempt in range(RETRIES):
            try:
                resp = await self.client.send(self.client.build_request("GET", url, **kwargs), stream=True)
            except httpx.HTTPError as e:
                last = f"{type(e).__name__}: {e}"
            else:
                if resp.status_code in TRANSIENT_STATUS:
                    last = f"HTTP {resp.status_code}"
                    delay = resp.headers.get("Retry-After", "")
                    await resp.aclose()
                    if delay.isdigit():
                        await asyncio.sleep(min(int(delay), 60))
                        continue
                elif stream:
                    return resp
                else:
                    try:
                        await resp.aread()
                        return resp
                    except httpx.HTTPError as e:
                        last = f"{type(e).__name__}: {e}"
                        await resp.aclose()
            await asyncio.sleep(2 ** attempt)
        raise ScrapeError(f"{endpoint}: giving up after {RETRIES} attempts ({last})")

    async def _body(self, resp: httpx.Response) -> str:
        if not resp.is_closed:
            await resp.aread()
        return resp.text

    async def _permission_warning(self, endpoint: str, resp: httpx.Response):
        detail = (await self._body(resp))[:200].replace("\n", " ")
        self.warnings.append({"endpoint": endpoint, "status": resp.status_code, "detail": detail})
        print(f"  [Permission] {endpoint}")

    def note_unavailable(self, endpoint: str, reason: str):
        self.unavailable.append({"endpoint": endpoint, "reason": reason})
        print(f"  [Unavailable] {reason}: {endpoint}")

    async def get(self, endpoint: str, absent_ok: bool = False, stream: bool = False,
                  absent_reason: Optional[str] = None, **kwargs) -> Optional[httpx.Response]:
        """The response of a GET, or None when it is not permitted / not there. With stream, close the response."""
        key = path_of(endpoint)
        template = self.matcher.match(key)
        outcome = self._outcomes.setdefault((self.scope, template), [0, 0])
        if template and not self.reprobe:
            probe = self._probes.setdefault((self.scope, template), _Probe())
            if not probe.verdict.is_set() and probe.started < FORBIDDEN_LIMIT:
                probe.started += 1
                async with probe.lock:
                    try:
                        if not probe.verdict.is_set():
                            return await self._get(endpoint, key, outcome, absent_ok, stream, absent_reason, **kwargs)
                    finally:
                        probe.finished += 1
                        if outcome[1] > 0 or outcome[0] >= FORBIDDEN_LIMIT or probe.finished >= FORBIDDEN_LIMIT:
                            probe.verdict.set()
            else:
                await probe.verdict.wait()
            if outcome[0] >= FORBIDDEN_LIMIT and outcome[1] == 0:
                # this route family has only ever been forbidden for this role: do not ask again
                self.ledger[key] = "cached 403"
                self.warnings.append({"endpoint": endpoint, "status": 403,
                                      "detail": "skipped: route family is forbidden", "cached": True})
                return None
        return await self._get(endpoint, key, outcome, absent_ok, stream, absent_reason, **kwargs)

    async def _get(self, endpoint: str, key: str, outcome: List[int], absent_ok: bool, stream: bool,
                   absent_reason: Optional[str], **kwargs) -> Optional[httpx.Response]:
        resp = await self._request(endpoint, stream=stream, **kwargs)
        self.ledger[key] = resp.status_code
        if resp.status_code == 200:
            outcome[1] += 1
            return resp
        if resp.status_code == 403:
            outcome[0] += 1
            await self._permission_warning(endpoint, resp)
            await resp.aclose()
            return None
        if resp.status_code == 404 and (absent_ok or absent_reason):
            await resp.aclose()
            if absent_reason:
                self.note_unavailable(endpoint, absent_reason)
            return None
        status, body = resp.status_code, (await self._body(resp))[:300].replace("\n", " ")
        await resp.aclose()
        hint = " (session expired? run auth.py again)" if status == 401 else ""
        raise ScrapeError(f"{endpoint}: HTTP {status}{hint} {body}")

    async def json(self, endpoint: str, absent_ok: bool = False, absent_reason: Optional[str] = None) -> Any:
        resp = await self.get(endpoint, absent_ok=absent_ok, absent_reason=absent_reason)
        if resp is None:
            return None
        try:
            data = resp.json()
        except ValueError as e:
            raise ScrapeError(f"{endpoint}: response is not JSON ({e})")
        self.seen[path_of(endpoint)] = data
        return data

    async def paged(self, endpoint: str, absent_ok: bool = False) -> Optional[List[Dict]]:
        """All items of a list route: plain lists, {Objects, Next} and {Items, PagingInfo}. None if not permitted."""
        items: List[Dict] = []
        sep = "&" if "?" in endpoint else "?"
        next_url: Optional[str] = endpoint
        seen = set()
        while next_url and next_url not in seen:
            seen.add(next_url)
            data = await self.json(next_url, absent_ok=absent_ok)
            if data is None:
                return items or None
            if isinstance(data, list):
                items += data
                break
            if "Objects" in data:
                items += data["Objects"]
                next_url = data.get("Next") or None
            elif "Items" in data:
                items += data["Items"]
                paging = data.get("PagingInfo") or {}
                bookmark = paging.get("Bookmark")
                next_url = f"{endpoint}{sep}bookmark={bookmark}" if paging.get("HasMoreItems") and bookmark else None
            else:
                items.append(data)
                break
        self.seen[path_of(endpoint)] = items
        return items

    async def download(self, endpoint: str, path: Path, absent_ok: bool = False,
                       absent_reason: Optional[str] = None) -> Optional[Path]:
        """Stream a file to disk. Skips files that already exist; the same target is only downloaded once."""
        if path.exists():
            print(f"    [Skip] exists: {path.name}")
            self.ledger[path_of(endpoint)] = 200  # fetched by an earlier run
            return path
        running = self._downloads.get(path)
        if running is not None:
            return await running  # another task is fetching this very file
        task = asyncio.ensure_future(self._download(endpoint, path, absent_ok, absent_reason))
        self._downloads[path] = task
        try:
            return await task
        finally:
            self._downloads.pop(path, None)

    async def _download(self, endpoint: str, path: Path, absent_ok: bool, absent_reason: Optional[str]) -> Optional[Path]:
        resp = await self.get(endpoint, absent_ok=absent_ok, stream=True, absent_reason=absent_reason)
        if resp is None:
            return None
        path.parent.mkdir(parents=True, exist_ok=True)
        part = path.with_name(path.name + ".part")
        try:
            try:
                with open(part, "wb") as f:
                    async for chunk in resp.aiter_bytes(65536):
                        f.write(chunk)
            finally:
                await resp.aclose()
        except httpx.HTTPError as e:
            part.unlink(missing_ok=True)
            raise ScrapeError(f"{endpoint}: download interrupted ({e})")
        part.replace(path)
        print(f"    [Download] {path.name}")
        return path
