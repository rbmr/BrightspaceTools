# src/extras.py
"""Everything attached to a course that the content table of contents does not list.

Error policy (see api.py): a 403 is recorded as a permission warning, any other failure aborts the course.
Every step fetches its routes concurrently, and the steps themselves run side by side.
"""

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import quote, unquote, urlparse

from api import Api, cap_name, gather_all
from markdown_export import MarkdownExporter

CALENDAR_WINDOW = "startDateTime=1990-01-01T00:00:00.000Z&endDateTime=2100-01-01T00:00:00.000Z"
MEDIA_EXTENSIONS = {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi", ".mp3", ".wav", ".m4a"}
LOCKER_FOLDER, LOCKER_FILE = 0, 1


def clean_name(name: Any) -> str:
    return cap_name(re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', str(name)).strip().rstrip('.')) or "unnamed"


def _labelled(name: Any, ident: Any) -> str:
    return f"{clean_name(name)} [{ident}]"


class CourseExtras:

    def __init__(self, api: Api, course_id: str, root: Path, toc: Dict, user_id: int,
                 md: Optional[MarkdownExporter] = None):
        self.api = api
        self.course_id = course_id
        self.root = root
        self.toc = toc
        self.user_id = user_id
        self.md = md

    # ---------- helpers ----------

    def le(self, path: str = "") -> str:
        return self.api.le(self.course_id, path)

    def lp(self, path: str = "") -> str:
        return self.api.lp(f"/{self.course_id}{path}")

    def _save(self, relative: str, data: Any):
        if data is None:
            return
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

    async def _save_from(self, relative: str, awaitable):
        """Fetch, then save the result as JSON."""
        self._save(relative, await awaitable)

    async def _md(self, rich: Any, path: Path, title: Optional[str] = None, header: str = ""):
        """Markdown copy of a rich-text field ({'Html': ...} or an HTML string); only with --markdown."""
        if not self.md:
            return
        html = rich.get("Html") if isinstance(rich, dict) else rich
        if not html or not html.strip():
            return
        if header:
            html = f"<p>{header}</p>{html}"
        await self.md.html_to_file(html, path, title=title)

    async def _download_files(self, files: Optional[List[Dict]], endpoint_for: Callable[[Dict], str], directory: Path):
        await gather_all(*(
            self.api.download(endpoint_for(file), directory / clean_name(file.get("FileName") or f"file_{file['FileId']}"))
            for file in files or [] if file.get("FileId") is not None
        ))

    # ---------- entry point ----------

    async def run(self):
        steps = [
            ("course info", self.course_info),
            ("content metadata", self.content_meta),
            ("dropbox", self.dropbox),
            ("rubrics", self.rubrics),
            ("news", self.news),
            ("discussions", self.discussions),
            ("quizzes", self.quizzes),
            ("surveys", self.surveys),
            ("checklists", self.checklists),
            ("grades", self.grades),
            ("calendar", self.calendar),
            ("groups and lockers", self.groups),
            ("outcomes", self.outcomes),
        ]

        async def run_step(label: str, step: Callable):
            print(f"[Extras] {label}")
            await step()

        await gather_all(*(run_step(label, step) for label, step in steps))

    # ---------- course level ----------

    async def course_info(self):
        api, cid = self.api, self.course_id
        le_version = api.le_version

        async def enrollment():
            enrolled, access, parents = await gather_all(
                api.json(api.lp(f"/enrollments/myenrollments/{cid}")),
                api.json(api.lp(f"/enrollments/myenrollments/{cid}/access")),
                api.paged(api.lp(f"/enrollments/myenrollments/{cid}/parentOrgUnits")),
            )
            self._save("course/enrollment.json", {"enrollment": enrolled, "access": access, "parent_org_units": parents})

        async def overview():
            data = await api.json(self.le("/overview"), absent_ok=True)
            self._save("course/overview.json", data)
            if data:
                await self._md(data.get("Description") or data.get("Html"), self.root / "course" / "overview.md",
                               title="Course overview")

        await gather_all(
            enrollment(),
            self._save_from("course/course.json", api.json(api.lp(f"/courses/{cid}"))),  # 403 for students
            self._save_from("course/tools.json", api.paged(api.lp(f"/tools/orgUnits/{cid}/toolNames"))),
            self._save_from("course/updates.json", api.json(self.le("/updates/myUpdates"))),
            self._save_from("course/accommodations.json",
                            api.json(f"/d2l/api/le/{le_version}/accommodations/{cid}/myaccommodations")),
            self._save_from("course/lti_links.json", api.json(f"/d2l/api/le/{le_version}/lti/link/{cid}/")),
            self._save_from("course/lti_tool_providers.json", api.json(f"/d2l/api/le/{le_version}/lti/tp/{cid}/")),
            self._save_from("course/lti_advantage_links.json",
                            api.paged(f"/d2l/api/le/{le_version}/ltiadvantage/links/orgunit/{cid}/")),
            self._save_from("course/lti_advantage_asset_processors.json",
                            api.paged(f"/d2l/api/le/{le_version}/ltiadvantage/orgunit/{cid}/assetprocessors/")),
            self._save_from("course/course_files.json", api.json(api.lp(f"/{cid}/managefiles/"))),
            api.download(api.lp(f"/courses/{cid}/image"), self.root / "course" / "course_image.jpg", absent_ok=True),
            overview(),
        )

    # ---------- content (beyond the TOC) ----------

    def _walk_toc(self):
        modules, topics = [], []

        def walk(module: Dict):
            modules.append(module)
            topics.extend(module.get("Topics", []))
            for sub in module.get("Modules", []):
                walk(sub)

        for module in self.toc.get("Modules", []):
            walk(module)
        return modules, topics

    @staticmethod
    def _window_ended(topic: Dict) -> bool:
        end = topic.get("EndDateTime")
        return bool(end) and datetime.fromisoformat(end.replace("Z", "+00:00")) < datetime.now(timezone.utc)

    async def content_meta(self):
        api = self.api
        out = "content_meta"
        modules, topics = self._walk_toc()

        async def counts():
            everything, due = await gather_all(api.json(self.le("/content/myItems/itemCount")),
                                               api.json(self.le("/content/myItems/due/itemCount")))
            self._save(f"{out}/item_counts.json", {"all": everything, "due": due})

        async def module_detail(module: Dict):
            mid = module.get("ModuleId") or module.get("Id")
            return str(mid), await api.json(self.le(f"/content/modules/{mid}"),
                                            absent_reason=f"module detail not found: {module.get('Title')}")

        async def topic_detail(topic: Dict):
            tid = topic.get("TopicId") or topic.get("Id")
            broken = topic.get("IsBroken")
            ended = self._window_ended(topic)
            detail, completion = await gather_all(
                api.json(self.le(f"/content/topics/{tid}"), absent_ok=ended,
                         absent_reason=None if ended else
                         f"topic detail not found (TOC marks it IsBroken={broken}): {topic.get('Title')}"),
                api.json(self.le(f"/content/topics/{tid}/completions/users/{self.user_id}"), absent_ok=True),
            )
            if detail is None and ended:
                # the detail route hides topics whose availability window has ended; the TOC entry has the same fields
                detail = {"_source": "toc: topic detail is hidden because its availability window ended", **topic}
            return str(tid), {"detail": detail, "my_completion": completion}

        async def caption(topic: Dict):
            tid = topic.get("TopicId") or topic.get("Id")
            return str(tid), await api.json(self.le(f"/content/topics/{tid}/captions"), absent_ok=True)

        async def captions():
            media = [t for t in topics
                     if t.get("ActivityType") == 1
                     and Path(unquote(urlparse(t.get("Url") or "").path)).suffix.lower() in MEDIA_EXTENSIONS]
            self._save(f"{out}/captions.json", dict(await gather_all(*(caption(t) for t in media))) or None)

        async def save_all(relative: str, awaitables):
            self._save(relative, dict(await gather_all(*awaitables)))

        await gather_all(
            self._save_from(f"{out}/root.json", api.json(self.le("/content/root/"))),
            self._save_from(f"{out}/my_items.json", api.paged(self.le("/content/myItems/"))),
            self._save_from(f"{out}/my_items_due.json", api.paged(self.le("/content/myItems/due/"))),
            counts(),
            self._save_from(f"{out}/recent.json", api.json(self.le("/content/recent"))),
            self._save_from(f"{out}/bookmarks.json", api.json(self.le("/content/bookmarks"))),
            self._save_from(f"{out}/completions.json",
                            api.json(self.le(f"/content/completions/?userIdsCSV={self.user_id}"))),
            self._save_from(f"{out}/pacing.json", api.json(self.le("/content/pacing"))),  # 403 for students
            save_all(f"{out}/modules.json", [module_detail(m) for m in modules]),
            save_all(f"{out}/topics.json", [topic_detail(t) for t in topics]),
            captions(),
        )

    # ---------- dropbox ----------

    async def dropbox(self):
        out = Path("dropbox")
        api = self.api
        folders, categories = await gather_all(api.json(self.le("/dropbox/folders/")),
                                               api.paged(self.le("/dropbox/categories/")))
        folders = folders or []
        self._save(f"{out}/folders.json", folders)
        self._save(f"{out}/categories.json", categories)
        await gather_all(*(self._dropbox_folder(folder, out) for folder in folders))

    async def _dropbox_folder(self, folder: Dict, out: Path):
        fid = folder["Id"]
        fdir = out / _labelled(folder.get("Name", fid), fid)
        self._save(f"{fdir}/folder.json", folder)

        async def submissions():
            entries = await self.api.json(self.le(f"/dropbox/folders/{fid}/submissions/mysubmissions/")) or []
            self._save(f"{fdir}/my_submissions.json", entries)
            await gather_all(*(self._submission_entry(fid, entry, fdir) for entry in entries))

        await gather_all(
            self._md(folder.get("CustomInstructions"), self.root / fdir / "instructions.md", title=folder.get("Name")),
            self._download_files(
                folder.get("Attachments"),
                lambda f: self.le(f"/dropbox/folders/{fid}/attachments/{f['FileId']}"),
                self.root / fdir / "instructions_attachments",
            ),
            submissions(),
        )

    async def _submission_entry(self, fid: int, entry: Dict, fdir: Path):
        entity = entry.get("Entity") or {}
        kind, eid = entity.get("EntityType", "User"), entity.get("EntityId")
        jobs = []
        for sub in entry.get("Submissions") or []:
            sid = sub.get("Id")
            sdir = self.root / fdir / "submissions" / str(sid)
            jobs.append(self._md(sub.get("Comment"), sdir / "comment.md", title="Submission comment"))
            jobs.append(self._download_files(
                sub.get("Files"),
                lambda f, sid=sid: self.le(f"/dropbox/folders/{fid}/submissions/{sid}/files/{f['FileId']}"),
                sdir,
            ))
        feedback = entry.get("Feedback")
        if feedback:
            self._save(f"{fdir}/feedback_{kind}_{eid}.json", feedback)
            jobs.append(self._md(feedback.get("Feedback"), self.root / fdir / f"feedback_{kind}_{eid}.md", title="Feedback"))
            jobs.append(self._download_files(
                feedback.get("Files"),
                lambda f: self.le(f"/dropbox/folders/{fid}/feedback/{kind}/{eid}/attachments/{f['FileId']}"),
                self.root / fdir / f"feedback_files_{kind}_{eid}",
            ))
        await gather_all(*jobs)

    # ---------- rubrics ----------

    async def rubrics(self):
        associations = await self.api.json(self.le("/rubrics/associations/"))
        self._save("rubrics/associations.json", associations)

        async def one(kind: str, oid: Any):
            data = await self.api.json(self.le(f"/rubrics/?objectType={quote(str(kind))}&objectId={oid}"))
            self._save(f"rubrics/{clean_name(kind)}_{oid}.json", data)

        await gather_all(*(one(assoc["ActivityType"], assoc["ObjectId"])
                           for item in associations or [] for assoc in item.get("Associations") or []))

    # ---------- news ----------

    async def news(self):
        out = self.root / "news"
        items = await self.api.paged(self.le("/news/")) or []
        self._save("news/news.json", items)

        async def one(item: Dict):
            nid = item["Id"]
            await gather_all(
                self._md(item.get("Body"), out / f"{nid} - {clean_name(item.get('Title', ''))}.md",
                         title=item.get("Title"),
                         header=f"<em>Posted {item.get('StartDate') or item.get('CreatedDate')}</em>"),
                self._download_files(
                    item.get("Attachments"),
                    lambda f: self.le(f"/news/{nid}/attachments/{f['FileId']}"),
                    out / "attachments" / str(nid),
                ),
            )

        await gather_all(*(one(item) for item in items))

    # ---------- discussions ----------

    async def discussions(self):
        out = Path("discussions")
        forums = await self.api.json(self.le("/discussions/forums/")) or []
        self._save(f"{out}/forums.json", forums)
        await gather_all(*(self._forum(forum, out) for forum in forums))

    async def _forum(self, forum: Dict, out: Path):
        fo = forum["ForumId"]
        fdir = out / _labelled(forum.get("Name", fo), fo)
        topics = await self.api.json(self.le(f"/discussions/forums/{fo}/topics/")) or []
        self._save(f"{fdir}/topics.json", topics)
        await gather_all(
            self._md(forum.get("Description"), self.root / fdir / "description.md", title=forum.get("Name")),
            *(self._forum_topic(fo, topic, fdir) for topic in topics),
        )

    async def _forum_topic(self, fo: Any, topic: Dict, fdir: Path):
        to = topic["TopicId"]
        tdir = fdir / _labelled(topic.get("Name", to), to)
        posts = await self.api.json(self.le(f"/discussions/forums/{fo}/topics/{to}/posts/")) or []
        self._save(f"{tdir}/posts.json", posts)
        await gather_all(
            self._md(topic.get("Description"), self.root / tdir / "description.md", title=topic.get("Name")),
            self._posts_md(posts, self.root / tdir / "posts.md", topic.get("Name")),
            *(self._download_files(
                post.get("Attachments"),
                lambda f, pid=post.get("PostId"): self.le(
                    f"/discussions/forums/{fo}/topics/{to}/posts/{pid}/attachments/{f['FileId']}"),
                self.root / tdir / "attachments" / str(post.get("PostId")),
            ) for post in posts),
        )

    async def _posts_md(self, posts: List[Dict], path: Path, title: Optional[str]):
        if not self.md or not posts:
            return
        parts = []
        for post in posts:
            body = (post.get("Message") or {}).get("Html") or ""
            parts.append(f"<h2>{post.get('Subject') or 'Post'}</h2>"
                         f"<p><em>User {post.get('PostingUserId')} - {post.get('DatePosted')}</em></p>{body}")
        await self.md.html_to_file("<hr>".join(parts), path, title=title)

    # ---------- quizzes and surveys ----------

    async def quizzes(self):
        out = Path("quizzes")
        api = self.api
        categories, quizzes = await gather_all(api.paged(self.le("/quizzes/categories/")),
                                               api.paged(self.le("/quizzes/")))
        quizzes = quizzes or []
        self._save(f"{out}/categories.json", categories)
        self._save(f"{out}/quizzes.json", quizzes)

        async def one(quiz: Dict):
            qid = quiz["QuizId"]
            qdir = out / _labelled(quiz.get("Name", qid), qid)

            async def detail():
                data = await api.json(self.le(f"/quizzes/{qid}"))
                self._save(f"{qdir}/quiz.json", data)
                for label, rich in (("description", "Description"), ("instructions", "Instructions")):
                    await self._md(((data or quiz).get(rich) or {}).get("Text"),
                                   self.root / qdir / f"{label}.md", title=quiz.get("Name"))

            await gather_all(
                detail(),
                # students are normally not allowed to read attempts and questions (403 -> warning)
                self._save_from(f"{qdir}/attempts.json", api.paged(self.le(f"/quizzes/{qid}/attempts/"))),
                self._save_from(f"{qdir}/questions.json", api.paged(self.le(f"/quizzes/{qid}/questions/"))),
            )

        await gather_all(*(one(q) for q in quizzes))

    async def surveys(self):
        out = Path("surveys")
        api = self.api
        categories, surveys = await gather_all(api.paged(self.le("/surveys/categories/")),
                                               api.paged(self.le("/surveys/")))
        surveys = surveys or []
        self._save(f"{out}/categories.json", categories)
        self._save(f"{out}/surveys.json", surveys)

        async def one(survey: Dict):
            sid = survey["SurveyId"]
            sdir = out / _labelled(survey.get("Name", sid), sid)
            await gather_all(
                self._save_from(f"{sdir}/survey.json", api.json(self.le(f"/surveys/{sid}"))),
                self._save_from(f"{sdir}/attempts.json", api.paged(self.le(f"/surveys/{sid}/attempts/"))),
                self._save_from(f"{sdir}/questions.json", api.paged(self.le(f"/surveys/{sid}/questions/"))),
            )

        await gather_all(*(one(s) for s in surveys))

    # ---------- checklists ----------

    async def checklists(self):
        out = Path("checklists")
        api = self.api
        lists = await api.paged(self.le("/checklists/")) or []
        self._save(f"{out}/checklists.json", lists)

        async def one(cl: Dict):
            cid = cl["ChecklistId"]
            name = _labelled(cl.get("Name", cid), cid)
            data, categories, items = await gather_all(
                api.json(self.le(f"/checklists/{cid}")),
                api.paged(self.le(f"/checklists/{cid}/categories/")),
                api.paged(self.le(f"/checklists/{cid}/items/")),
            )
            data = dict(data or {}, categories=categories, items=items)
            self._save(f"{out}/{name}.json", data)
            await self._checklist_md(data, self.root / out / f"{name}.md")

        await gather_all(*(one(cl) for cl in lists))

    async def _checklist_md(self, data: Dict, path: Path):
        if not self.md:
            return
        parts = [(data.get("Description") or {}).get("Html") or ""]
        items = data.get("items") or []
        for category in data.get("categories") or []:
            parts.append(f"<h2>{category.get('Name')}</h2>")
            mine = "".join(f"<li>{i.get('Name')}</li>" for i in items if i.get("CategoryId") == category.get("CategoryId"))
            if mine:
                parts.append(f"<ul>{mine}</ul>")
        await self.md.html_to_file("".join(parts), path, title=data.get("Name"))

    # ---------- grades ----------

    async def grades(self):
        out = "grades"
        api = self.api
        items, categories = await gather_all(api.json(self.le("/grades/")), api.json(self.le("/grades/categories/")))
        items, categories = items or [], categories or []
        self._save(f"{out}/grade_items.json", items)
        scheme_ids = sorted({i["GradeSchemeId"] for i in items if i.get("GradeSchemeId")})

        async def category_details():
            self._save(f"{out}/categories.json",
                       await gather_all(*(api.json(self.le(f"/grades/categories/{c['Id']}")) for c in categories)))

        async def scheme_details():
            if scheme_ids:
                details = await gather_all(*(api.json(self.le(f"/grades/schemes/{s}")) for s in scheme_ids))
                self._save(f"{out}/scheme_details.json", {str(s): d for s, d in zip(scheme_ids, details)})

        await gather_all(
            self._save_from(f"{out}/setup.json", api.json(self.le("/grades/setup/"))),
            self._save_from(f"{out}/my_grade_values.json", api.json(self.le("/grades/values/myGradeValues/"))),
            self._save_from(f"{out}/my_final_grade.json",
                            api.json(self.le("/grades/final/values/myGradeValue"), absent_ok=True)),
            category_details(),
            self._save_from(f"{out}/schemes.json", api.json(self.le("/grades/schemes/"))),  # 403 for students
            scheme_details(),
        )

    # ---------- calendar ----------

    async def calendar(self):
        api = self.api
        await gather_all(
            self._save_from("calendar/events.json", api.json(self.le("/calendar/events/"))),
            self._save_from("calendar/my_events.json", api.paged(self.le(f"/calendar/events/myEvents/?{CALENDAR_WINDOW}"))),
            self._save_from("calendar/event_occurrences.json", api.paged(
                self.le(f"/calendar/events/orgunits/?orgUnitIdsCSV={self.course_id}&{CALENDAR_WINDOW}"))),
        )

    # ---------- groups and group lockers ----------

    async def groups(self):
        api = self.api
        categories, _ = await gather_all(
            api.json(self.lp("/groupcategories/")),
            self._save_from("groups/sections.json", api.json(self.lp("/sections/"))),  # 403 for students
        )
        categories = categories or []
        self._save("groups/categories.json", categories)
        await gather_all(*(self._group_category(cat) for cat in categories))

    async def _group_category(self, cat: Dict):
        api = self.api
        cid = cat["GroupCategoryId"]
        cdir = Path("groups") / _labelled(cat.get("Name", cid), cid)
        groups, _, locker = await gather_all(
            api.json(self.lp(f"/groupcategories/{cid}/groups/")),
            self._save_from(f"{cdir}/enrollments.json", api.paged(self.lp(f"/groupcategories/{cid}/groups/enrollments"))),
            api.json(self.lp(f"/groupcategories/{cid}/locker")),
        )
        groups = groups or []
        self._save(f"{cdir}/groups.json", groups)
        if not (locker or {}).get("HasLocker"):
            return
        await gather_all(*(
            self._locker(group["GroupId"], "",
                         self.root / cdir / _labelled(group.get("Name", group["GroupId"]), group["GroupId"]) / "locker")
            for group in groups if self.user_id in (group.get("Enrollments") or [])  # only groups we are a member of
        ))

    async def _locker(self, group_id: int, path: str, directory: Path):
        base = self.le(f"/locker/group/{group_id}/")
        listing = await self.api.json(f"{base}{path}")
        entries = listing.get("Contents", []) if isinstance(listing, dict) else (listing or [])
        self._save(str((directory / "_listing.json").relative_to(self.root)), entries)
        await gather_all(*(
            self._locker(group_id, f"{path}{quote(entry['Name'])}/", directory / clean_name(entry["Name"]))
            if entry.get("Type") == LOCKER_FOLDER else
            self.api.download(f"{base}{path}{quote(entry['Name'])}", directory / clean_name(entry["Name"]))
            for entry in entries
        ))

    # ---------- learning outcomes ----------

    async def outcomes(self):
        api = self.api
        await gather_all(
            self._save_from("outcomes/competencies.json", api.paged(self.le("/competencies/structure"))),
            self._save_from("outcomes/outcome_sets.json", api.paged(self.le("/lo/outcomeSets/"))),  # 403 for students
            self._save_from("outcomes/alignments.json", api.paged(self.le("/lo/alignments/"))),  # 403 for students
        )
