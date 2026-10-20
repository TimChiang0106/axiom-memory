"""The /app pages: a phone-first work home over tasks and project overviews.

The home page exists to answer one question — "is everything taken care
of?" — so it leads with a one-line status (how many today, how many already
arranged for later, how many still need a day) and then lists only what
needs a look now. Tasks can be added, edited, finished and undone here;
memories and documents are shown read-only. The "我" page shows the
`user-profile` memory, which the user's AI clients curate on request.

Server-rendered HTML with plain forms (POST, then redirect back): no build
step, no client state, works on any phone browser. A manifest and icon let
the page be added to a phone's home screen.
"""

import json
import re
from collections import Counter
from collections.abc import Awaitable, Callable
from datetime import date, timedelta
from html import escape
from pathlib import Path
from urllib.parse import parse_qsl, quote, urlencode, urlparse

from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response
from starlette.routing import Route

from axiom.auth import AppAuth
from axiom.documents import DocumentStore
from axiom.models import Memory, Task
from axiom.store import MemoryStore
from axiom.tasks import ProjectStats, TaskError, TaskStore, later_this_week

WEEKDAYS = "一二三四五六日"
TRIGGER_LABELS = {
    "unrecorded_commitment": "對話中提到但沒記下的承諾",
    "due_soon": "截止日將近",
    "overview_stale": "專案總覽落後",
}
CATEGORY_LABELS = {
    "you": "關於你",
    "people": "人",
    "areas": "專案與領域",
    "topics": "主題",
}
TYPE_LABELS = {
    "preference": "偏好",
    "fact": "事實",
    "state": "現況",
    "procedural": "做法",
    "reference": "參考",
    "project": "專案",
}
PROFILE = "user-profile"
STATUS_LABELS = {
    "open": "進行中",
    "done": "已完成",
    "dropped": "已刪除",
    "suggested": "AI 建議",
    "expired": "已失效",
}
OPEN_WARN = 5
# Path segment standing for "no project" in /app/projects/<name>.
NO_PROJECT = "-"

_STATIC = Path(__file__).parent / "static"
_ICONS = {size: (_STATIC / f"icon-{size}.png").read_bytes() for size in (180, 512)}
_MANIFEST = json.dumps(
    {
        "name": "Axiom",
        "short_name": "Axiom",
        "start_url": "/app",
        "scope": "/app",
        "display": "standalone",
        "background_color": "#FFFFFF",
        "theme_color": "#FFFFFF",
        "icons": [
            {"src": "/app/icon-180.png", "sizes": "180x180", "type": "image/png"},
            {
                "src": "/app/icon-512.png",
                "sizes": "512x512",
                "type": "image/png",
                "purpose": "any maskable",
            },
        ],
    }
)

_CSS = """
:root{color-scheme:light;
--bg:#FFFFFF;--surface:#FFFFFF;--line:#E3E5EC;--fg:#1B1F2A;--dim:#5B6173;--input:#7E8494;
--primary:#1B1F2A;--primary-hover:#363A44;--on-primary:#FFFFFF;--selected:#EDEDEE;
--ok:#1E7F4F;--warn:#9A5B00;--bad:#C62828;--info:#2F3A8F;
--r-tag:4px;--r-ctl:12px;--r-card:20px}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--fg);font-size:16px;line-height:1.6;
font-family:"DM Sans","Noto Sans TC","PingFang TC","Microsoft JhengHei",system-ui,sans-serif;
font-variant-numeric:tabular-nums}
header{position:sticky;top:0;z-index:2;background:var(--bg);border-bottom:1px solid var(--line);
padding-top:env(safe-area-inset-top)}
.bar{max-width:720px;margin:0 auto;padding:0 16px;height:56px;display:flex;align-items:center;
gap:4px}
.brand{font-weight:700;margin-right:auto}
nav{display:flex;gap:2px}
nav a{padding:6px 10px;border-radius:var(--r-ctl);color:var(--dim);text-decoration:none;
transition:background-color .15s}
nav a:hover{background:var(--selected)}
nav a.on{background:var(--selected);color:var(--fg);font-weight:600}
main{max-width:720px;margin:0 auto;padding:0 16px calc(96px + env(safe-area-inset-bottom))}
a{color:var(--fg);text-decoration:underline;text-decoration-color:var(--input);
text-underline-offset:3px}
h1{font-size:22px;font-weight:700;line-height:1.4;margin:24px 0 4px}
h2{display:flex;gap:8px;font-size:14px;font-weight:600;color:var(--dim);margin:28px 4px 8px}
h2 .count{font-weight:400}
.back{display:inline-block;margin-top:16px;font-size:14px;color:var(--dim);text-decoration:none}
.back:hover{color:var(--fg)}
.summary{margin:24px 4px 0}
.summary .date{font-size:14px;color:var(--dim)}
.summary .main{font-size:22px;font-weight:700;line-height:1.4}
.summary .sub{color:var(--dim)}
.summary .todo{color:var(--warn);font-weight:600}
.list{background:var(--surface);border:1px solid var(--line);border-radius:var(--r-card);
overflow:hidden;margin-bottom:12px}
.row{padding:14px 16px}
.row+.row{border-top:1px solid var(--line)}
a.row{display:block;color:inherit;text-decoration:none;transition:background-color .15s}
a.row:hover{background:var(--selected)}
.title{font-weight:600}
.title a{color:inherit;text-decoration:none}
.meta{font-size:14px;color:var(--dim);margin-top:2px}
.flag{font-size:14px;color:var(--warn);margin-top:4px}
.bad{color:var(--bad)}
.tag{display:inline-block;font-size:12px;padding:0 6px;border-radius:var(--r-tag);
background:var(--selected);color:var(--dim)}
.title .tag{margin-right:6px;vertical-align:2px;font-weight:600;line-height:18px}
.tag.hi{background:var(--primary);color:var(--on-primary)}
.tag.lo{background:transparent;box-shadow:inset 0 0 0 1px var(--line);font-weight:400}
.task{display:flex;gap:4px;align-items:flex-start;transition:opacity .2s}
.task.leaving{opacity:.4}
.check{margin:-6px 0 0 -8px}
.check button{width:36px;height:36px;min-height:0;padding:0;justify-content:center;
background:transparent;border:0}
.check button::before{content:"";width:20px;height:20px;border:1.5px solid var(--input);
border-radius:50%;transition:background-color .15s,border-color .15s}
.check button:hover{background:transparent}
.check button:hover::before{border-color:var(--fg)}
.leaving .check button::before{background:var(--primary);border-color:var(--primary)}
.task details{flex:1;min-width:0}
.task summary{display:flex;gap:8px;list-style:none;cursor:pointer}
.task summary::-webkit-details-marker{display:none}
.task summary>div{flex:1;min-width:0}
.task summary .dots{color:var(--input);line-height:1.5;padding:0 2px}
.task details[open] .dots{color:var(--fg)}
.task summary:focus-visible{outline:2px solid var(--fg);outline-offset:2px;
border-radius:var(--r-tag)}
.opts{display:flex;flex-wrap:wrap;gap:8px;margin-top:12px}
.opts form{margin:0}
.opts .end{margin-left:auto}
a.btn{display:inline-flex;align-items:center;min-height:36px;padding:0 14px;font-size:14px;
line-height:1;color:var(--fg);text-decoration:none;border:1px solid var(--line);
border-radius:var(--r-ctl);transition:background-color .15s}
a.btn:hover{background:var(--selected)}
.chips{display:flex;flex-wrap:wrap;gap:8px;margin-top:12px}
.chips a{display:inline-flex;align-items:center;gap:6px;min-height:36px;padding:0 14px;
font-size:14px;color:var(--fg);text-decoration:none;border:1px solid var(--line);
border-radius:var(--r-ctl);transition:background-color .15s}
.chips a:hover{background:var(--selected)}
.chips a.on{background:var(--selected);border-color:var(--fg);font-weight:600}
.chips span{color:var(--dim);font-weight:400}
.prose p{margin:0}
.prose ul{margin:0;padding-left:20px}
.prose li+li{margin-top:4px}
.prose>*+*{margin-top:8px}
.clamp{display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.actions,.tools{display:flex;flex-wrap:wrap;align-items:center;gap:8px}
.actions{margin-top:12px}
.actions.flush,.row.flush .metrics{margin-top:0}
.grow{flex:1;min-width:0}
.meta.grow{margin-top:0}
.foot{margin-top:16px}
.add{margin-top:20px}
.tools{margin:0 0 8px}
.actions form,.tools form{margin:0}
button,.more summary{appearance:none;display:inline-flex;align-items:center;min-height:36px;
padding:0 14px;font:inherit;font-size:14px;line-height:1;color:var(--fg);background:var(--surface);
border:1px solid var(--line);border-radius:var(--r-ctl);cursor:pointer;
transition:background-color .15s,border-color .15s,color .15s}
button:hover,.more summary:hover{background:var(--selected)}
button.primary{background:var(--primary);border-color:var(--primary);color:var(--on-primary);
font-weight:600}
button.primary:hover{background:var(--primary-hover);border-color:var(--primary-hover)}
button.quiet{background:transparent;border-color:transparent;color:var(--dim)}
button.quiet:hover{background:transparent;color:var(--bad)}
button:focus-visible,.more summary:focus-visible,input:focus-visible,select:focus-visible,
a:focus-visible{outline:2px solid var(--fg);outline-offset:2px}
.more summary{list-style:none}
.more summary::-webkit-details-marker{display:none}
.more[open]{flex-basis:100%}
.more[open] summary{background:var(--selected);font-weight:600}
.more .days{display:flex;flex-wrap:wrap;gap:8px;margin-top:8px}
.days .end{margin-left:auto}
input[type=text],input[type=password],input[type=search],input[type=date],select{
min-height:40px;padding:0 12px;font:inherit;font-size:16px;color:var(--fg);
background:var(--surface);border:1px solid var(--input);border-radius:var(--r-ctl)}
input[type=date]{min-width:0}
.wide{width:100%}
.reject{display:flex;gap:8px;flex:1;min-width:220px}
.reject input{flex:1;min-width:0;min-height:36px;font-size:14px}
.seg{display:flex;flex-wrap:wrap;gap:8px}
.seg label{position:relative}
.seg input{position:absolute;opacity:0;pointer-events:none}
.seg span{display:inline-flex;align-items:center;min-height:36px;padding:0 14px;font-size:14px;
border:1px solid var(--line);border-radius:var(--r-ctl);cursor:pointer;
transition:background-color .15s,border-color .15s}
.seg input:checked+span{background:var(--selected);border-color:var(--fg);font-weight:600}
.seg input:focus-visible+span{outline:2px solid var(--fg);outline-offset:2px}
.addopts{display:flex;flex-wrap:wrap;align-items:center;gap:8px;margin-top:12px}
.addopts select{min-height:36px;font-size:14px}
.addopts button{margin-left:auto}
.fields{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.field{display:flex;flex-direction:column;gap:4px;font-size:14px;color:var(--dim)}
.field.full{grid-column:1/-1}
.field input,.field select{width:100%;min-width:0}
.fields>*{min-width:0}
.search{display:flex;gap:8px;margin-top:16px}
.search input{flex:1;min-width:0}
.empty{color:var(--dim);padding:14px 16px}
.err{margin:16px 0 0;padding:10px 14px;font-size:14px;color:var(--bad);background:var(--surface);
border:1px solid currentColor;border-radius:var(--r-ctl)}
.notice{position:fixed;z-index:3;left:16px;right:16px;
bottom:calc(16px + env(safe-area-inset-bottom));
max-width:688px;margin:0 auto;display:flex;align-items:center;gap:12px;
padding:6px 6px 6px 16px;font-size:14px;color:var(--on-primary);background:var(--primary);
border-radius:var(--r-ctl)}
.notice span{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.notice form{margin:0}
.notice button{background:transparent;border-color:transparent;color:var(--on-primary);
font-weight:600}
.notice button:hover{background:var(--primary-hover)}
.day{display:grid;grid-template-columns:96px 1fr;gap:12px}
.day .d{font-size:14px;color:var(--dim);padding-top:1px}
.day.today{background:var(--selected)}
.day.today .d{color:var(--fg);font-weight:600}
.day .t+.t{margin-top:4px}
.day .t a{color:inherit;text-decoration:none}
.done{text-decoration:line-through;color:var(--dim)}
.weeknav{display:flex;justify-content:space-between;align-items:baseline;gap:12px}
.weeknav a{font-size:14px;color:var(--dim);text-decoration:none;white-space:nowrap}
.metrics{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-top:12px}
.metrics .n{display:block;font-size:20px;font-weight:700;line-height:1.3}
.metrics .l{font-size:13px;color:var(--dim)}
details.ov{margin-top:12px}
details.ov summary{cursor:pointer;color:var(--dim);font-size:14px}
.body{white-space:pre-wrap;font-size:15px;margin-top:8px}
.row>.body:first-child{margin-top:0}
details.ov .body{padding-top:12px;border-top:1px solid var(--line)}
.tags{display:flex;flex-wrap:wrap;align-items:center;gap:8px;margin:8px 0 16px;font-size:14px;
color:var(--dim)}
.login{max-width:360px;margin:15vh auto 0}
.login form{display:flex;flex-direction:column;gap:12px;margin-top:16px}
.login button{justify-content:center;min-height:40px}
@media (max-width:380px){nav a{padding:6px 8px}.fields{grid-template-columns:1fr}}
.progress{position:absolute;left:0;right:0;bottom:-1px;height:2px;overflow:hidden;opacity:0;
transition:opacity .2s}
.progress::before{content:"";position:absolute;top:0;bottom:0;width:30%;
background:var(--primary);animation:slide 1s ease-in-out infinite}
.busy .progress{opacity:1}
@keyframes slide{from{left:-30%}to{left:100%}}
button.pending{position:relative;color:transparent;pointer-events:none}
button.pending::after{content:"";position:absolute;inset:0;margin:auto;width:14px;height:14px;
border:2px solid var(--line);border-top-color:var(--fg);border-radius:50%;
animation:spin .7s linear infinite}
button.primary.pending::after,.notice button.pending::after{border-color:rgba(255,255,255,.3);
border-top-color:var(--on-primary)}
@keyframes spin{to{transform:rotate(360deg)}}
.skel{display:none}
.nav main>:not(.skel){display:none}
.nav .skel{display:block}
.skel i{display:block;height:72px;margin-top:12px;border-radius:var(--r-card);
background:linear-gradient(90deg,#F3F4F7 0,#E8EAEF 40%,#F3F4F7 80%) 0 0/200% 100%;
animation:shimmer 1.2s linear infinite}
.skel i.h{width:45%;height:28px;margin-top:24px;border-radius:var(--r-tag)}
@keyframes shimmer{to{background-position:-200% 0}}
@media (prefers-reduced-motion:reduce){*{transition:none!important}
.skel i,.progress::before{animation:none}.progress::before{left:0;width:100%}}
"""
# Progressive enhancement: post forms in place and swap in the page the
# redirect lands on, so the list keeps its scroll position. Without
# JavaScript the same forms post and redirect as usual. Anything slower
# than a blink shows a progress line under the header; leaving the page
# also swaps the content for a skeleton, since an installed app has no
# browser loading bar.
_JS = """
let timer;
function busy(on,leaving){
clearTimeout(timer);
const c=document.documentElement.classList;
if(!on){c.remove('busy','nav');return}
timer=setTimeout(()=>{c.add('busy');if(leaving)c.add('nav')},120);
}
addEventListener('pageshow',()=>busy(false));
document.addEventListener('click',e=>{
const a=e.target.closest('a[href]');
if(!a||e.defaultPrevented||e.button||e.metaKey||e.ctrlKey||e.shiftKey||e.altKey||a.target)return;
const u=new URL(a.href,location.href);
if(u.origin!==location.origin||!u.pathname.startsWith('/app'))return;
if(a.closest('nav'))document.querySelectorAll('nav a').forEach(x=>x.classList.toggle('on',x===a));
busy(true,true);
});
document.addEventListener('submit',async e=>{
const f=e.target;
if(e.defaultPrevented||!f.closest('main'))return;
if(f.method!=='post'){busy(true,true);return}
e.preventDefault();
const body=new FormData(f);
const row=f.closest('.task');
if(row&&f.action.endsWith('/done'))row.classList.add('leaving');
else if(e.submitter){e.submitter.classList.add('pending');e.submitter.disabled=true}
const add=f.classList.contains('add');
busy(true);
try{
const r=await fetch(f.action,{method:'POST',body});
const u=new URL(r.url);
if(!r.ok||u.pathname!==location.pathname){location.href=r.url;return}
const doc=new DOMParser().parseFromString(await r.text(),'text/html');
const y=scrollY;
document.querySelector('main').replaceWith(doc.querySelector('main'));
history.replaceState(null,'',u.pathname+u.search);
scrollTo(0,y);
if(add)document.querySelector('main form.add input[name=title]')?.focus();
busy(false);
}catch(_){f.submit()}
});
"""
_SKELETON = '<div class="skel" aria-hidden="true"><i class="h"></i>' + "<i></i>" * 4 + "</div>"
_FONTS = "https://fonts.googleapis.com/css2?family=DM+Sans:opsz,wght@9..40,400..700&display=swap"


def _page(title: str, active: str, body: str) -> HTMLResponse:
    tabs = [
        ("today", "/app", "今天"),
        ("week", "/app/week", "本週"),
        ("projects", "/app/projects", "專案"),
        ("library", "/app/library", "資料"),
        ("me", "/app/me", "我"),
    ]
    nav = "".join(
        f'<a href="{href}"{" class=on aria-current=page" if key == active else ""}>{label}</a>'
        for key, href, label in tabs
    )
    return HTMLResponse(
        "<!doctype html><html lang='zh-Hant'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1,viewport-fit=cover'>"
        "<meta name='robots' content='noindex'>"
        "<meta name='theme-color' content='#FFFFFF'>"
        "<meta name='mobile-web-app-capable' content='yes'>"
        "<meta name='apple-mobile-web-app-capable' content='yes'>"
        "<meta name='apple-mobile-web-app-title' content='Axiom'>"
        "<link rel='manifest' href='/app/manifest.webmanifest' crossorigin='use-credentials'>"
        "<link rel='icon' href='/app/icon-180.png'>"
        "<link rel='apple-touch-icon' href='/app/icon-180.png'>"
        f"<title>{escape(title)} · Axiom</title>"
        f"<link rel='stylesheet' href='{_FONTS}'><style>{_CSS}</style></head>"
        f"<body><header><div class='bar'><span class='brand'>Axiom</span><nav>{nav}</nav>"
        f"</div><div class='progress'></div></header><main>{body}{_SKELETON}</main>"
        f"<script>{_JS}</script></body></html>"
    )


# A leading "[P1]" or "[有時間再做]" in a title is a priority label the AI
# clients write; it is shown as a tag and used to order a section.
_LABEL = re.compile(r"^\[([^\[\]]{1,10})\]\s*")


def _split_label(title: str) -> tuple[str | None, str]:
    if m := _LABEL.match(title):
        return m.group(1), title[m.end() :]
    return None, title


def _rank(task: Task) -> int:
    """P0..P9 first by number, then unlabelled, then any other label
    ("有時間再做" and the like mean "not now")."""
    label, _ = _split_label(task.title)
    if label is None:
        return 10
    if re.fullmatch(r"P\d", label):
        return int(label[1])
    return 11


def _by_priority(tasks: list[Task]) -> list[Task]:
    return sorted(tasks, key=_rank)


def _title_html(title: str) -> str:
    label, rest = _split_label(title)
    if label is None:
        return escape(title)
    cls = "hi" if label in ("P0", "P1") else "" if re.fullmatch(r"P\d", label) else "lo"
    return f'<span class="tag {cls}">{escape(label)}</span>{escape(rest)}'


def _day(d: date, today: date | None = None) -> str:
    if today is not None:
        if d == today:
            return "今天"
        if d == today + timedelta(days=1):
            return "明天"
    return f"{d.month}/{d.day}（{WEEKDAYS[d.weekday()]}）"


def _with(url: str, **params: str | int) -> str:
    """`url` with its query replaced by `params` — so a stale error or undo
    notice never follows the user back to the page."""
    path = url.split("?", 1)[0]
    return f"{path}?{urlencode(params)}" if params else path


def _task_url(task: Task, back: str) -> str:
    return f"/app/tasks/{task.id}?{urlencode({'from': back})}"


def _project_url(project: str | None) -> str:
    return f"/app/projects/{quote(project or NO_PROJECT)}"


def _meta_text(task: Task, today: date, *, section_day: date | None = None) -> str:
    """`section_day` is the day the surrounding section already stands for,
    so a planned date equal to it is left out."""
    parts = []
    if task.project:
        parts.append(escape(task.project))
    if task.planned_date and task.planned_date == section_day:
        pass
    elif task.planned_date:
        parts.append(f"計畫 {_day(task.planned_date, today)}")
    elif task.someday:
        parts.append("之後再說")
    if task.due_date:
        overdue = task.due_date < today and task.status == "open"
        label = f"截止 {_day(task.due_date, today)}"
        parts.append(f'<span class="bad">{label}</span>' if overdue else label)
    return "・".join(parts)


def _meta(task: Task, today: date, *, section_day: date | None = None) -> str:
    text = _meta_text(task, today, section_day=section_day)
    return f'<div class="meta">{text}</div>' if text else ""


def _button(
    task_id: int,
    action: str,
    label: str,
    *,
    back: str,
    extra: str = "",
    cls: str = "",
    confirm: str = "",
) -> str:
    onsubmit = f" onsubmit=\"return confirm('{confirm}')\"" if confirm else ""
    end = ' class="end"' if action == "drop" else ""
    return (
        f'<form method="post" action="/app/tasks/{task_id}/{action}"{end}{onsubmit}>'
        f'<input type="hidden" name="next" value="{escape(back)}">{extra}'
        f'<button class="{cls}">{label}</button></form>'
    )


def _defer(task_id: int, label: str, to: str, *, back: str) -> str:
    return _button(
        task_id, "defer", label, back=back, extra=f'<input type="hidden" name="to" value="{to}">'
    )


def _task_actions(task: Task, today: date, *, back: str) -> str:
    days = "".join(
        _defer(task.id, _day(d), d.isoformat(), back=back) for d in later_this_week(today)
    )
    return (
        _button(task.id, "done", "完成", back=back, cls="primary")
        + _defer(task.id, "明天", "tomorrow", back=back)
        + _defer(task.id, "之後再說", "someday", back=back)
        + '<details class="more"><summary>更多</summary><div class="days">'
        + days
        + _button(task.id, "drop", "刪除", back=back, cls="quiet", confirm="刪除這件事？")
        + "</div></details>"
    )


def _task_card(
    task: Task, today: date, *, back: str = "/app", section_day: date | None = None
) -> str:
    """One open task: a circle to finish it, and the title, which opens the
    rest of the options — so a list of tasks reads as tasks, not buttons."""
    flags = ""
    if task.defer_count >= 3:
        flags += f'<div class="flag">延了 {task.defer_count} 次，還要做嗎？</div>'
    check = (
        f'<form method="post" action="/app/tasks/{task.id}/done" class="check">'
        f'<input type="hidden" name="next" value="{escape(back)}">'
        f'<button aria-label="完成「{escape(task.title)}」"></button></form>'
    )
    days = "".join(
        _defer(task.id, _day(d), d.isoformat(), back=back) for d in later_this_week(today)
    )
    opts = (
        _defer(task.id, "明天", "tomorrow", back=back)
        + _defer(task.id, "之後再說", "someday", back=back)
        + days
        + f'<a class="btn" href="{escape(_task_url(task, back))}">編輯</a>'
        + _button(task.id, "drop", "刪除", back=back, cls="quiet", confirm="刪除這件事？")
    )
    return (
        f'<div class="row task">{check}<details><summary><div>'
        f'<div class="title">{_title_html(task.title)}</div>'
        f"{_meta(task, today, section_day=section_day)}{flags}</div>"
        f'<span class="dots" aria-hidden="true">⋯</span></summary>'
        f'<div class="opts">{opts}</div></details></div>'
    )


def _done_card(task: Task, *, back: str) -> str:
    return (
        '<div class="row"><div class="actions flush">'
        f'<a class="done grow" href="{escape(_task_url(task, back))}">'
        f"{escape(_split_label(task.title)[1])}</a>"
        f"{_button(task.id, 'reopen', '復原', back=back, cls='quiet')}</div></div>"
    )


def _suggestion_card(task: Task, today: date, *, back: str = "/app") -> str:
    ev = task.evidence
    lines = []
    if ev and ev.trigger:
        lines.append(f"原因：{escape(TRIGGER_LABELS.get(ev.trigger, ev.trigger))}")
    if ev and ev.memories:
        links = "、".join(f'<a href="/app/memory/{escape(n)}">{escape(n)}</a>' for n in ev.memories)
        lines.append(f"依據：{links}")
    if ev and ev.note:
        lines.append(escape(ev.note))
    if task.expires_at:
        lines.append(f"{_day(task.expires_at.date())} 前未處理會自動失效")
    evidence = "".join(f'<div class="meta">{line}</div>' for line in lines)
    reject = (
        f'<form method="post" action="/app/tasks/{task.id}/reject" class="reject">'
        f'<input type="hidden" name="next" value="{escape(back)}">'
        '<input type="text" name="reason" placeholder="拒絕原因（選填）" aria-label="拒絕原因">'
        "<button>拒絕</button></form>"
    )
    return (
        f'<div class="row"><div class="title">{_title_html(task.title)}</div>'
        f'{_meta(task, today)}{evidence}<div class="actions">'
        f"{_button(task.id, 'confirm', '確認', back=back, cls='primary')}{reject}</div></div>"
    )


def _section(
    title: str,
    cards: list[str],
    *,
    empty: str | None = None,
    tools: str = "",
    counted: bool = True,
) -> str:
    if not cards and empty is None:
        return ""
    inner = "".join(cards) if cards else f'<div class="empty">{empty}</div>'
    count = f'<span class="count">{len(cards)}</span>' if cards and counted else ""
    return f'<section><h2>{title}{count}</h2>{tools}<div class="list">{inner}</div></section>'


def _project_options(projects: list[str], selected: str | None, *, none_label: str) -> str:
    options = [f'<option value="">{none_label}</option>']
    for p in projects:
        sel = " selected" if p == selected else ""
        options.append(f'<option value="{escape(p)}"{sel}>{escape(p)}</option>')
    return "".join(options)


def _quick_add(projects: list[str]) -> str:
    whens = [("today", "今天"), ("tomorrow", "明天"), ("someday", "之後"), ("none", "不排日期")]
    seg = "".join(
        f'<label><input type="radio" name="when" value="{v}"{" checked" if v == "today" else ""}>'
        f"<span>{label}</span></label>"
        for v, label in whens
    )
    project = (
        f'<select name="project" aria-label="專案">'
        f"{_project_options(projects, None, none_label='不分專案')}</select>"
        if projects
        else ""
    )
    return (
        '<form method="post" action="/app/tasks/new" class="list add">'
        '<div class="row"><input class="wide" type="text" name="title" required '
        'placeholder="新增一件事" aria-label="新增一件事" autocomplete="off" enterkeyhint="done">'
        f'<div class="addopts"><div class="seg">{seg}</div>{project}'
        '<button class="primary">新增</button></div></div></form>'
    )


def _metrics(s: ProjectStats | None) -> str:
    return (
        '<div class="metrics">'
        + "".join(
            f'<div><span class="n">{n}</span><span class="l">{label}</span></div>'
            for n, label in (
                (s.open if s else 0, "進行中"),
                (s.done_this_week if s else 0, "本週完成"),
                (s.done_total if s else 0, "總完成"),
            )
        )
        + "</div>"
    )


def _inline_md(text: str) -> str:
    """Escape, then render **bold** and link `doc:<name>` references —
    enough for overview memories and plain-text documents."""
    html = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", escape(text))
    return re.sub(r"\bdoc:([\w.-]+)", r'<a href="/app/docs/\1">doc:\1</a>', html)


def _memory_row(name: str, description: str, kind: str = "") -> str:
    tag = f'<div class="meta"><span class="tag">{kind}</span></div>' if kind else ""
    return (
        f'<a class="row" href="/app/memory/{escape(name)}"><div class="title">'
        f'{escape(name)}</div><div class="meta clamp">{escape(description)}</div>{tag}</a>'
    )


def _prose(text: str) -> str:
    """Paragraphs and `- ` bullet lists, each line through `_inline_md`."""
    html, items = [], []
    for line in [*text.strip().splitlines(), ""]:
        line = line.strip()
        if line.startswith(("- ", "* ")):
            items.append(f"<li>{_inline_md(line[2:])}</li>")
            continue
        if items:
            html.append(f"<ul>{''.join(items)}</ul>")
            items = []
        if line:
            html.append(f"<p>{_inline_md(line)}</p>")
    return f'<div class="prose">{"".join(html)}</div>'


def _profile_sections(text: str) -> list[tuple[str, str]]:
    """Split a profile at its `## ` headings into (heading, body) pairs; text
    before the first heading gets an empty heading."""
    sections: list[tuple[str, list[str]]] = [("", [])]
    for line in text.splitlines():
        if line.startswith("## "):
            sections.append((line[3:].strip(), []))
        else:
            sections[-1][1].append(line)
    return [(h, "\n".join(b)) for h, b in sections if h or "".join(b).strip()]


def build_routes(
    *,
    auth: AppAuth,
    get_tasks: Callable[[], Awaitable[TaskStore]],
    get_memories: Callable[[], Awaitable[MemoryStore]],
    get_documents: Callable[[], Awaitable[DocumentStore]],
) -> list[Route]:
    async def guard(request: Request) -> Response | None:
        if not auth.configured:
            return HTMLResponse("App auth is not configured.", status_code=503)
        who = await auth.identify(request.headers, request.cookies)
        if who is None:
            return RedirectResponse("/app/login", status_code=303)
        if request.method == "POST":
            # Same-origin forms only: the session survives cross-site
            # navigation, so a foreign page must not be able to post here.
            origin = request.headers.get("origin")
            if origin and urlparse(origin).netloc != request.headers.get("host"):
                return HTMLResponse("Cross-origin request refused.", status_code=403)
        return None

    def back_to(value: str | None) -> str:
        if value and value.startswith("/app") and not value.startswith("//"):
            return value
        return "/app"

    def here(request: Request) -> str:
        """The current page, minus one-shot notices, to come back to after a form."""
        params = {k: v for k, v in request.query_params.items() if k not in ("error", "undo")}
        return _with(request.url.path, **params)

    async def notices(request: Request, tasks: TaskStore) -> str:
        """The error banner and the undo bar a redirect asked for."""
        html = ""
        if error := request.query_params.get("error"):
            html += f'<div class="err">{escape(error)}</div>'
        undo = request.query_params.get("undo", "")
        if undo.isdigit() and (task := await tasks.get(int(undo))) is not None:
            verb = {"done": "已完成", "dropped": "已刪除"}.get(task.status)
            if verb:
                html += (
                    f'<div class="notice" role="status"><span>{verb}「{escape(task.title)}」</span>'
                    f"{_button(task.id, 'reopen', '復原', back=here(request))}</div>"
                )
        return html

    def redirect(back: str, **params: str | int) -> RedirectResponse:
        if not params:
            return RedirectResponse(back, status_code=303)
        keep = {k: v for k, v in parse_qsl(urlparse(back).query) if k not in ("error", "undo")}
        return RedirectResponse(_with(back, **keep, **params), status_code=303)

    async def today_page(request: Request) -> Response:
        if (denied := await guard(request)) is not None:
            return denied
        tasks = await get_tasks()
        view = await tasks.today_view()
        today = view.today
        n_today = len(view.overdue) + len(view.due_today)
        status = (
            f'<div class="date">{_day(today)}</div>'
            f'<div class="main">今天 {n_today} 件</div>'
            f'<div class="sub">其他 {view.arranged} 件都已排好日期</div>'
        )
        if view.unscheduled:
            status += f'<div class="todo">{len(view.unscheduled)} 件還沒安排</div>'
        body = f'<div class="summary">{status}</div>'
        body += await notices(request, tasks)
        body += _quick_add(await tasks.known_projects())
        bulk = ""
        if len(view.overdue) >= 2:
            bulk = (
                '<div class="tools">'
                + "".join(
                    '<form method="post" action="/app/tasks/bulk">'
                    f'<input type="hidden" name="to" value="{to}"><button>{label}</button></form>'
                    for to, label in (("today", "全部移到今天"), ("someday", "全部改到之後"))
                )
                + "</div>"
            )
        body += _section(
            "逾期", [_task_card(t, today) for t in _by_priority(view.overdue)], tools=bulk
        )
        body += _section(
            "今天",
            [_task_card(t, today, section_day=today) for t in _by_priority(view.due_today)],
        )
        body += _section("還沒安排", [_task_card(t, today) for t in view.unscheduled])
        body += _section("AI 建議待確認", [_suggestion_card(t, today) for t in view.suggested])
        if not (view.overdue or view.due_today or view.unscheduled or view.suggested):
            body += '<div class="list"><div class="empty">今天沒有要處理的事。</div></div>'
        body += _section(
            "今天已完成", [_done_card(t, back="/app") for t in await tasks.done_today()]
        )
        return _page("今天", "today", body)

    async def week_page(request: Request) -> Response:
        if (denied := await guard(request)) is not None:
            return denied
        tasks = await get_tasks()
        today = await tasks.today()
        try:
            day = date.fromisoformat(request.query_params.get("d", ""))
        except ValueError:
            day = today
        view = await tasks.week_view(day)
        back = here(request)
        cols = []
        for i, day_tasks in enumerate(view.days):
            d = view.monday + timedelta(days=i)
            items = (
                "".join(
                    f'<div class="t{" done" if t.status == "done" else ""}">'
                    f'<a href="{escape(_task_url(t, back))}">{_title_html(t.title)}</a></div>'
                    for t in day_tasks
                )
                or '<div class="t meta">—</div>'
            )
            cls = "row day today" if d == today else "row day"
            cols.append(
                f'<div class="{cls}"><div class="d">週{WEEKDAYS[i]} {d.month}/{d.day}</div>'
                f"<div>{items}</div></div>"
            )
        prev_week = (view.monday - timedelta(days=7)).isoformat()
        next_week = (view.monday + timedelta(days=7)).isoformat()
        body = (
            f'<div class="weeknav"><a href="/app/week?d={prev_week}">‹ 上週</a>'
            f"<h1>{_day(view.monday)} – {_day(view.monday + timedelta(days=6))}</h1>"
            f'<a href="/app/week?d={next_week}">下週 ›</a></div>'
        )
        body += await notices(request, tasks)
        body += f'<div class="list">{"".join(cols)}</div>'
        groups: dict[str, list[Task]] = {}
        for t in view.done:
            groups.setdefault(t.project or "未分專案", []).append(t)
        rows = [
            f'<div class="row"><div class="title">{escape(project)}'
            f' <span class="tag">{len(items)}</span></div>'
            + "".join(f'<div class="meta">{escape(t.title)}</div>' for t in items)
            + "</div>"
            for project, items in groups.items()
        ]
        body += _section("這週已完成", rows, empty="還沒有。")
        return _page("本週", "week", body)

    async def projects_page(request: Request) -> Response:
        if (denied := await guard(request)) is not None:
            return denied
        tasks = await get_tasks()
        memories = await get_memories()
        today = await tasks.today()
        stats = await tasks.project_stats()
        overviews = await memories.overviews()
        drifted = {d.name: d for d in await memories.drifted_overviews()}
        orphans = await memories.orphans()

        def card(project: str | None, overview: Memory | None) -> str:
            s = stats.get(project)
            name = project or "未分專案"
            due = (
                f'<div class="meta">最近截止 {_day(s.next_due, today)}</div>'
                if s and s.next_due
                else ""
            )
            flags = ""
            if s and s.open > OPEN_WARN:
                flags += (
                    f'<div class="flag">進行中 {s.open} 筆，超過 {OPEN_WARN} 筆，該收斂了</div>'
                )
            if overview and overview.name in drifted:
                newer = "、".join(drifted[overview.name].newer_related)
                flags += f'<div class="flag">總覽落後於：{escape(newer)}</div>'
            mine = [o for o in orphans if project and o.name.startswith(f"{project}-")]
            if mine:
                names = "、".join(
                    f'<a href="/app/memory/{escape(o.name)}">{escape(o.name)}</a>' for o in mine
                )
                flags += (
                    '<details class="ov">'
                    f'<summary class="flag">{len(mine)} 筆記憶沒連到總覽</summary>'
                    f'<div class="meta">{names}</div></details>'
                )
            if s and s.stale_open:
                for t in s.stale_open:
                    flags += (
                        f'<div class="flag">「{escape(t.title)}」{tasks.stale_open_days} '
                        "天沒動，還要做嗎？</div>"
                    )
            return (
                f'<div class="list"><div class="row"><div class="title">'
                f'<a href="{_project_url(project)}">{escape(name)} ›</a></div>'
                f"{due}{_metrics(s)}{flags}</div></div>"
            )

        known = {o.name.removesuffix("-overview") for o in overviews}
        # Busiest projects first, so the page leads with where work is.
        ordered = sorted(
            overviews,
            key=lambda o: (
                -(stats[p].open if (p := o.name.removesuffix("-overview")) in stats else 0)
            ),
        )
        cards = [card(o.name.removesuffix("-overview"), o) for o in ordered]
        cards += [card(p, None) for p in stats if p not in known]
        body = "<h1>專案</h1>" + (
            "".join(cards) or '<div class="list"><div class="empty">沒有專案。</div></div>'
        )
        return _page("專案", "projects", body)

    async def project_page(request: Request) -> Response:
        if (denied := await guard(request)) is not None:
            return denied
        tasks = await get_tasks()
        memories = await get_memories()
        documents = await get_documents()
        raw = request.path_params["name"]
        project = None if raw == NO_PROJECT else raw
        today = await tasks.today()
        stats = await tasks.project_stats()
        overview = await memories.get(f"{project}-overview") if project else None
        if project and overview is None and project not in stats:
            return _page("找不到", "projects", '<div class="empty">找不到這個專案。</div>')
        back = here(request)

        found = await tasks.find(project=project, statuses=["open", "suggested", "done"])
        if project is None:
            found = [t for t in found if t.project is None]
        open_ = [t for t in found if t.status == "open"]
        suggested = [t for t in found if t.status == "suggested"]
        done = sorted(
            (t for t in found if t.status == "done"),
            key=lambda t: t.done_at or t.updated_at,
            reverse=True,
        )[:10]
        body = '<a class="back" href="/app/projects">‹ 專案</a>'
        body += f"<h1>{escape(project or '未分專案')}</h1>"
        body += await notices(request, tasks)
        body += (
            f'<div class="list"><div class="row flush">{_metrics(stats.get(project))}</div></div>'
        )
        body += _section(
            "進行中",
            [_task_card(t, today, back=back) for t in _by_priority(open_)],
            empty="沒有進行中的事。",
        )
        body += _section(
            "AI 建議待確認", [_suggestion_card(t, today, back=back) for t in suggested]
        )
        body += _section("最近完成", [_done_card(t, back=back) for t in done])
        if overview:
            body += _section(
                "總覽",
                [
                    f'<div class="row"><div class="body">{_inline_md(overview.content)}</div>'
                    f'<div class="meta foot">'
                    f'<a href="/app/memory/{escape(overview.name)}">{escape(overview.name)}</a>'
                    f"・{overview.updated_at:%Y-%m-%d} 更新</div></div>"
                ],
                counted=False,
            )
        if project:
            docs = await documents.list_all(project=project)
            body += _section(
                "文件",
                [
                    f'<a class="row" href="/app/docs/{escape(d.name)}"><div class="title">'
                    f'{escape(d.title)}</div><div class="meta">{d.chars} 字・'
                    f"{d.updated_at:%Y-%m-%d} 更新</div></a>"
                    for d in docs
                ],
            )
        return _page(project or "未分專案", "projects", body)

    async def library_page(request: Request) -> Response:
        if (denied := await guard(request)) is not None:
            return denied
        memories = await get_memories()
        documents = await get_documents()
        q = request.query_params.get("q", "").strip()
        body = (
            "<h1>資料</h1>"
            '<form class="search" method="get" action="/app/library">'
            f'<input type="search" name="q" value="{escape(q)}" placeholder="搜尋記憶" '
            'aria-label="搜尋記憶" enterkeyhint="search">'
            '<button class="primary">搜尋</button></form>'
        )

        if q:
            found = await memories.recall(q, limit=20, track_usage=False)
            body += _section(
                "搜尋結果",
                [_memory_row(m.name, m.description, m.type) for m in found],
                empty="沒有符合的記憶。",
            )
            return _page(f"搜尋 {q}", "library", body)
        category = request.query_params.get("c", "")
        if category not in CATEGORY_LABELS:
            category = ""
        everything = await memories.list_all()
        counts = Counter(m.category for m in everything)
        chips = [("", "全部", len(everything))] + [
            (key, label, counts[key]) for key, label in CATEGORY_LABELS.items() if counts[key]
        ]
        body += (
            '<div class="chips" role="navigation" aria-label="記憶分類">'
            + "".join(
                f'<a href="{_with("/app/library", c=key) if key else "/app/library"}"'
                f"{' class=on aria-current=page' if key == category else ''}>"
                f"{label}<span>{n}</span></a>"
                for key, label, n in chips
            )
            + "</div>"
        )
        if category:
            body += _section(
                CATEGORY_LABELS[category],
                [
                    _memory_row(m.name, m.description, m.type)
                    for m in everything
                    if m.category == category
                ],
            )
            return _page(CATEGORY_LABELS[category], "library", body)
        docs = await documents.list_all()
        body += _section(
            "文件",
            [
                f'<a class="row" href="/app/docs/{escape(d.name)}"><div class="title">'
                f'{escape(d.title)}</div><div class="meta">'
                f"{escape(d.project) + '・' if d.project else ''}"
                f"{d.chars} 字・{d.updated_at:%Y-%m-%d} 更新</div></a>"
                for d in docs
            ],
            empty="還沒有文件。",
        )
        body += _section(
            "所有記憶・最近更新在前",
            [_memory_row(m.name, m.description, m.type) for m in everything],
        )
        return _page("資料", "library", body)

    async def me_page(request: Request) -> Response:
        if (denied := await guard(request)) is not None:
            return denied
        memories = await get_memories()
        profile = await memories.get(PROFILE)
        mine = [m for m in await memories.list_all(category="you") if m.name != PROFILE]
        body = "<h1>關於我</h1>"
        if profile is None:
            body += (
                '<div class="list"><div class="empty">還沒有個人總覽。跟 AI 說'
                "「幫我整理 Axiom 的關於我」，它會從下面的記憶整理出來。</div></div>"
            )
        else:
            body += f'<div class="meta">AI 整理・{profile.updated_at:%Y-%m-%d} 更新</div>'
            newer = [m for m in mine if m.updated_at > profile.updated_at]
            if newer:
                names = "、".join(
                    f'<a href="/app/memory/{escape(m.name)}">{escape(m.name)}</a>' for m in newer
                )
                body += (
                    '<details class="ov"><summary class="flag">'
                    f"有 {len(newer)} 筆關於你的記憶比總覽新，可以請 AI 重新整理</summary>"
                    f'<div class="meta">{names}</div></details>'
                )
            for heading, text in _profile_sections(profile.content):
                card = f'<div class="row">{_prose(text)}</div>'
                body += (
                    _section(escape(heading), [card], counted=False)
                    if heading
                    else (f'<div class="list">{card}</div>')
                )
        groups: dict[str, list[str]] = {}
        for m in mine:
            groups.setdefault(m.type, []).append(_memory_row(m.name, m.description))
        for kind, label in TYPE_LABELS.items():
            body += _section(label, groups.get(kind, []))
        body += (
            '<div class="meta foot">想更新時，跟 ChatGPT 或 Claude 說'
            "「幫我整理 Axiom 的關於我」。</div>"
        )
        return _page("關於我", "me", body)

    async def memory_page(request: Request) -> Response:
        if (denied := await guard(request)) is not None:
            return denied
        memories = await get_memories()
        memory = await memories.get(request.path_params["name"])
        back = '<a class="back" href="/app/library">‹ 資料</a>'
        if memory is None:
            return _page(
                "找不到",
                "library",
                back + '<div class="list"><div class="empty">找不到這筆記憶。</div></div>',
            )
        related = "、".join(
            f'<a href="/app/memory/{escape(n)}">{escape(n)}</a>' for n in memory.related
        )
        body = (
            f"{back}<h1>{escape(memory.name)}</h1>"
            f'<div class="meta">{escape(memory.description)}</div>'
            f'<div class="tags"><span class="tag">{memory.type}</span>'
            f'<span class="tag">{memory.category}</span>'
            f"<span>{memory.updated_at:%Y-%m-%d} 更新</span></div>"
            f'<div class="list"><div class="row"><div class="body">'
            f"{_inline_md(memory.content)}</div></div>"
            + (f'<div class="row meta">相關：{related}</div>' if related else "")
            + "</div>"
        )
        return _page(memory.name, "library", body)

    async def document_page(request: Request) -> Response:
        if (denied := await guard(request)) is not None:
            return denied
        documents = await get_documents()
        doc = await documents.get(request.path_params["name"])
        back = '<a class="back" href="/app/library">‹ 資料</a>'
        if doc is None:
            return _page(
                "找不到",
                "library",
                back + '<div class="list"><div class="empty">找不到這份文件。</div></div>',
            )
        project = (
            f'<a class="tag" href="{_project_url(doc.project)}">{escape(doc.project)}</a>'
            if doc.project
            else ""
        )
        body = (
            f"{back}<h1>{escape(doc.title)}</h1>"
            f'<div class="tags">{project}<span>{escape(doc.name)}</span>'
            f"<span>{len(doc.body)} 字・{doc.updated_at:%Y-%m-%d} 更新</span></div>"
            f'<div class="list"><div class="row"><div class="body">{_inline_md(doc.body)}'
            "</div></div></div>"
        )
        return _page(doc.title, "library", body)

    async def task_page(request: Request) -> Response:
        if (denied := await guard(request)) is not None:
            return denied
        tasks = await get_tasks()
        task = await tasks.get(int(request.path_params["task_id"]))
        back = back_to(request.query_params.get("from"))
        top = f'<a class="back" href="{escape(back)}">‹ 返回</a>'
        if task is None:
            return _page("找不到", "", top + '<div class="empty">找不到這件事。</div>')
        today = await tasks.today()
        projects = await tasks.known_projects()
        body = (
            f"{top}<h1>{escape(task.title)}</h1>"
            f'<div class="tags"><span class="tag">{STATUS_LABELS.get(task.status, task.status)}'
            f"</span><span>{_meta_text(task, today)}</span></div>"
        )
        body += await notices(request, tasks)
        if task.status == "open":
            body += (
                f'<div class="list"><div class="row"><div class="actions flush">'
                f"{_task_actions(task, today, back=back)}</div></div></div>"
            )
        elif task.status == "suggested":
            body += f'<div class="list">{_suggestion_card(task, today, back=back)}</div>'
        elif task.status in ("done", "dropped"):
            when = f"{task.done_at:%Y-%m-%d} 完成・" if task.done_at else ""
            body += (
                f'<div class="list"><div class="row"><div class="actions flush">'
                f'<span class="meta grow">{when}可以改回進行中</span>'
                f"{_button(task.id, 'reopen', '復原', back=here(request))}</div></div></div>"
            )
        if task.status in ("open", "suggested"):

            def date_value(d: date | None) -> str:
                return d.isoformat() if d else ""

            body += (
                "<h2>編輯</h2>"
                f'<form method="post" action="/app/tasks/{task.id}/edit" class="list">'
                f'<input type="hidden" name="next" value="{escape(here(request))}">'
                '<div class="row"><div class="fields">'
                '<label class="field full">標題<input type="text" name="title" required '
                f'value="{escape(task.title)}"></label>'
                '<label class="field">計畫日期<input type="date" name="planned_date" '
                f'value="{date_value(task.planned_date)}"></label>'
                '<label class="field">截止日<input type="date" name="due_date" '
                f'value="{date_value(task.due_date)}"></label>'
                '<label class="field full">專案<select name="project">'
                f"{_project_options(projects, task.project, none_label='不分專案')}"
                "</select></label>"
                '</div><div class="actions"><button class="primary">儲存</button></div></div>'
                "</form>"
            )
        if task.evidence and task.status != "suggested":
            ev = task.evidence
            if ev.memories:
                links = "、".join(
                    f'<a href="/app/memory/{escape(n)}">{escape(n)}</a>' for n in ev.memories
                )
                body += f'<div class="meta">AI 建議，依據：{links}</div>'
        body += (
            f'<div class="meta foot">{task.created_at:%Y-%m-%d} 建立'
            f"・{task.updated_at:%Y-%m-%d} 更新</div>"
        )
        return _page(task.title, "", body)

    async def new_task(request: Request) -> Response:
        if (denied := await guard(request)) is not None:
            return denied
        form = await request.form()
        back = back_to(str(form.get("next") or ""))
        tasks = await get_tasks()
        today = await tasks.today()
        when = str(form.get("when") or "")
        planned = {"today": today, "tomorrow": today + timedelta(days=1)}.get(when)
        try:
            await tasks.add(
                title=str(form.get("title") or ""),
                project=str(form.get("project") or "") or None,
                planned_date=planned,
                someday=when == "someday",
            )
        except TaskError as e:
            return redirect(back, error=str(e))
        return redirect(back)

    async def bulk_action(request: Request) -> Response:
        if (denied := await guard(request)) is not None:
            return denied
        form = await request.form()
        to = str(form.get("to") or "")
        tasks = await get_tasks()
        view = await tasks.today_view()
        try:
            for t in view.overdue:
                await tasks.defer(t.id, to=view.today if to == "today" else "someday")
        except TaskError as e:
            return redirect("/app", error=str(e))
        return redirect("/app")

    async def task_action(request: Request) -> Response:
        if (denied := await guard(request)) is not None:
            return denied
        form = await request.form()
        back = back_to(str(form.get("next") or ""))
        tasks = await get_tasks()
        task_id = int(request.path_params["task_id"])
        action = request.path_params["action"]
        try:
            if action == "done":
                await tasks.done(task_id)
            elif action == "drop":
                await tasks.drop(task_id)
            elif action == "reopen":
                await tasks.reopen(task_id)
            elif action == "confirm":
                await tasks.confirm(task_id)
            elif action == "reject":
                await tasks.reject(task_id, reason=str(form.get("reason") or ""))
            elif action == "defer":
                to = str(form.get("to") or "")
                await tasks.defer(
                    task_id,
                    to=to if to in ("tomorrow", "someday") else date.fromisoformat(to),  # type: ignore[arg-type]
                )
            elif action == "edit":
                fields = {
                    k: str(form.get(k) or "").strip()
                    for k in ("title", "project", "planned_date", "due_date")
                }
                await tasks.update(
                    task_id,
                    title=fields["title"],
                    project=fields["project"] or None,
                    planned_date=date.fromisoformat(fields["planned_date"])
                    if fields["planned_date"]
                    else None,
                    due_date=date.fromisoformat(fields["due_date"]) if fields["due_date"] else None,
                    clear=[k for k in ("project", "planned_date", "due_date") if not fields[k]],
                )
            else:
                return HTMLResponse("Unknown action.", status_code=404)
        except (TaskError, ValueError) as e:
            return redirect(back, error=str(e))
        if action in ("done", "drop"):
            return redirect(back, undo=task_id)
        return redirect(back)

    async def login_page(request: Request) -> Response:
        if not auth.configured:
            return HTMLResponse("App auth is not configured.", status_code=503)
        error = ""
        if request.method == "POST":
            form = await request.form()
            if auth.check_token(str(form.get("token") or "")):
                response = RedirectResponse("/app", status_code=303)
                response.set_cookie(
                    AppAuth.COOKIE,
                    auth.session_value(),
                    max_age=60 * 60 * 24 * 30,
                    path="/app",
                    httponly=True,
                    secure=request.headers.get("x-forwarded-proto", request.url.scheme) == "https",
                    samesite="strict",
                )
                return response
            error = '<div class="err">不對。</div>'
        elif await auth.identify(request.headers, request.cookies):
            return RedirectResponse("/app", status_code=303)
        body = (
            f'<div class="login"><h1>登入 Axiom</h1>'
            '<div class="meta">輸入 service token，這台裝置會記住 30 天。</div>'
            f'{error}<form method="post"><input type="password" name="token" '
            'placeholder="Service token" aria-label="Service token" '
            'autocomplete="current-password">'
            '<button class="primary">進入</button></form></div>'
        )
        return _page("登入", "", body)

    # The manifest and icons hold nothing private and are fetched by the
    # browser's install flow, which may not carry the session.
    async def manifest(request: Request) -> Response:
        return Response(_MANIFEST, media_type="application/manifest+json")

    def icon(size: int) -> Callable[[Request], Awaitable[Response]]:
        async def serve(request: Request) -> Response:
            return Response(
                _ICONS[size], media_type="image/png", headers={"cache-control": "max-age=86400"}
            )

        return serve

    return [
        Route("/app", today_page, methods=["GET"]),
        Route("/app/week", week_page, methods=["GET"]),
        Route("/app/projects", projects_page, methods=["GET"]),
        Route("/app/projects/{name}", project_page, methods=["GET"]),
        Route("/app/library", library_page, methods=["GET"]),
        Route("/app/me", me_page, methods=["GET"]),
        Route("/app/memory/{name}", memory_page, methods=["GET"]),
        Route("/app/docs/{name}", document_page, methods=["GET"]),
        Route("/app/tasks/new", new_task, methods=["POST"]),
        Route("/app/tasks/bulk", bulk_action, methods=["POST"]),
        Route("/app/tasks/{task_id:int}", task_page, methods=["GET"]),
        Route("/app/tasks/{task_id:int}/{action}", task_action, methods=["POST"]),
        Route("/app/login", login_page, methods=["GET", "POST"]),
        Route("/app/manifest.webmanifest", manifest, methods=["GET"]),
        Route("/app/icon-180.png", icon(180), methods=["GET"]),
        Route("/app/icon-512.png", icon(512), methods=["GET"]),
    ]
