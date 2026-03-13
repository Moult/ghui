#!/usr/bin/env python3
"""yolo - Browse GitHub issues and PRs in your terminal."""

import argparse
import curses
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import textwrap
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path


# --- Cache ---

CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "yolo"


def _cache_key(parts: list[str]) -> Path:
    raw = ":".join(parts)
    h = hashlib.sha256(raw.encode()).hexdigest()[:16]
    safe = "_".join(p.replace("/", "_") for p in parts[:3])
    return CACHE_DIR / f"{safe}_{h}.json"


def cache_path_list(repo: str, kind: str, state_filter: str, search: str) -> Path:
    return _cache_key([repo, kind, state_filter, search])


def cache_path_detail(repo: str, kind: str, number: int) -> Path:
    return _cache_key([repo, kind, "detail", str(number)])


def load_cache(path: Path):
    if not path.exists():
        return None, None
    try:
        data = json.loads(path.read_text())
        return data.get("data"), data.get("fetched_at", "")
    except (json.JSONDecodeError, OSError):
        return None, None


def save_cache(path: Path, data):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "data": data,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }))


# --- GitHub CLI wrapper ---

class GH:
    def __init__(self, repo: str):
        self.repo = repo

    def _run(self, *args: str) -> str:
        result = subprocess.run(
            ["gh", *args, "-R", self.repo],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip())
        return result.stdout

    def _json(self, *args: str):
        return json.loads(self._run(*args))

    def list_issues(self, state: str = "open", search: str = "", limit: int = 100):
        cmd = [
            "issue", "list",
            "--json", "number,title,author,labels,state,updatedAt,createdAt,comments",
            "--state", state, "-L", str(limit),
        ]
        if search:
            cmd += ["--search", search]
        return self._json(*cmd)

    def list_prs(self, state: str = "open", search: str = "", limit: int = 100):
        cmd = [
            "pr", "list",
            "--json", "number,title,author,labels,state,updatedAt,createdAt,comments,isDraft,reviewDecision",
            "--state", state, "-L", str(limit),
        ]
        if search:
            cmd += ["--search", search]
        return self._json(*cmd)

    def view_issue(self, number: int):
        return self._json(
            "issue", "view", str(number),
            "--json", "number,title,body,author,labels,state,comments,createdAt,updatedAt,url",
        )

    def view_pr(self, number: int):
        return self._json(
            "pr", "view", str(number),
            "--json", "number,title,body,author,labels,state,comments,createdAt,updatedAt,url,isDraft,reviewDecision,additions,deletions,changedFiles",
        )

    def comment(self, kind: str, number: int, body_file: str):
        self._run(kind, "comment", str(number), "--body-file", body_file)

    def close(self, kind: str, number: int):
        self._run(kind, "close", str(number))

    def reopen(self, kind: str, number: int):
        self._run(kind, "reopen", str(number))

    def open_in_browser(self, kind: str, number: int):
        subprocess.run(
            ["gh", kind, "view", str(number), "--web", "-R", self.repo],
            capture_output=True,
        )

    @staticmethod
    def detect_repo() -> str:
        result = subprocess.run(
            ["gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"],
            capture_output=True, text=True,
        )
        return result.stdout.strip() if result.returncode == 0 else ""


# --- Helpers ---

SORT_KEYS = [
    ("updatedAt", "updated"),
    ("createdAt", "created"),
    ("comments", "comments"),
]


@dataclass
class AppState:
    mode: str = "list"  # "list", "detail", "search", "goto"
    kind: str = "issue"  # "issue" or "pr"
    items: list = field(default_factory=list)
    cursor: int = 0
    scroll_offset: int = 0
    detail_scroll: int = 0
    detail_item: dict = None
    detail_lines: list = field(default_factory=list)
    state_filter: str = "open"
    search_query: str = ""
    search_buf: str = ""
    goto_buf: str = ""
    sort_idx: int = 0
    sort_reverse: bool = True
    status_msg: str = ""
    loading: bool = False
    fetched_at: str = ""
    draft_body: str = ""  # pending comment draft
    draft_tmppath: str = ""  # temp file for draft


def relative_time(iso: str) -> str:
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        delta = datetime.now(timezone.utc) - dt
        secs = int(delta.total_seconds())
        if secs < 0:
            return "now"
        if secs < 60:
            return f"{secs}s"
        if secs < 3600:
            return f"{secs // 60}m"
        if secs < 86400:
            return f"{secs // 3600}h"
        if secs < 2592000:
            return f"{secs // 86400}d"
        if secs < 31536000:
            return f"{secs // 2592000}mo"
        return f"{secs // 31536000}y"
    except Exception:
        return ""


def format_fetch_time(iso: str) -> str:
    if not iso:
        return "never"
    try:
        dt = datetime.fromisoformat(iso)
        return dt.astimezone().strftime("%Y-%m-%d %H:%M")
    except Exception:
        return "?"


def comment_count(item: dict) -> int:
    c = item.get("comments", [])
    return len(c) if isinstance(c, list) else (int(c) if c else 0)


def sort_items(items: list, sort_key: str, reverse: bool) -> list:
    if sort_key == "comments":
        return sorted(items, key=lambda x: comment_count(x), reverse=reverse)
    return sorted(items, key=lambda x: x.get(sort_key, ""), reverse=reverse)


def author_login(item: dict) -> str:
    a = item.get("author")
    if isinstance(a, dict):
        return a.get("login", "")
    return str(a) if a else ""


def label_names(item: dict) -> str:
    labels = item.get("labels", [])
    if not labels:
        return ""
    return ",".join(
        lb.get("name", "") if isinstance(lb, dict) else str(lb)
        for lb in labels
    )


def state_indicator(item: dict) -> tuple[str, int]:
    """Returns (symbol, color_pair) for open/closed/merged state."""
    s = item.get("state", "").upper()
    if s == "OPEN":
        return ("●", C_OPEN)
    elif s == "MERGED":
        return ("◆", C_PR_NUM)  # magenta
    else:  # CLOSED
        return ("○", C_CLOSED)


# --- Colors ---

C_TITLE = 1
C_SELECTED = 2
C_HELP = 3
C_ISSUE_NUM = 4
C_PR_NUM = 5
C_LABEL = 6
C_COMMENT_HDR = 7
C_OPEN = 8
C_CLOSED = 9
C_DRAFT = 10


def init_colors():
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(C_TITLE, curses.COLOR_BLACK, curses.COLOR_CYAN)
    curses.init_pair(C_SELECTED, curses.COLOR_BLACK, curses.COLOR_WHITE)
    curses.init_pair(C_HELP, curses.COLOR_BLACK, curses.COLOR_CYAN)
    curses.init_pair(C_ISSUE_NUM, curses.COLOR_GREEN, -1)
    curses.init_pair(C_PR_NUM, curses.COLOR_MAGENTA, -1)
    curses.init_pair(C_LABEL, curses.COLOR_YELLOW, -1)
    curses.init_pair(C_COMMENT_HDR, curses.COLOR_CYAN, -1)
    curses.init_pair(C_OPEN, curses.COLOR_GREEN, -1)
    curses.init_pair(C_CLOSED, curses.COLOR_RED, -1)
    curses.init_pair(C_DRAFT, curses.COLOR_BLACK, curses.COLOR_YELLOW)


def safe_addstr(win, y, x, text, attr=0):
    h, w = win.getmaxyx()
    if y < 0 or y >= h or x >= w:
        return
    max_len = w - x - 1
    if max_len <= 0:
        return
    try:
        win.addnstr(y, x, text, max_len, attr)
    except curses.error:
        pass


# --- Drawing ---

def draw_title_bar(stdscr, state: AppState, repo: str):
    h, w = stdscr.getmaxyx()
    tab = "Issues" if state.kind == "issue" else "PRs"
    count = len(state.items)
    fetched = format_fetch_time(state.fetched_at)
    title = f" yolo - {repo}"
    right = f" {tab} | {state.state_filter} | {count} | fetched {fetched} "
    pad = w - len(title) - len(right)
    if pad < 0:
        pad = 0
    bar = title + " " * pad + right
    safe_addstr(stdscr, 0, 0, bar.ljust(w), curses.color_pair(C_TITLE) | curses.A_BOLD)


def draw_filter_bar(stdscr, state: AppState):
    h, w = stdscr.getmaxyx()
    sort_name = SORT_KEYS[state.sort_idx][1]
    sort_dir = "desc" if state.sort_reverse else "asc"
    search_part = f" Search: {state.search_query}" if state.search_query else ""
    right = f"Sort: {sort_name} {sort_dir} "
    left = search_part or ""
    pad = w - len(left) - len(right)
    if pad < 0:
        pad = 0
    bar = left + " " * pad + right
    safe_addstr(stdscr, 1, 0, bar.ljust(w), curses.A_DIM)


def draw_list_view(stdscr, state: AppState, repo: str):
    h, w = stdscr.getmaxyx()
    stdscr.erase()
    draw_title_bar(stdscr, state, repo)
    draw_filter_bar(stdscr, state)

    if state.loading:
        safe_addstr(stdscr, h // 2, w // 2 - 5, "Loading...", curses.A_BOLD)
        stdscr.refresh()
        return

    if not state.items:
        safe_addstr(stdscr, h // 2, w // 2 - 7, "No items found", curses.A_DIM)

    list_h = h - 4
    if list_h < 1:
        list_h = 1

    if state.cursor < state.scroll_offset:
        state.scroll_offset = state.cursor
    if state.cursor >= state.scroll_offset + list_h:
        state.scroll_offset = state.cursor - list_h + 1

    # Column widths: indicator + number + title + author + labels + time + comments
    ind_col = 2
    num_col = 7
    author_col = min(16, max(8, w // 8))
    time_col = 6
    cmts_col = 6
    label_col = min(18, max(8, w // 7))
    title_col = w - ind_col - num_col - author_col - time_col - cmts_col - label_col - 4

    for i in range(list_h):
        idx = state.scroll_offset + i
        if idx >= len(state.items):
            break
        item = state.items[idx]
        row = i + 2

        is_selected = idx == state.cursor
        attr = curses.color_pair(C_SELECTED) if is_selected else 0

        if is_selected:
            safe_addstr(stdscr, row, 0, " " * (w - 1), attr)

        x = 1

        # State indicator (● open, ○ closed, ◆ merged)
        sym, sym_color = state_indicator(item)
        sym_attr = curses.color_pair(sym_color) if not is_selected else attr
        safe_addstr(stdscr, row, x, sym, sym_attr | curses.A_BOLD)
        x += ind_col

        # Number
        num = f"#{item.get('number', '')}"
        num_color = C_PR_NUM if state.kind == "pr" else C_ISSUE_NUM
        num_attr = curses.color_pair(num_color) if not is_selected else attr
        safe_addstr(stdscr, row, x, num.rjust(num_col - 1), num_attr | curses.A_BOLD)
        x += num_col

        # Title
        title = item.get("title", "")[:title_col]
        safe_addstr(stdscr, row, x, title, attr)
        x += title_col + 1

        # Author
        author = author_login(item)[:author_col]
        safe_addstr(stdscr, row, x, author, attr | curses.A_DIM)
        x += author_col + 1

        # Labels
        lbls = label_names(item)[:label_col]
        lbl_attr = curses.color_pair(C_LABEL) if not is_selected else attr
        safe_addstr(stdscr, row, x, lbls, lbl_attr)

        # Time (right-aligned)
        t = relative_time(item.get("updatedAt", ""))
        safe_addstr(stdscr, row, w - cmts_col - time_col - 2, t.rjust(time_col), attr | curses.A_DIM)

        # Comments (right-aligned)
        cc = comment_count(item)
        cc_str = f"{cc}c" if cc else ""
        safe_addstr(stdscr, row, w - cmts_col - 1, cc_str.rjust(cmts_col - 1), attr | curses.A_DIM)

    # Overlay for search/goto
    if state.mode == "search":
        prompt = f" /{state.search_buf}█"
        safe_addstr(stdscr, h - 2, 0, prompt.ljust(w), curses.A_BOLD)
    elif state.mode == "goto":
        prompt = f" #{state.goto_buf}█"
        safe_addstr(stdscr, h - 2, 0, prompt.ljust(w), curses.A_BOLD)
    elif state.status_msg:
        safe_addstr(stdscr, h - 2, 0, f" {state.status_msg}"[:w], curses.A_DIM)

    # Help bar
    if state.mode == "search":
        help_text = " Enter:search  Esc:cancel"
    elif state.mode == "goto":
        help_text = " Enter:go  Esc:cancel"
    else:
        help_text = " j/k:move  o/Enter:view  B:browser  #:goto  i/p:issues/PRs  /:search  f:filter  s:sort  r:refresh  q:quit"
    safe_addstr(stdscr, h - 1, 0, help_text.ljust(w)[:w], curses.color_pair(C_HELP))

    stdscr.refresh()


def build_detail_lines(item: dict, width: int, kind: str) -> list:
    lines = []
    w = max(width - 4, 20)

    title = f"#{item.get('number', '')}  {item.get('title', '')}"
    lines.extend(textwrap.wrap(title, w) or [title])
    lines.append("")

    st = item.get("state", "").upper()
    author = author_login(item)
    created = relative_time(item.get("createdAt", ""))
    lbls = label_names(item)
    meta = f"@{author}  opened {created} ago  |  {st}"
    if lbls:
        meta += f"  |  labels: {lbls}"
    if kind == "pr":
        adds = item.get("additions", 0)
        dels = item.get("deletions", 0)
        files = item.get("changedFiles", 0)
        draft = " DRAFT" if item.get("isDraft") else ""
        review = item.get("reviewDecision", "")
        meta += f"  |  +{adds}/-{dels} ({files} files){draft}"
        if review:
            meta += f"  |  {review}"
    lines.extend(textwrap.wrap(meta, w) or [meta])
    lines.append("")
    lines.append("─" * min(w, 60))
    lines.append("")

    body = item.get("body", "") or "(no description)"
    for paragraph in body.split("\n"):
        if paragraph.strip() == "":
            lines.append("")
        else:
            lines.extend(textwrap.wrap(paragraph, w) or [paragraph])
    lines.append("")

    comments = item.get("comments", [])
    if isinstance(comments, list) and comments:
        lines.append("─" * min(w, 60))
        for cm in comments:
            lines.append("")
            cm_author = cm.get("author", {}).get("login", "?") if isinstance(cm.get("author"), dict) else "?"
            cm_time = relative_time(cm.get("createdAt", ""))
            lines.append(f"── {cm_author} ({cm_time} ago) ──")
            lines.append("")
            cm_body = cm.get("body", "")
            for p in cm_body.split("\n"):
                if p.strip() == "":
                    lines.append("")
                else:
                    lines.extend(textwrap.wrap(p, w) or [p])

    return lines


def draw_detail_view(stdscr, state: AppState, repo: str):
    h, w = stdscr.getmaxyx()
    stdscr.erase()

    if not state.detail_lines:
        safe_addstr(stdscr, 0, 0, "No content", curses.A_DIM)
        stdscr.refresh()
        return

    item = state.detail_item
    num = item.get("number", "")
    title = item.get("title", "")
    st = item.get("state", "").upper()
    bar = f" #{num}  {title}"
    safe_addstr(stdscr, 0, 0, bar.ljust(w)[:w], curses.color_pair(C_TITLE) | curses.A_BOLD)

    # State indicator on the right of title bar
    st_color = C_OPEN if st == "OPEN" else C_CLOSED
    st_label = f" {st} "
    safe_addstr(stdscr, 0, w - len(st_label) - 1, st_label, curses.color_pair(st_color) | curses.A_BOLD)

    # Draft banner
    has_draft = bool(state.draft_body)
    banner_h = 0
    if has_draft:
        banner_h = 1
        preview = state.draft_body.replace("\n", " ")[:w - 30]
        draft_text = f" DRAFT: {preview}   y:post  e:edit  n:discard "
        safe_addstr(stdscr, 1, 0, draft_text.ljust(w)[:w], curses.color_pair(C_DRAFT) | curses.A_BOLD)

    # Content area
    content_start = 1 + banner_h
    content_h = h - content_start - 1  # -1 for help bar
    if content_h < 1:
        content_h = 1
    max_scroll = max(0, len(state.detail_lines) - content_h)
    if state.detail_scroll > max_scroll:
        state.detail_scroll = max_scroll
    if state.detail_scroll < 0:
        state.detail_scroll = 0

    for i in range(content_h):
        line_idx = state.detail_scroll + i
        if line_idx >= len(state.detail_lines):
            break
        line = state.detail_lines[line_idx]
        row = content_start + i

        if line.startswith("──"):
            safe_addstr(stdscr, row, 2, line[:w - 3], curses.color_pair(C_COMMENT_HDR))
        elif line.startswith("─"):
            safe_addstr(stdscr, row, 2, line[:w - 3], curses.A_DIM)
        else:
            safe_addstr(stdscr, row, 2, line[:w - 3])

    # Scroll indicator
    if len(state.detail_lines) > content_h:
        pct = int(state.detail_scroll / max(max_scroll, 1) * 100)
        safe_addstr(stdscr, 0, w - len(st_label) - 7, f" {pct}% ", curses.color_pair(C_TITLE))

    # Help bar
    if has_draft:
        help_text = " j/k:scroll  y:post draft  e:edit draft  n:discard draft  B:browser  q:back"
    else:
        help_text = " j/k:scroll  c:comment  O:reopen  C:close  B:browser  r:refresh  q:back"
    safe_addstr(stdscr, h - 1, 0, help_text.ljust(w)[:w], curses.color_pair(C_HELP))

    if state.status_msg and not has_draft:
        safe_addstr(stdscr, h - 2, 0, f" {state.status_msg}"[:w], curses.A_DIM)

    stdscr.refresh()


# --- Data operations ---

def fetch_list(gh: GH, state: AppState, force: bool = False):
    """Fetch list from cache or network."""
    cp = cache_path_list(gh.repo, state.kind, state.state_filter, state.search_query)
    if not force:
        data, fetched_at = load_cache(cp)
        if data is not None:
            sort_key = SORT_KEYS[state.sort_idx][0]
            state.items = sort_items(data, sort_key, state.sort_reverse)
            state.fetched_at = fetched_at
            state.cursor = 0
            state.scroll_offset = 0
            state.status_msg = ""
            return

    state.loading = True
    try:
        if state.kind == "issue":
            items = gh.list_issues(state=state.state_filter, search=state.search_query)
        else:
            items = gh.list_prs(state=state.state_filter, search=state.search_query)
        save_cache(cp, items)
        sort_key = SORT_KEYS[state.sort_idx][0]
        state.items = sort_items(items, sort_key, state.sort_reverse)
        state.fetched_at = datetime.now(timezone.utc).isoformat()
        state.cursor = 0
        state.scroll_offset = 0
        state.status_msg = "Refreshed"
    except RuntimeError as e:
        state.items = []
        state.status_msg = f"Error: {e}"[:80]
    state.loading = False


def fetch_detail(gh: GH, state: AppState, number: int, force: bool = False):
    """Fetch detail from cache or network."""
    cp = cache_path_detail(gh.repo, state.kind, number)
    if not force:
        data, _ = load_cache(cp)
        if data is not None:
            state.detail_item = data
            state.detail_lines = []
            state.detail_scroll = 0
            state.mode = "detail"
            return

    try:
        if state.kind == "issue":
            full = gh.view_issue(number)
        else:
            full = gh.view_pr(number)
        save_cache(cp, full)
        state.detail_item = full
        state.detail_lines = []
        state.detail_scroll = 0
        state.mode = "detail"
    except RuntimeError as e:
        state.status_msg = f"Error: {e}"[:80]


def open_detail(gh: GH, state: AppState, force: bool = False):
    if not state.items:
        return
    item = state.items[state.cursor]
    fetch_detail(gh, state, item.get("number"), force=force)


def goto_number(gh: GH, state: AppState, number: int):
    # Check if in current list
    for i, item in enumerate(state.items):
        if item.get("number") == number:
            state.cursor = i
            state.status_msg = ""
            return

    # Fetch directly
    fetch_detail(gh, state, number)
    if state.mode != "detail":
        # Try the other kind
        orig_kind = state.kind
        state.kind = "pr" if orig_kind == "issue" else "issue"
        fetch_detail(gh, state, number)
        if state.mode != "detail":
            state.kind = orig_kind
            state.status_msg = f"#{number} not found"


# --- Comment workflow ---

def start_comment(stdscr, state: AppState):
    """Open $EDITOR, then return to detail view with draft banner."""
    if not state.detail_item:
        return stdscr
    num = state.detail_item.get("number")
    kind = state.kind
    editor = os.environ.get("EDITOR", os.environ.get("VISUAL", "vi"))

    # Create or reuse temp file
    if state.draft_tmppath and os.path.exists(state.draft_tmppath):
        tmppath = state.draft_tmppath
    else:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False, prefix="yolo_comment_") as f:
            f.write(f"\n# Comment for {kind} #{num}\n# Lines starting with # are ignored.\n# Save and close to preview draft.\n")
            tmppath = f.name
        state.draft_tmppath = tmppath

    curses.endwin()
    subprocess.run([editor, tmppath])

    # Read and clean
    with open(tmppath) as f:
        body_lines = [ln for ln in f.readlines() if not ln.startswith("#")]
    body = "".join(body_lines).strip()

    stdscr = _reinit_curses(stdscr)

    if body:
        state.draft_body = body
        state.status_msg = ""
    else:
        state.draft_body = ""
        _cleanup_draft(state)
        state.status_msg = "Comment cancelled (empty)"

    return stdscr


def post_draft(gh: GH, state: AppState):
    """Post the current draft comment."""
    if not state.draft_body or not state.detail_item:
        return
    num = state.detail_item.get("number")
    kind = state.kind
    tmppath = state.draft_tmppath

    # Write clean body
    with open(tmppath, "w") as f:
        f.write(state.draft_body)
    try:
        gh.comment(kind, num, tmppath)
        state.status_msg = "Comment posted!"
        _cleanup_draft(state)
        # Refresh detail from network to show new comment
        cp = cache_path_detail(gh.repo, kind, num)
        try:
            if kind == "issue":
                full = gh.view_issue(num)
            else:
                full = gh.view_pr(num)
            save_cache(cp, full)
            state.detail_item = full
            state.detail_lines = []
        except RuntimeError:
            pass
    except RuntimeError as e:
        state.status_msg = f"Post failed: {e}"[:80]


def edit_draft(stdscr, state: AppState):
    """Re-open $EDITOR with the current draft."""
    if not state.draft_body or not state.draft_tmppath:
        return stdscr
    # Write current draft back
    with open(state.draft_tmppath, "w") as f:
        f.write(state.draft_body)
        num = state.detail_item.get("number", "?")
        kind = state.kind
        f.write(f"\n# Comment for {kind} #{num}\n# Lines starting with # are ignored.\n")

    editor = os.environ.get("EDITOR", os.environ.get("VISUAL", "vi"))
    curses.endwin()
    subprocess.run([editor, state.draft_tmppath])

    with open(state.draft_tmppath) as f:
        body_lines = [ln for ln in f.readlines() if not ln.startswith("#")]
    body = "".join(body_lines).strip()

    stdscr = _reinit_curses(stdscr)

    if body:
        state.draft_body = body
    else:
        state.draft_body = ""
        _cleanup_draft(state)
        state.status_msg = "Draft discarded (empty)"

    return stdscr


def discard_draft(state: AppState):
    _cleanup_draft(state)
    state.status_msg = "Draft discarded"


def _cleanup_draft(state: AppState):
    state.draft_body = ""
    if state.draft_tmppath and os.path.exists(state.draft_tmppath):
        os.unlink(state.draft_tmppath)
    state.draft_tmppath = ""


def _reinit_curses(stdscr):
    stdscr = curses.initscr()
    curses.noecho()
    curses.cbreak()
    stdscr.keypad(True)
    curses.curs_set(0)
    init_colors()
    return stdscr


# --- Main loop ---

def main_loop(stdscr, repo: str):
    curses.curs_set(0)
    init_colors()
    stdscr.timeout(-1)

    gh = GH(repo)
    state = AppState()

    # Load from cache
    fetch_list(gh, state, force=False)
    if not state.items and not state.fetched_at:
        fetch_list(gh, state, force=True)
        draw_list_view(stdscr, state, repo)

    while True:
        h, w = stdscr.getmaxyx()

        if state.mode in ("list", "search", "goto"):
            sort_key = SORT_KEYS[state.sort_idx][0]
            state.items = sort_items(state.items, sort_key, state.sort_reverse)
            draw_list_view(stdscr, state, repo)
        elif state.mode == "detail":
            if not state.detail_lines and state.detail_item:
                state.detail_lines = build_detail_lines(state.detail_item, w, state.kind)
            draw_detail_view(stdscr, state, repo)

        key = stdscr.getch()

        if key == curses.KEY_RESIZE:
            if state.mode == "detail" and state.detail_item:
                state.detail_lines = []
            continue

        # --- Search mode ---
        if state.mode == "search":
            if key == 27:
                state.mode = "list"
                state.search_buf = ""
            elif key in (curses.KEY_ENTER, 10, 13):
                state.search_query = state.search_buf
                state.search_buf = ""
                state.mode = "list"
                # Search uses cache if available, force only if no cache
                fetch_list(gh, state, force=False)
                if not state.items and not state.fetched_at:
                    fetch_list(gh, state, force=True)
            elif key in (curses.KEY_BACKSPACE, 127, 8):
                state.search_buf = state.search_buf[:-1]
            elif 32 <= key <= 126:
                state.search_buf += chr(key)
            continue

        # --- Goto mode ---
        if state.mode == "goto":
            if key == 27:
                state.mode = "list"
                state.goto_buf = ""
            elif key in (curses.KEY_ENTER, 10, 13):
                if state.goto_buf.isdigit():
                    goto_number(gh, state, int(state.goto_buf))
                state.goto_buf = ""
                if state.mode == "goto":
                    state.mode = "list"
            elif key in (curses.KEY_BACKSPACE, 127, 8):
                state.goto_buf = state.goto_buf[:-1]
            elif ord("0") <= key <= ord("9"):
                state.goto_buf += chr(key)
            continue

        # --- List mode ---
        if state.mode == "list":
            if key in (ord("q"), ord("Q")):
                break
            elif key in (ord("j"), curses.KEY_DOWN):
                if state.cursor < len(state.items) - 1:
                    state.cursor += 1
            elif key in (ord("k"), curses.KEY_UP):
                if state.cursor > 0:
                    state.cursor -= 1
            elif key in (ord("g"), curses.KEY_HOME):
                state.cursor = 0
            elif key in (ord("G"), curses.KEY_END):
                state.cursor = max(0, len(state.items) - 1)
            elif key in (ord("o"), curses.KEY_ENTER, 10, 13):
                open_detail(gh, state)
            elif key == ord("B"):
                if state.items:
                    item = state.items[state.cursor]
                    gh.open_in_browser(state.kind, item.get("number"))
                    state.status_msg = "Opened in browser"
            elif key == ord("i"):
                if state.kind != "issue":
                    state.kind = "issue"
                    state.state_filter = "open"
                    fetch_list(gh, state, force=False)
            elif key == ord("p"):
                if state.kind != "pr":
                    state.kind = "pr"
                    state.state_filter = "open"
                    fetch_list(gh, state, force=False)
            elif key == ord("/"):
                state.mode = "search"
                state.search_buf = state.search_query
            elif key == ord("#"):
                state.mode = "goto"
                state.goto_buf = ""
            elif key == ord("f"):
                if state.kind == "pr":
                    cycle = ["open", "closed", "merged", "all"]
                else:
                    cycle = ["open", "closed", "all"]
                idx = cycle.index(state.state_filter) if state.state_filter in cycle else 0
                state.state_filter = cycle[(idx + 1) % len(cycle)]
                fetch_list(gh, state, force=False)
            elif key == ord("s"):
                state.sort_idx = (state.sort_idx + 1) % len(SORT_KEYS)
            elif key == ord("r"):
                fetch_list(gh, state, force=True)

        # --- Detail mode ---
        elif state.mode == "detail":
            has_draft = bool(state.draft_body)

            if has_draft:
                # Draft mode keys
                if key in (ord("y"), ord("Y")):
                    post_draft(gh, state)
                elif key in (ord("e"), ord("E")):
                    stdscr = edit_draft(stdscr, state)
                elif key in (ord("n"), ord("N")):
                    discard_draft(state)
                elif key in (ord("j"), curses.KEY_DOWN):
                    state.detail_scroll += 1
                elif key in (ord("k"), curses.KEY_UP):
                    state.detail_scroll -= 1
                elif key in (ord("g"), curses.KEY_HOME):
                    state.detail_scroll = 0
                elif key in (ord("G"), curses.KEY_END):
                    content_h = h - 3
                    state.detail_scroll = max(0, len(state.detail_lines) - content_h)
                elif key == ord(" "):
                    content_h = h - 3
                    state.detail_scroll += content_h
                elif key == ord("B"):
                    if state.detail_item:
                        gh.open_in_browser(state.kind, state.detail_item.get("number"))
                elif key in (ord("q"), ord("Q")):
                    # Back to list, keep draft
                    state.mode = "list"
                    state.detail_item = None
                    state.detail_lines = []
            else:
                # Normal detail keys
                if key in (ord("q"), ord("Q")):
                    state.mode = "list"
                    state.detail_item = None
                    state.detail_lines = []
                elif key in (ord("j"), curses.KEY_DOWN):
                    state.detail_scroll += 1
                elif key in (ord("k"), curses.KEY_UP):
                    state.detail_scroll -= 1
                elif key in (ord("g"), curses.KEY_HOME):
                    state.detail_scroll = 0
                elif key in (ord("G"), curses.KEY_END):
                    content_h = h - 2
                    state.detail_scroll = max(0, len(state.detail_lines) - content_h)
                elif key == ord(" "):
                    content_h = h - 2
                    state.detail_scroll += content_h
                elif key == ord("c"):
                    stdscr = start_comment(stdscr, state)
                elif key == ord("O"):
                    if state.detail_item:
                        num = state.detail_item.get("number")
                        try:
                            gh.reopen(state.kind, num)
                            state.detail_item["state"] = "OPEN"
                            state.detail_lines = []
                            state.status_msg = f"Reopened #{num}"
                            # Invalidate caches
                            cp = cache_path_detail(gh.repo, state.kind, num)
                            save_cache(cp, state.detail_item)
                        except RuntimeError as e:
                            state.status_msg = f"Reopen failed: {e}"[:80]
                elif key == ord("C"):
                    if state.detail_item:
                        num = state.detail_item.get("number")
                        try:
                            gh.close(state.kind, num)
                            state.detail_item["state"] = "CLOSED"
                            state.detail_lines = []
                            state.status_msg = f"Closed #{num}"
                            cp = cache_path_detail(gh.repo, state.kind, num)
                            save_cache(cp, state.detail_item)
                        except RuntimeError as e:
                            state.status_msg = f"Close failed: {e}"[:80]
                elif key == ord("B"):
                    if state.detail_item:
                        gh.open_in_browser(state.kind, state.detail_item.get("number"))
                        state.status_msg = "Opened in browser"
                elif key == ord("r"):
                    if state.detail_item:
                        num = state.detail_item.get("number")
                        fetch_detail(gh, state, num, force=True)
                        state.status_msg = "Refreshed"


def main():
    parser = argparse.ArgumentParser(description="yolo - Browse GitHub issues and PRs in your terminal")
    parser.add_argument("repo", nargs="?", help="owner/repo (auto-detected from git remote if omitted)")
    args = parser.parse_args()

    repo = args.repo or GH.detect_repo()
    if not repo:
        print("Error: Could not detect repo. Pass owner/repo as argument or run from a git repo.", file=sys.stderr)
        sys.exit(1)

    try:
        subprocess.run(["gh", "auth", "status"], capture_output=True, check=True)
    except FileNotFoundError:
        print("Error: gh CLI not found. Install it from https://cli.github.com/", file=sys.stderr)
        sys.exit(1)
    except subprocess.CalledProcessError:
        print("Error: gh CLI not authenticated. Run 'gh auth login' first.", file=sys.stderr)
        sys.exit(1)

    curses.wrapper(main_loop, repo)


if __name__ == "__main__":
    main()
