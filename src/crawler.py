# src/crawler.py
"""Visits every GET route of the Brightspace API documentation (routes_manifest.json).

Identifiers in a route ((folderId), (postId), ...) come from the list route one level up, so a route is
only visited with ids that exist, and a 403 on a list route ends that whole branch. The routes the
hand-written steps in extras.py already fetched are not requested again. All routes are visited at the same
time (a route that needs the answer of another one fetches it itself, once). What is left of the manifest
after a run is reported per route in coverage.json, so a backup can be checked for completeness.
"""

import asyncio
import json
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import unquote, urlparse

from api import Api, ScrapeError, gather_all, path_of
from extras import CALENDAR_WINDOW, MEDIA_EXTENSIONS, clean_name

PLACEHOLDER = re.compile(r"^\((\w+)\)$")
# routes that return a file instead of JSON
BINARY = re.compile(r"(/attachments/\(fileId\)|/files/\(fileId\)|/file|/download|/image|/managefiles/file)$", re.I)
# routes where 404 only means "nothing there": no overview page, no pacing, no deleted items, ...
ABSENT_OK = re.compile(
    r"/(overview|overview/attachment|content/userprogress|content/userprogress/\(topicId\)|grades/final/values/myGradeValue"
    r"|grades/\(gradeObjectId\)/values/myGradeValue|news/deleted|calendar/events/occurrences|content/topics/\(topicId\)/file"
    r"|content/topics/\(topicId\)|sections/mysections|enrollments/orgUnits/\(orgUnitId\)/users/\(userId\)/completion"
    r"|dropbox/folders/\(folderId\)/submissions/user/\(userId\)|courses/\(orgUnitId\)/image)/?$", re.I)
# query strings a route cannot be called without (several = one request each)
WINDOW = CALENDAR_WINDOW
ORGS = "orgUnitIdsCSV={orgs}"
QUERY = [
    (re.compile(r"/calendar/events/user/?$"), ["userId={userId}&" + WINDOW]),
    (re.compile(r"/calendar/events/(myEvents|myEventsWithOccurrences)/?$"), [ORGS + "&" + WINDOW]),
    (re.compile(r"/calendar/events/orgunits/?$"), [ORGS + "&" + WINDOW]),
    (re.compile(r"/calendar/events/(occurrences/?|myEvents/itemCounts?/?|\(userId\)/itemCounts?)$"), [ORGS + "&" + WINDOW]),
    (re.compile(r"/content/completions/mycount/?$"), ["level=Topic", "level=OrgUnit"]),
    (re.compile(r"/content/completions/?$"), ["userIdsCSV={userId}"]),
    (re.compile(r"/content/completions/\(userId\)/?$"), [ORGS]),
    (re.compile(r"/content/items/\(userId\)$"), [ORGS]),
    (re.compile(r"/content/myItems/(completions/)?(due/)?(itemCounts?)?/?$"), [ORGS]),
    (re.compile(r"/grades/final/values/myGradeValues/?$"), [ORGS]),
    (re.compile(r"/overdueItems/?$"), ["userId={userId}&" + ORGS]),
    (re.compile(r"/updates/myUpdates/?$"), [ORGS]),
]
# a 400 that only says "this does not apply here"
NOT_APPLICABLE = ("not attached to a source system", "Method Not Allowed")


def _items(data: Any) -> List[Any]:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("Objects", "Items", "Contents", "Results"):
            if isinstance(data.get(key), list):
                return data[key]
        return [data]
    return []


def _id_of(item: Any, name: str) -> Any:
    """The identifier a list item carries for the placeholder `name` (folderId -> FolderId, Id, ...)."""
    if not isinstance(item, dict):
        return None
    wanted = name.lower()
    entity = wanted[:-2] if wanted.endswith("id") else wanted
    for key in item:
        if key.lower() == wanted:
            return item[key]
    for key in item:
        if key.lower().endswith(entity + "id") and not isinstance(item[key], (dict, list)):
            return item[key]
    for key in ("Id", "Identifier"):
        if key in item and not isinstance(item[key], (dict, list)):
            return item[key]
    return None


def _scan(data: Any, key: str) -> Iterable[Any]:
    """Every value stored under `key` anywhere inside data."""
    if isinstance(data, dict):
        for k, v in data.items():
            if k == key and not isinstance(v, (dict, list)):
                yield v
            else:
                yield from _scan(v, key)
    elif isinstance(data, list):
        for v in data:
            yield from _scan(v, key)


class Crawler:

    def __init__(self, api: Api, root: Path, user_id: int, course_id: Optional[str] = None,
                 toc: Optional[Dict] = None, org_ids: Optional[List[str]] = None):
        self.api = api
        self.root = root
        self.user_id = user_id
        self.course_id = course_id
        self.orgs = ",".join(org_ids or ([course_id] if course_id else []))
        self.reasons: Dict[str, str] = {}      # template -> why it could not be visited
        self.failures: Dict[str, str] = {}     # concrete path -> error
        self.needs_params: Dict[str, str] = {}  # concrete path -> the 400 answer
        self.topics: List[Dict] = []
        self.modules: List[Dict] = []
        self._loading: Dict[str, "asyncio.Task"] = {}   # a route that several routes need is fetched once
        self._fetching: Dict[Any, "asyncio.Task"] = {}  # every request is made once
        if toc:
            self._walk(toc.get("Modules", []))

    def _walk(self, modules: List[Dict]):
        for m in modules:
            self.modules.append(m)
            self.topics.extend(m.get("Topics", []))
            self._walk(m.get("Modules", []))

    # ---------- driver ----------

    async def run(self, scope_course: bool = True) -> Dict[str, str]:
        """Visit the routes of the manifest that belong to a course (or, with scope_course=False, the rest)."""
        templates = [t for t in self.api.matcher.templates if ("(orgUnitId)" in t) == scope_course]
        if scope_course and not self.course_id:
            return {}
        await gather_all(*(self._visit(tpl) for tpl in sorted(templates, key=lambda t: (t.count("/"), t))))
        return self.reasons

    async def _visit(self, tpl: str):
        if tpl.rstrip("/").endswith(("/rubrics", "/assessment")) and "(orgUnitId)" in tpl:
            await self._visit_rubric_route(tpl)
            return
        try:
            bindings, reason = await self._bindings(tpl)
        except ScrapeError as e:
            self.reasons[tpl] = f"parent route failed: {e}"
            return
        if reason:
            self.reasons[tpl] = reason
        todo = [(self._fill(tpl, b), b) for b in bindings]
        await gather_all(*(self._fetch(path, tpl, b) for path, b in todo if path.rstrip("/") not in self.api.ledger))

    async def _rubric_pairs(self) -> List[Tuple[str, Any]]:
        assoc = await self._load(self._fill("/d2l/api/le/(version)/(orgUnitId)/rubrics/associations/", {}))
        return [(a["ActivityType"], a["ObjectId"]) for item in _items(assoc) for a in item.get("Associations") or []]

    async def _visit_rubric_route(self, tpl: str):
        """rubrics/ and assessment need an activity (and a rubric) that has a rubric attached."""
        pairs = await self._rubric_pairs()
        if not pairs:
            self.reasons[tpl] = "empty: no activity in this course has a rubric (or the association list is forbidden)"
            return
        base = self._fill(tpl, {})

        async def one(kind: str, oid: Any):
            if tpl.rstrip("/").endswith("/rubrics"):
                await self._fetch(base, tpl, {}, query=f"objectType={kind}&objectId={oid}", tag=f"{kind}_{oid}")
                return
            rubrics = await self.api.json(self._fill("/d2l/api/le/(version)/(orgUnitId)/rubrics/", {})
                                          + f"?objectType={kind}&objectId={oid}") or []
            await gather_all(*(
                self._fetch(base, tpl, {}, tag=f"{kind}_{oid}_{rubric.get('RubricId')}",
                            query=f"assessmentType=Rubric&objectType={kind}&objectId={oid}"
                                  f"&rubricId={rubric.get('RubricId')}&userId={self.user_id}")
                for rubric in _items(rubrics)))

        await gather_all(*(one(kind, oid) for kind, oid in pairs))

    # ---------- filling templates ----------

    def _version(self, tpl: str, binding: Dict) -> str:
        product = tpl.replace("/d2l/api", "").strip("/").split("/")[0]
        if product == "(productCode)":
            product = str(binding.get("productCode"))
        latest = {k.lower(): v for k, v in self.api.versions.items()}
        return latest.get(product.lower(), "1.0")

    def _fill(self, tpl: str, binding: Dict[str, Any]) -> str:
        values = {"version": self._version(tpl, binding), "orgUnitId": self.course_id, "userId": self.user_id, **binding}
        return PLACEHOLDER_ALL.sub(lambda m: str(values[m.group(1)]), tpl)  # keeps the template's trailing slash

    async def _bindings(self, tpl: str) -> Tuple[List[Dict], Optional[str]]:
        """Every combination of identifiers the route can be visited with (and why there are none)."""
        segs = tpl.replace("/d2l/api", "").strip("/").split("/")
        partial: List[Dict] = [{}]
        state = ""
        for i, seg in enumerate(segs):
            m = PLACEHOLDER.match(seg)
            if not m or m.group(1) in ("version", "orgUnitId", "userId"):
                continue
            name = m.group(1)
            found = await gather_all(*(self._candidates(tpl, segs, i, name, b) for b in partial))
            expanded = []
            for b, (values, state) in zip(partial, found):
                expanded += [{**b, name: v} for v in values]
            if not expanded:
                return [], self._why_empty(name, state)
            partial = expanded
        return partial, None

    @staticmethod
    def _why_empty(name: str, state: str) -> str:
        if state == "blocked":
            return f"blocked: the list of {name}s is forbidden or unavailable"
        if state == "empty":
            return f"empty: there are no {name}s to visit"
        return f"no source for {name}"

    async def _candidates(self, tpl: str, segs: List[str], i: int, name: str, b: Dict) -> Tuple[List[Any], str]:
        """The identifiers for placeholder `name`, and what its parent route said ("blocked", "empty" or "")."""
        prefix = "/d2l/api/" + "/".join(segs[:i])
        full = "/d2l/api/" + "/".join(segs[:i + 1])
        special = await self._special(tpl, full, name, b)
        if special is not None:
            return special, ""
        if name in ("fileId", "linkId"):
            parent = prefix.rsplit("/", 1)[0]
            data = await self._load(self._fill(parent, b))
            found = sorted({v for v in _scan(data, "FileId" if name == "fileId" else "LinkId")}, key=str)
            return found, "blocked" if data is None else "empty"
        data = await self._load(self._fill(prefix, b))
        ids = [_id_of(item, name) for item in _items(data)]
        return list(dict.fromkeys(v for v in ids if v is not None)), "blocked" if data is None else "empty"

    async def _special(self, tpl: str, full: str, name: str, b: Dict) -> Optional[List[Any]]:
        """Identifiers that no list route hands out."""
        if re.search(r"/content/topics/\(topicId\)", full) and name == "topicId":
            topics = self.topics
            if "/captions" in tpl:  # only video topics have captions
                topics = [t for t in topics if self._extension(t) in MEDIA_EXTENSIONS]
            elif tpl.endswith("/file"):  # only file topics with a path have a file
                topics = [t for t in topics if t.get("ActivityType") == 1 and not (t.get("Url") or "").startswith("d2l:")]
            return [t.get("TopicId") or t.get("Id") for t in topics]
        if name == "moduleId":
            return [m.get("ModuleId") or m.get("Id") for m in self.modules]
        if name in ("entityType", "entityId", "submissionId"):
            entries = await self._load(self._fill(
                "/d2l/api/le/(version)/(orgUnitId)/dropbox/folders/(folderId)/submissions/mysubmissions/", b))
            if name == "submissionId":
                return [s["Id"] for e in _items(entries) for s in e.get("Submissions") or []]
            entities = [(e.get("Entity") or {}) for e in _items(entries)]
            if name == "entityType":
                return list(dict.fromkeys(e["EntityType"] for e in entities if "EntityType" in e))
            return [e["EntityId"] for e in entities if e.get("EntityType") == b.get("entityType") and "EntityId" in e]
        if name in ("activityType", "objectId") and ("/rubrics/" in full or "/lo/alignments/" in full):
            pairs = await self._rubric_pairs()
            if name == "activityType":
                return list(dict.fromkeys(p[0] for p in pairs))
            return [p[1] for p in pairs if p[0] == b.get("activityType")]
        if name == "groupId" and "/dropbox/" in full:
            groups = []
            for path, data in list(self.api.seen.items()):
                if re.search(r"/groupcategories/\d+/groups/?$", path):
                    groups += [g["GroupId"] for g in _items(data) if self.user_id in (g.get("Enrollments") or [])]
            return groups
        if name == "rubricId":
            lists = await gather_all(*(
                self.api.json(self._fill("/d2l/api/le/(version)/(orgUnitId)/rubrics/", {})
                              + f"?objectType={kind}&objectId={oid}")
                for kind, oid in await self._rubric_pairs()))
            ids = [r.get("RubricId") for rubrics in lists for r in _items(rubrics)]
            return [i for i in dict.fromkeys(ids) if i is not None]
        if name == "productCode":
            return list(self.api.versions)
        if name in ("jobToken", "customWidgetId", "variableId", "path"):
            return []
        return None

    @staticmethod
    def _extension(topic: Dict) -> str:
        return Path(unquote(urlparse(topic.get("Url") or "").path)).suffix.lower()

    async def _load(self, path: str) -> Any:
        """Response of a route, fetched when no step asked for it yet. None if it is not available."""
        key = path.rstrip("/")
        if key in self.api.seen:
            return self.api.seen[key]
        tpl = self.api.matcher.match(path)
        if not tpl:
            return None
        if key not in self._loading:
            self._loading[key] = asyncio.ensure_future(self._fetch(key + ("/" if tpl.endswith("/") else ""), tpl, {}))
        await self._loading[key]
        return self.api.seen.get(key)

    # ---------- fetching ----------

    def _queries(self, tpl: str) -> List[str]:
        for pattern, queries in QUERY:
            if pattern.search(tpl):
                return [q.format(orgUnitId=self.course_id, userId=self.user_id, orgs=self.orgs) for q in queries]
        return [""]

    def _raw_path(self, path: str, suffix: str) -> Path:
        rest = path.replace("/d2l/api/", "").strip("/")
        parts = [clean_name(p) for p in rest.split("/")]
        return self.root / "raw" / Path(*parts[:-1]) / (parts[-1] + suffix)

    async def _fetch(self, path: str, tpl: str, binding: Dict, query: Optional[str] = None, tag: str = ""):
        """Request a route once, however many routes ask for it."""
        key = (path, query, tag)
        if key not in self._fetching:
            self._fetching[key] = asyncio.ensure_future(self._fetch_now(path, tpl, query, tag))
        await self._fetching[key]

    async def _fetch_now(self, path: str, tpl: str, query: Optional[str], tag: str):
        queries = [query] if query is not None else self._queries(tpl)
        for q in queries:
            endpoint = path + (("&" if "?" in path else "?") + q if q else "")
            label = tag or (q.split("=")[-1] if len(queries) > 1 else "")
            try:
                if tpl.endswith("/content/topics/(topicId)/file"):
                    # the TOC step already saved this file under its module: only check that the route answers
                    resp = await self.api.get(endpoint, absent_ok=True, stream=True)
                    if resp is not None:
                        await resp.aclose()
                    continue
                if BINARY.search(tpl):
                    await self.api.download(endpoint, self._raw_path(path, ""), absent_ok=True)
                    continue
                data = await self.api.json(endpoint, absent_ok=True)  # 404 on a derived route: nothing there
                if isinstance(data, dict) and (data.get("Next") or (data.get("PagingInfo") or {}).get("HasMoreItems")):
                    data = await self.api.paged(endpoint)
                if data is not None:
                    target = self._raw_path(path, (f".{clean_name(label)}" if label else "") + ".json")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with open(target, "w", encoding="utf-8") as f:
                        json.dump(data, f, indent=2, ensure_ascii=False)
            except ScrapeError as e:
                message = str(e)
                key = path.rstrip("/")
                if any(text in message for text in NOT_APPLICABLE):
                    self.api.ledger[key] = "n/a"
                elif "HTTP 400" in message:
                    # the route wants parameters that no response of this account hands out
                    self.api.ledger[key] = "needs-params"
                    self.needs_params[path] = message[-200:]
                elif "not JSON" in message:
                    await self._save_text(endpoint, path)
                else:
                    self.failures[path] = message

    async def _save_text(self, endpoint: str, path: str):
        resp = await self.api.get(endpoint)
        if resp is not None:
            target = self._raw_path(path, ".txt")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(resp.text, encoding="utf-8")

    # ---------- report ----------

    def coverage(self, scope_course: bool = True) -> Dict[str, Any]:
        """How every documented route fared in this run."""
        api = self.api
        excluded = json.loads((Path(__file__).with_name("routes_manifest.json")).read_text(encoding="utf-8")).get("excluded", {})
        by_template: Dict[str, List[Any]] = {}
        for path, status in api.ledger.items():
            tpl = api.matcher.match(path)
            if tpl:
                by_template.setdefault(tpl, []).append(status)
        report: Dict[str, str] = {}
        for tpl in api.matcher.templates:
            if ("(orgUnitId)" in tpl) != scope_course:
                continue
            statuses = by_template.get(tpl, [])
            if 200 in statuses:
                report[tpl] = f"ok ({statuses.count(200)} of {len(statuses)} requests)"
            elif any(s in (403, "cached 403") for s in statuses):
                report[tpl] = "forbidden (403)"
            elif statuses and all(s == 404 for s in statuses):
                report[tpl] = "absent (404: nothing there)"
            elif statuses and all(s == "needs-params" for s in statuses):
                report[tpl] = "needs-params (HTTP 400: required query parameters cannot be derived)"
            elif statuses and all(s == "n/a" for s in statuses):
                report[tpl] = "not-applicable (the server says this feature is not set up)"
            elif statuses:
                report[tpl] = f"error: HTTP {sorted(set(map(str, statuses)))}"
            elif tpl in excluded:
                report[tpl] = f"excluded: {excluded[tpl]}"
            elif self.reasons.get(tpl, "").startswith("empty"):
                report[tpl] = f"empty ({self.reasons[tpl][7:]})"
            elif self.reasons.get(tpl, "").startswith("blocked"):
                report[tpl] = f"forbidden (parent: {self.reasons[tpl][9:]})"
            elif self.reasons.get(tpl, "").startswith("no source"):
                report[tpl] = f"needs-params ({self.reasons[tpl]}: no response of this account hands it out)"
            elif tpl in self.reasons:
                report[tpl] = f"not visited: {self.reasons[tpl]}"
            else:
                report[tpl] = "not visited"
        return report


PLACEHOLDER_ALL = re.compile(r"\((\w+)\)")
