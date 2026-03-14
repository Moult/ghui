#!/usr/bin/env python3
"""yolo - Browse GitHub issues and PRs in your terminal."""

import argparse
import curses
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import textwrap
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

try:
    from pygments.lexers import get_lexer_by_name, TextLexer
    from pygments.token import Token
    HAS_PYGMENTS = True
except ImportError:
    HAS_PYGMENTS = False


# --- Cache ---

CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "yolo"


def _cache_key(parts: list[str]) -> Path:
    raw = ":".join(parts)
    h = hashlib.sha256(raw.encode()).hexdigest()[:16]
    safe = "_".join(p.replace("/", "_") for p in parts[:3])
    return CACHE_DIR / f"{safe}_{h}.json"


def cache_path_list(repo, kind, state_filter, search, page=1):
    return _cache_key([repo, kind, state_filter, search, str(page)])


def cache_path_detail(repo, kind, number):
    return _cache_key([repo, kind, "detail", str(number)])


def cache_path_diff(repo, number):
    return _cache_key([repo, "diff", str(number)])


def load_cache(path):
    if not path.exists():
        return None, None
    try:
        data = json.loads(path.read_text())
        return data.get("data"), data.get("fetched_at", "")
    except (json.JSONDecodeError, OSError):
        return None, None


def save_cache(path, data):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"data": data, "fetched_at": datetime.now(timezone.utc).isoformat()}))


# --- GitHub CLI ---

class GH:
    def __init__(self, repo):
        self.repo = repo

    def _run(self, *args):
        r = subprocess.run(["gh", *args, "-R", self.repo], capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(r.stderr.strip())
        return r.stdout

    def _json(self, *args):
        return json.loads(self._run(*args))

    def _api_get(self, endpoint):
        r = subprocess.run(["gh", "api", endpoint], capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(r.stderr.strip())
        return json.loads(r.stdout)

    @staticmethod
    def _normalize_item(item):
        return {
            "number": item["number"],
            "title": item.get("title", ""),
            "author": item.get("user") or {},
            "labels": item.get("labels", []),
            "state": item.get("state", "").upper(),
            "updatedAt": item.get("updated_at", ""),
            "createdAt": item.get("created_at", ""),
            "comments": item.get("comments", 0),
            "isDraft": item.get("draft", False),
            "reviewDecision": "",
        }

    def _search(self, query, page=1, per_page=100):
        from urllib.parse import quote
        q = quote(f"repo:{self.repo} {query}")
        data = self._api_get(f"search/issues?q={q}&sort=updated&order=desc&per_page={per_page}&page={page}")
        items = [self._normalize_item(it) for it in data.get("items", [])]
        total = data.get("total_count", 0)
        return items, total

    def list_issues(self, state="open", search="", page=1, per_page=100):
        q = f"is:issue state:{state}"
        if search:
            q += f" {search}"
        return self._search(q, page, per_page)

    def list_prs(self, state="open", search="", page=1, per_page=100):
        if state == "merged":
            q = "is:pr is:merged"
        else:
            q = f"is:pr state:{state}"
        if search:
            q += f" {search}"
        return self._search(q, page, per_page)

    def view_issue(self, number):
        return self._json("issue", "view", str(number), "--json",
                          "number,title,body,author,labels,state,comments,createdAt,updatedAt,url")

    def view_pr(self, number):
        return self._json("pr", "view", str(number), "--json",
                          "number,title,body,author,labels,state,comments,createdAt,updatedAt,url,isDraft,reviewDecision,additions,deletions,changedFiles")

    def pr_diff(self, number):
        return self._run("pr", "diff", str(number))

    def comment(self, kind, number, body_file):
        self._run(kind, "comment", str(number), "--body-file", body_file)

    def merge_pr(self, number, method="merge"):
        flag = {"merge": "--merge", "rebase": "--rebase", "squash": "--squash"}[method]
        self._run("pr", "merge", str(number), flag, "--delete-branch")

    def close(self, kind, number):
        self._run(kind, "close", str(number))

    def reopen(self, kind, number):
        self._run(kind, "reopen", str(number))

    def open_in_browser(self, kind, number):
        subprocess.run(["gh", kind, "view", str(number), "--web", "-R", self.repo], capture_output=True)

    @staticmethod
    def detect_repo():
        r = subprocess.run(["gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"],
                           capture_output=True, text=True)
        return r.stdout.strip() if r.returncode == 0 else ""


# --- State ---

SORT_KEYS = [("updatedAt", "updated"), ("createdAt", "created"), ("comments", "comments")]


@dataclass
class AppState:
    mode: str = "list"  # list, detail, search, goto
    kind: str = "issue"
    items: list = field(default_factory=list)
    cursor: int = 0
    scroll_offset: int = 0
    detail_scroll: int = 0
    detail_item: dict = None
    detail_lines: list = field(default_factory=list)  # list of StyledLine
    state_filter: str = "open"
    search_query: str = ""
    search_buf: str = ""
    goto_buf: str = ""
    sort_idx: int = 0
    sort_reverse: bool = True
    status_msg: str = ""
    loading: bool = False
    fetched_at: str = ""
    draft_body: str = ""
    draft_tmppath: str = ""
    page: int = 1
    has_next_page: bool = False
    viewing_diff: bool = False
    diff_lines: list = field(default_factory=list)


# --- Helpers ---

def relative_time(iso):
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        secs = int((datetime.now(timezone.utc) - dt).total_seconds())
        if secs < 0: return "now"
        if secs < 60: return f"{secs}s"
        if secs < 3600: return f"{secs // 60}m"
        if secs < 86400: return f"{secs // 3600}h"
        if secs < 2592000: return f"{secs // 86400}d"
        if secs < 31536000: return f"{secs // 2592000}mo"
        return f"{secs // 31536000}y"
    except Exception:
        return ""


def format_fetch_time(iso):
    if not iso:
        return "never"
    try:
        return datetime.fromisoformat(iso).astimezone().strftime("%Y-%m-%d %H:%M")
    except Exception:
        return "?"


def comment_count(item):
    c = item.get("comments", [])
    return len(c) if isinstance(c, list) else (int(c) if c else 0)


def sort_items(items, sort_key, reverse):
    if sort_key == "comments":
        return sorted(items, key=lambda x: comment_count(x), reverse=reverse)
    return sorted(items, key=lambda x: x.get(sort_key, ""), reverse=reverse)


def author_login(item):
    a = item.get("author")
    return a.get("login", "") if isinstance(a, dict) else (str(a) if a else "")


def label_names(item):
    labels = item.get("labels", [])
    if not labels:
        return ""
    return ",".join(lb.get("name", "") if isinstance(lb, dict) else str(lb) for lb in labels)


# --- Colors ---
# Color pair IDs
C_TITLE = 1
C_SELECTED = 2
C_HELP = 3
C_NUM_GREEN = 4
C_NUM_MAUVE = 5
C_LABEL = 6
C_COMMENT_HDR = 7
C_OPEN = 8
C_CLOSED = 9
C_DRAFT = 10
C_AUTHOR = 11
C_DATE = 12
C_CODE = 13
C_KW = 14
C_STR = 15
C_CMT = 16
C_NUMLIT = 17
C_DIFF_ADD = 18
C_DIFF_DEL = 19
C_DIFF_HUNK = 20
C_MERGED = 21

# Custom color IDs (16+)
_custom_colors_ok = False


def _c(hex_str):
    h = hex_str.lstrip("#")
    return (int(h[0:2], 16) * 1000 // 255, int(h[2:4], 16) * 1000 // 255, int(h[4:6], 16) * 1000 // 255)


def init_colors():
    global _custom_colors_ok
    curses.start_color()
    curses.use_default_colors()

    if curses.can_change_color() and curses.COLORS >= 32:
        _custom_colors_ok = True
        # Catppuccin Mocha inspired
        curses.init_color(16, *_c("#303446"))  # surface0
        curses.init_color(17, *_c("#414559"))  # surface1
        curses.init_color(18, *_c("#c6d0f5"))  # text
        curses.init_color(19, *_c("#a5adce"))  # subtext
        curses.init_color(20, *_c("#a6d189"))  # green
        curses.init_color(21, *_c("#e78284"))  # red
        curses.init_color(22, *_c("#ca9ee6"))  # mauve
        curses.init_color(23, *_c("#e5c890"))  # yellow
        curses.init_color(24, *_c("#81c8be"))  # teal
        curses.init_color(25, *_c("#8caaee"))  # blue
        curses.init_color(26, *_c("#ef9f76"))  # peach
        curses.init_color(27, *_c("#737994"))  # overlay0
        curses.init_color(28, *_c("#232634"))  # crust
        curses.init_color(29, *_c("#51576d"))  # surface2

        curses.init_pair(C_TITLE, 18, 16)
        curses.init_pair(C_SELECTED, 18, 17)
        curses.init_pair(C_HELP, 19, 16)
        curses.init_pair(C_NUM_GREEN, 20, -1)
        curses.init_pair(C_NUM_MAUVE, 22, -1)
        curses.init_pair(C_LABEL, 23, -1)
        curses.init_pair(C_COMMENT_HDR, 24, -1)
        curses.init_pair(C_OPEN, 20, -1)
        curses.init_pair(C_CLOSED, 21, -1)
        curses.init_pair(C_DRAFT, 28, 23)
        curses.init_pair(C_AUTHOR, 25, -1)
        curses.init_pair(C_DATE, 27, -1)
        curses.init_pair(C_CODE, 19, 16)
        curses.init_pair(C_KW, 22, -1)
        curses.init_pair(C_STR, 20, -1)
        curses.init_pair(C_CMT, 27, -1)
        curses.init_pair(C_NUMLIT, 26, -1)
        curses.init_pair(C_DIFF_ADD, 20, -1)
        curses.init_pair(C_DIFF_DEL, 21, -1)
        curses.init_pair(C_DIFF_HUNK, 25, -1)
        curses.init_pair(C_MERGED, 22, -1)
    else:
        _custom_colors_ok = False
        curses.init_pair(C_TITLE, curses.COLOR_WHITE, curses.COLOR_BLUE)
        curses.init_pair(C_SELECTED, curses.COLOR_BLACK, curses.COLOR_WHITE)
        curses.init_pair(C_HELP, curses.COLOR_WHITE, curses.COLOR_BLUE)
        curses.init_pair(C_NUM_GREEN, curses.COLOR_GREEN, -1)
        curses.init_pair(C_NUM_MAUVE, curses.COLOR_MAGENTA, -1)
        curses.init_pair(C_LABEL, curses.COLOR_YELLOW, -1)
        curses.init_pair(C_COMMENT_HDR, curses.COLOR_CYAN, -1)
        curses.init_pair(C_OPEN, curses.COLOR_GREEN, -1)
        curses.init_pair(C_CLOSED, curses.COLOR_RED, -1)
        curses.init_pair(C_DRAFT, curses.COLOR_BLACK, curses.COLOR_YELLOW)
        curses.init_pair(C_AUTHOR, curses.COLOR_BLUE, -1)
        curses.init_pair(C_DATE, curses.COLOR_WHITE, -1)
        curses.init_pair(C_CODE, curses.COLOR_WHITE, -1)
        curses.init_pair(C_KW, curses.COLOR_MAGENTA, -1)
        curses.init_pair(C_STR, curses.COLOR_GREEN, -1)
        curses.init_pair(C_CMT, curses.COLOR_WHITE, -1)
        curses.init_pair(C_NUMLIT, curses.COLOR_YELLOW, -1)
        curses.init_pair(C_DIFF_ADD, curses.COLOR_GREEN, -1)
        curses.init_pair(C_DIFF_DEL, curses.COLOR_RED, -1)
        curses.init_pair(C_DIFF_HUNK, curses.COLOR_CYAN, -1)
        curses.init_pair(C_MERGED, curses.COLOR_MAGENTA, -1)


def state_indicator(item):
    s = item.get("state", "").upper()
    if s == "OPEN":
        return "●", C_OPEN
    elif s == "MERGED":
        return "◆", C_MERGED
    return "○", C_CLOSED


# --- Styled lines ---
# A StyledLine is list[tuple[str, int]] where int is curses attr.

def safe_addstr(win, y, x, text, attr=0):
    h, w = win.getmaxyx()
    if y < 0 or y >= h or x >= w:
        return
    try:
        win.addnstr(y, x, text, w - x - 1, attr)
    except curses.error:
        pass


def draw_styled_line(win, row, col, segments, max_w):
    x = col
    for text, attr in segments:
        if x >= max_w - 1:
            break
        safe_addstr(win, row, x, text[:max_w - x - 1], attr)
        x += len(text)


# --- Markdown ---

_RE_BOLD = re.compile(r'\*\*(.+?)\*\*')
_RE_CODE = re.compile(r'`([^`]+)`')


def _parse_inline_md(text):
    """Parse **bold** and `code` into styled segments."""
    segments = []
    # Merge both patterns, process left-to-right
    tokens = []
    for m in _RE_BOLD.finditer(text):
        tokens.append((m.start(), m.end(), m.group(1), curses.A_BOLD))
    for m in _RE_CODE.finditer(text):
        tokens.append((m.start(), m.end(), m.group(1), curses.color_pair(C_CODE)))
    tokens.sort(key=lambda t: t[0])

    # Remove overlapping
    filtered = []
    last_end = 0
    for start, end, content, attr in tokens:
        if start >= last_end:
            filtered.append((start, end, content, attr))
            last_end = end
    tokens = filtered

    pos = 0
    for start, end, content, attr in tokens:
        if start > pos:
            segments.append((text[pos:start], 0))
        segments.append((content, attr))
        pos = end
    if pos < len(text):
        segments.append((text[pos:], 0))
    return segments or [("", 0)]


def _highlight_code(code, lang, width):
    """Tokenize code with pygments, return list of StyledLine."""
    TOKEN_MAP = {
        Token.Keyword: C_KW, Token.Keyword.Constant: C_KW, Token.Keyword.Declaration: C_KW,
        Token.Keyword.Namespace: C_KW, Token.Keyword.Type: C_KW,
        Token.Name.Builtin: C_KW, Token.Name.Function: C_AUTHOR, Token.Name.Class: C_AUTHOR,
        Token.Name.Decorator: C_KW,
        Token.Literal.String: C_STR, Token.Literal.String.Doc: C_STR,
        Token.Literal.String.Single: C_STR, Token.Literal.String.Double: C_STR,
        Token.Literal.String.Backtick: C_STR, Token.Literal.String.Affix: C_STR,
        Token.Literal.Number: C_NUMLIT, Token.Literal.Number.Integer: C_NUMLIT,
        Token.Literal.Number.Float: C_NUMLIT,
        Token.Comment: C_CMT, Token.Comment.Single: C_CMT, Token.Comment.Multiline: C_CMT,
        Token.Comment.Hashbang: C_CMT,
    }

    if not HAS_PYGMENTS:
        return [[(line, curses.color_pair(C_CODE))] for line in code.split("\n")]

    try:
        lexer = get_lexer_by_name(lang)
    except Exception:
        lexer = TextLexer()

    lines = [[]]
    for ttype, value in lexer.get_tokens(code):
        attr = 0
        t = ttype
        while t:
            if t in TOKEN_MAP:
                attr = curses.color_pair(TOKEN_MAP[t])
                break
            t = t.parent
        for i, part in enumerate(value.split("\n")):
            if i > 0:
                lines.append([])
            if part:
                lines[-1].append((part, attr))

    # Remove trailing empty line from pygments
    if lines and not lines[-1]:
        lines.pop()
    return lines


def build_detail_lines(item, width, kind):
    """Build styled lines for issue/PR detail view."""
    lines = []
    w = max(width - 4, 20)

    # Title
    title = f"#{item.get('number', '')}  {item.get('title', '')}"
    for wrapped in (textwrap.wrap(title, w) or [title]):
        lines.append([(wrapped, curses.A_BOLD)])

    lines.append([("", 0)])

    # Meta line
    st = item.get("state", "").upper()
    author = author_login(item)
    created = relative_time(item.get("createdAt", ""))
    lbls = label_names(item)

    sym, sym_c = state_indicator(item)
    meta_segs = [
        (f"{sym} {st}", curses.color_pair(sym_c) | curses.A_BOLD),
        ("  ", 0),
        (f"@{author}", curses.color_pair(C_AUTHOR)),
        ("  ", 0),
        (f"{created} ago", curses.color_pair(C_DATE)),
    ]
    if lbls:
        meta_segs += [("  ", 0), (lbls, curses.color_pair(C_LABEL))]
    if kind == "pr":
        adds = item.get("additions", 0)
        dels = item.get("deletions", 0)
        files = item.get("changedFiles", 0)
        meta_segs += [("  ", 0), (f"+{adds}", curses.color_pair(C_DIFF_ADD)),
                      (f"/-{dels}", curses.color_pair(C_DIFF_DEL)),
                      (f" ({files} files)", curses.color_pair(C_DATE))]
        if item.get("isDraft"):
            meta_segs += [(" ", 0), ("DRAFT", curses.color_pair(C_LABEL) | curses.A_BOLD)]
        rd = item.get("reviewDecision", "")
        if rd:
            meta_segs += [(" ", 0), (rd, curses.color_pair(C_DATE))]
    lines.append(meta_segs)

    lines.append([("", 0)])
    lines.append([("─" * min(w, 60), curses.A_DIM)])
    lines.append([("", 0)])

    # Body
    body = item.get("body", "") or "(no description)"
    lines.extend(_parse_md_block(body, w))
    lines.append([("", 0)])

    # Comments
    comments = item.get("comments", [])
    if isinstance(comments, list) and comments:
        lines.append([("─" * min(w, 60), curses.A_DIM)])
        for cm in comments:
            lines.append([("", 0)])
            cm_author = cm.get("author", {}).get("login", "?") if isinstance(cm.get("author"), dict) else "?"
            cm_time = relative_time(cm.get("createdAt", ""))
            lines.append([
                ("── ", curses.A_DIM),
                (cm_author, curses.color_pair(C_AUTHOR)),
                (f" ({cm_time} ago)", curses.color_pair(C_DATE)),
                (" ──", curses.A_DIM),
            ])
            lines.append([("", 0)])
            cm_body = cm.get("body", "")
            lines.extend(_parse_md_block(cm_body, w))

    return lines


def _parse_md_block(text, width):
    """Parse a markdown text block into styled lines."""
    lines_out = []
    raw_lines = text.split("\n")
    i = 0
    while i < len(raw_lines):
        line = raw_lines[i]

        # Code fence
        if line.strip().startswith("```"):
            lang = line.strip()[3:].strip()
            code_lines = []
            i += 1
            while i < len(raw_lines) and not raw_lines[i].strip().startswith("```"):
                code_lines.append(raw_lines[i])
                i += 1
            i += 1  # skip closing ```
            code = "\n".join(code_lines)
            highlighted = _highlight_code(code, lang or "text", width)
            for hl in highlighted:
                lines_out.append([("│ ", curses.color_pair(C_DATE))] + hl)
            continue

        # Header
        hm = re.match(r'^(#{1,3})\s+(.+)$', line)
        if hm:
            for wrapped in (textwrap.wrap(hm.group(2), width) or [hm.group(2)]):
                lines_out.append([(wrapped, curses.A_BOLD | curses.A_UNDERLINE)])
            i += 1
            continue

        # Blank line
        if not line.strip():
            lines_out.append([("", 0)])
            i += 1
            continue

        # Regular paragraph - wrap then parse inline
        for wrapped in (textwrap.wrap(line, width) or [line]):
            lines_out.append(_parse_inline_md(wrapped))
        i += 1

    return lines_out


def build_diff_lines(diff_text, width):
    """Parse unified diff into styled lines."""
    lines = []
    for raw in diff_text.split("\n"):
        if raw.startswith("+++") or raw.startswith("---"):
            lines.append([(raw, curses.A_BOLD)])
        elif raw.startswith("@@"):
            lines.append([(raw, curses.color_pair(C_DIFF_HUNK))])
        elif raw.startswith("+"):
            lines.append([(raw, curses.color_pair(C_DIFF_ADD))])
        elif raw.startswith("-"):
            lines.append([(raw, curses.color_pair(C_DIFF_DEL))])
        elif raw.startswith("diff "):
            lines.append([(raw, curses.A_BOLD)])
        else:
            lines.append([(raw, 0)])
    return lines


# --- Drawing ---

def draw_title_bar(stdscr, state, repo):
    _, w = stdscr.getmaxyx()
    tab = "Issues" if state.kind == "issue" else "PRs"
    count = len(state.items)
    fetched = format_fetch_time(state.fetched_at)
    page_str = f" p{state.page}" if state.page > 1 else ""
    title = f" yolo - {repo}"
    right = f" {tab} | {state.state_filter} | {count}{page_str} | fetched {fetched} "
    pad = max(0, w - len(title) - len(right))
    bar = title + " " * pad + right
    safe_addstr(stdscr, 0, 0, bar.ljust(w)[:w], curses.color_pair(C_TITLE) | curses.A_BOLD)


def draw_filter_bar(stdscr, state):
    _, w = stdscr.getmaxyx()
    sort_name = SORT_KEYS[state.sort_idx][1]
    sort_dir = "desc" if state.sort_reverse else "asc"
    left = f" Search: {state.search_query}" if state.search_query else ""
    right = f"Sort: {sort_name} {sort_dir} "
    pad = max(0, w - len(left) - len(right))
    safe_addstr(stdscr, 1, 0, (left + " " * pad + right).ljust(w)[:w], curses.A_DIM)


def draw_list_view(stdscr, state, repo):
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

    list_h = max(1, h - 5)

    if state.cursor < state.scroll_offset:
        state.scroll_offset = state.cursor
    if state.cursor >= state.scroll_offset + list_h:
        state.scroll_offset = state.cursor - list_h + 1

    ind_col, num_col = 2, 7
    author_col = min(16, max(8, w // 8))
    time_col, cmts_col = 6, 6
    label_col = min(18, max(8, w // 7))
    title_col = w - ind_col - num_col - author_col - time_col - cmts_col - label_col - 4

    for i in range(list_h):
        idx = state.scroll_offset + i
        if idx >= len(state.items):
            break
        item = state.items[idx]
        row = i + 2
        is_sel = idx == state.cursor
        attr = curses.color_pair(C_SELECTED) if is_sel else 0

        if is_sel:
            safe_addstr(stdscr, row, 0, " " * (w - 1), attr)

        x = 1
        # State indicator
        sym, sym_c = state_indicator(item)
        safe_addstr(stdscr, row, x, sym, (attr if is_sel else curses.color_pair(sym_c)) | curses.A_BOLD)
        x += ind_col

        # Number
        num = f"#{item.get('number', '')}"
        nc = C_NUM_MAUVE if state.kind == "pr" else C_NUM_GREEN
        safe_addstr(stdscr, row, x, num.rjust(num_col - 1), (attr if is_sel else curses.color_pair(nc)) | curses.A_BOLD)
        x += num_col

        # Title
        safe_addstr(stdscr, row, x, item.get("title", "")[:title_col], attr)
        x += title_col + 1

        # Author
        safe_addstr(stdscr, row, x, author_login(item)[:author_col], attr if is_sel else curses.color_pair(C_AUTHOR))
        x += author_col + 1

        # Labels
        safe_addstr(stdscr, row, x, label_names(item)[:label_col], attr if is_sel else curses.color_pair(C_LABEL))

        # Time
        t = relative_time(item.get("updatedAt", ""))
        safe_addstr(stdscr, row, w - cmts_col - time_col - 2, t.rjust(time_col), (attr | curses.A_DIM) if is_sel else curses.color_pair(C_DATE))

        # Comments
        cc = comment_count(item)
        safe_addstr(stdscr, row, w - cmts_col - 1, (f"{cc}c" if cc else "").rjust(cmts_col - 1), attr | curses.A_DIM)

    # Status / overlays (h-3), blank spacer (h-2), help bar (h-1)
    if state.mode == "search":
        safe_addstr(stdscr, h - 2, 0, f" /{state.search_buf}█".ljust(w)[:w], curses.A_BOLD)
    elif state.mode == "goto":
        safe_addstr(stdscr, h - 2, 0, f" #{state.goto_buf}█".ljust(w)[:w], curses.A_BOLD)
    elif state.status_msg:
        safe_addstr(stdscr, h - 2, 0, f" {state.status_msg}"[:w], curses.A_DIM)

    # Help
    helps = {
        "search": " Enter:search  Esc:cancel",
        "goto": " Enter:go  Esc:cancel",
    }
    help_text = helps.get(state.mode,
        " j/k:move  J/K:5x  o/Enter:view  B:browser  #:goto  n/N:page  i/p:issues/PRs  /:search  f:filter  s:sort  r:refresh  q:quit")
    safe_addstr(stdscr, h - 1, 0, help_text.ljust(w)[:w], curses.color_pair(C_HELP))
    stdscr.refresh()


def draw_detail_view(stdscr, state, repo):
    h, w = stdscr.getmaxyx()
    stdscr.erase()

    active_lines = state.diff_lines if state.viewing_diff else state.detail_lines
    if not active_lines:
        safe_addstr(stdscr, 0, 0, "No content", curses.A_DIM)
        stdscr.refresh()
        return

    # Title bar
    item = state.detail_item
    num = item.get("number", "")
    title = item.get("title", "")
    st = item.get("state", "").upper()
    bar = f" #{num}  {title}"
    if state.viewing_diff:
        bar += "  [DIFF]"
    safe_addstr(stdscr, 0, 0, bar.ljust(w)[:w], curses.color_pair(C_TITLE) | curses.A_BOLD)

    sym, sym_c = state_indicator(item)
    st_label = f" {sym} {st} "
    safe_addstr(stdscr, 0, max(0, w - len(st_label) - 1), st_label,
                curses.color_pair(sym_c) | curses.A_BOLD)

    # Draft banner
    has_draft = bool(state.draft_body) and not state.viewing_diff
    banner_h = 1 if has_draft else 0
    if has_draft:
        preview = state.draft_body.replace("\n", " ")[:w - 30]
        safe_addstr(stdscr, 1, 0, f" DRAFT: {preview}   y:post  e:edit  n:discard ".ljust(w)[:w],
                    curses.color_pair(C_DRAFT) | curses.A_BOLD)

    # Content (leave 2 rows at bottom: status + help)
    content_start = 1 + banner_h
    content_h = max(1, h - content_start - 2)
    max_scroll = max(0, len(active_lines) - content_h)
    state.detail_scroll = max(0, min(state.detail_scroll, max_scroll))

    for i in range(content_h):
        li = state.detail_scroll + i
        if li >= len(active_lines):
            break
        draw_styled_line(stdscr, content_start + i, 2, active_lines[li], w)

    # Scroll %
    if len(active_lines) > content_h and max_scroll > 0:
        pct = int(state.detail_scroll / max_scroll * 100)
        safe_addstr(stdscr, 0, w - len(st_label) - 7, f" {pct}% ", curses.color_pair(C_TITLE))

    # Help
    if has_draft:
        ht = " j/k:scroll  y:post  e:edit  n:discard  B:browser  q:back"
    elif state.viewing_diff:
        ht = " j/k:scroll  v:back to detail  B:browser  q:back"
    elif state.kind == "pr":
        ht = " j/k:scroll  c:comment  v:diff  m:merge  O:reopen  C:close  B:browser  r:refresh  q:back"
    else:
        ht = " j/k:scroll  c:comment  O:reopen  C:close  B:browser  r:refresh  q:back"
    safe_addstr(stdscr, h - 1, 0, ht.ljust(w)[:w], curses.color_pair(C_HELP))

    if state.status_msg and not has_draft:
        safe_addstr(stdscr, h - 2, 0, f" {state.status_msg}"[:w], curses.A_DIM)

    stdscr.refresh()


# --- Data operations ---

def fetch_list(gh, state, force=False):
    PER_PAGE = 100
    cp = cache_path_list(gh.repo, state.kind, state.state_filter, state.search_query, state.page)
    if not force:
        data, fetched_at = load_cache(cp)
        if data is not None:
            items = data.get("items", data) if isinstance(data, dict) else data
            total = data.get("total", 0) if isinstance(data, dict) else len(items)
            state.items = sort_items(items, SORT_KEYS[state.sort_idx][0], state.sort_reverse)
            state.has_next_page = state.page * PER_PAGE < total
            state.fetched_at = fetched_at
            state.cursor = state.scroll_offset = 0
            state.status_msg = ""
            return
    # Remember old updatedAt per item to detect stale detail caches
    old_updated = {it.get("number"): it.get("updatedAt") for it in state.items}

    state.loading = True
    try:
        items, total = gh.list_issues(state=state.state_filter, search=state.search_query, page=state.page, per_page=PER_PAGE) \
            if state.kind == "issue" \
            else gh.list_prs(state=state.state_filter, search=state.search_query, page=state.page, per_page=PER_PAGE)
        state.has_next_page = state.page * PER_PAGE < total
        save_cache(cp, {"items": items, "total": total})

        # Purge detail/diff caches for items whose updatedAt changed
        for it in items:
            num = it.get("number")
            if num in old_updated and old_updated[num] != it.get("updatedAt"):
                for p in (cache_path_detail(gh.repo, state.kind, num), cache_path_diff(gh.repo, num)):
                    if p.exists():
                        p.unlink()

        state.items = sort_items(items, SORT_KEYS[state.sort_idx][0], state.sort_reverse)
        state.fetched_at = datetime.now(timezone.utc).isoformat()
        state.cursor = state.scroll_offset = 0
        state.status_msg = "Refreshed"
    except RuntimeError as e:
        state.items = []
        state.status_msg = f"Error: {e}"[:80]
    state.loading = False


def fetch_detail(gh, state, number, force=False):
    cp = cache_path_detail(gh.repo, state.kind, number)
    if not force:
        data, _ = load_cache(cp)
        if data is not None:
            state.detail_item = data
            state.detail_lines = []
            state.detail_scroll = 0
            state.mode = "detail"
            state.viewing_diff = False
            state.diff_lines = []
            return
    try:
        full = gh.view_issue(number) if state.kind == "issue" else gh.view_pr(number)
        save_cache(cp, full)
        state.detail_item = full
        state.detail_lines = []
        state.detail_scroll = 0
        state.mode = "detail"
        state.viewing_diff = False
        state.diff_lines = []
    except RuntimeError as e:
        state.status_msg = f"Error: {e}"[:80]


def fetch_diff(gh, state, number, width, force=False):
    cp = cache_path_diff(gh.repo, number)
    if not force:
        data, _ = load_cache(cp)
        if data is not None:
            state.diff_lines = build_diff_lines(data, width)
            state.viewing_diff = True
            state.detail_scroll = 0
            return
    try:
        diff_text = gh.pr_diff(number)
        save_cache(cp, diff_text)
        state.diff_lines = build_diff_lines(diff_text, width)
        state.viewing_diff = True
        state.detail_scroll = 0
    except RuntimeError as e:
        state.status_msg = f"Error: {e}"[:80]


def open_detail(gh, state):
    if state.items:
        fetch_detail(gh, state, state.items[state.cursor].get("number"))


def goto_number(gh, state, number):
    for i, item in enumerate(state.items):
        if item.get("number") == number:
            state.cursor = i
            state.status_msg = ""
            return
    fetch_detail(gh, state, number)
    if state.mode != "detail":
        orig = state.kind
        state.kind = "pr" if orig == "issue" else "issue"
        fetch_detail(gh, state, number)
        if state.mode != "detail":
            state.kind = orig
            state.status_msg = f"#{number} not found"


# --- Comment workflow ---

def start_comment(stdscr, state):
    if not state.detail_item:
        return stdscr
    num = state.detail_item.get("number")
    kind = state.kind
    editor = os.environ.get("EDITOR", os.environ.get("VISUAL", "vi"))

    if state.draft_tmppath and os.path.exists(state.draft_tmppath):
        tmppath = state.draft_tmppath
    else:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False, prefix="yolo_comment_") as f:
            f.write(f"\n# Comment for {kind} #{num}\n# Lines starting with # are ignored.\n# Save and close to preview draft.\n")
            tmppath = f.name
        state.draft_tmppath = tmppath

    curses.endwin()
    subprocess.run([editor, tmppath])

    with open(tmppath) as f:
        body = "".join(ln for ln in f if not ln.startswith("#")).strip()

    stdscr = _reinit_curses(stdscr)
    if body:
        state.draft_body = body
        state.status_msg = ""
    else:
        _cleanup_draft(state)
        state.status_msg = "Comment cancelled (empty)"
    return stdscr


def post_draft(gh, state):
    if not state.draft_body or not state.detail_item:
        return
    num = state.detail_item.get("number")
    kind = state.kind
    with open(state.draft_tmppath, "w") as f:
        f.write(state.draft_body)
    try:
        gh.comment(kind, num, state.draft_tmppath)
        state.status_msg = "Comment posted!"
        _cleanup_draft(state)
        cp = cache_path_detail(gh.repo, kind, num)
        try:
            full = gh.view_issue(num) if kind == "issue" else gh.view_pr(num)
            save_cache(cp, full)
            state.detail_item = full
            state.detail_lines = []
        except RuntimeError:
            pass
    except RuntimeError as e:
        state.status_msg = f"Post failed: {e}"[:80]


def edit_draft(stdscr, state):
    if not state.draft_body or not state.draft_tmppath:
        return stdscr
    num = state.detail_item.get("number", "?")
    kind = state.kind
    with open(state.draft_tmppath, "w") as f:
        f.write(state.draft_body)
        f.write(f"\n# Comment for {kind} #{num}\n# Lines starting with # are ignored.\n")
    editor = os.environ.get("EDITOR", os.environ.get("VISUAL", "vi"))
    curses.endwin()
    subprocess.run([editor, state.draft_tmppath])
    with open(state.draft_tmppath) as f:
        body = "".join(ln for ln in f if not ln.startswith("#")).strip()
    stdscr = _reinit_curses(stdscr)
    if body:
        state.draft_body = body
    else:
        _cleanup_draft(state)
        state.status_msg = "Draft discarded (empty)"
    return stdscr


def discard_draft(state):
    _cleanup_draft(state)
    state.status_msg = "Draft discarded"


def _cleanup_draft(state):
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

def _detail_scroll_keys(key, state, h, banner_h=0):
    """Handle j/k/J/K/g/G/space/pgup/pgdn scrolling. Returns True if handled."""
    content_h = max(1, h - 2 - banner_h - 1)
    if key in (ord("j"), curses.KEY_DOWN):
        state.detail_scroll += 1
    elif key in (ord("k"), curses.KEY_UP):
        state.detail_scroll -= 1
    elif key == ord("J"):
        state.detail_scroll += 5
    elif key == ord("K"):
        state.detail_scroll -= 5
    elif key in (curses.KEY_NPAGE,):
        state.detail_scroll += content_h
    elif key in (curses.KEY_PPAGE,):
        state.detail_scroll -= content_h
    elif key in (ord("g"), curses.KEY_HOME):
        state.detail_scroll = 0
    elif key in (ord("G"), curses.KEY_END):
        active = state.diff_lines if state.viewing_diff else state.detail_lines
        state.detail_scroll = max(0, len(active) - content_h)
    elif key == ord(" "):
        state.detail_scroll += content_h
    else:
        return False
    return True


def main_loop(stdscr, repo):
    curses.curs_set(0)
    init_colors()
    stdscr.timeout(-1)

    gh = GH(repo)
    state = AppState()

    fetch_list(gh, state)
    if not state.items and not state.fetched_at:
        fetch_list(gh, state, force=True)

    while True:
        h, w = stdscr.getmaxyx()

        if state.mode in ("list", "search", "goto"):
            state.items = sort_items(state.items, SORT_KEYS[state.sort_idx][0], state.sort_reverse)
            draw_list_view(stdscr, state, repo)
        elif state.mode == "detail":
            if not state.detail_lines and state.detail_item:
                state.detail_lines = build_detail_lines(state.detail_item, w, state.kind)
            draw_detail_view(stdscr, state, repo)

        key = stdscr.getch()

        if key == curses.KEY_RESIZE:
            if state.mode == "detail" and state.detail_item:
                state.detail_lines = []
                if state.viewing_diff:
                    state.diff_lines = []  # will need refetch
            continue

        # --- Search ---
        if state.mode == "search":
            if key == 27:
                state.mode = "list"
                state.search_buf = ""
            elif key in (curses.KEY_ENTER, 10, 13):
                state.search_query = state.search_buf
                state.search_buf = ""
                state.mode = "list"
                state.page = 1
                fetch_list(gh, state)
                if not state.items and not state.fetched_at:
                    fetch_list(gh, state, force=True)
            elif key in (curses.KEY_BACKSPACE, 127, 8):
                state.search_buf = state.search_buf[:-1]
            elif 32 <= key <= 126:
                state.search_buf += chr(key)
            continue

        # --- Goto ---
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

        # --- List ---
        if state.mode == "list":
            if key in (ord("q"), ord("Q")):
                break
            elif key in (ord("j"), curses.KEY_DOWN):
                if state.cursor < len(state.items) - 1:
                    state.cursor += 1
            elif key in (ord("k"), curses.KEY_UP):
                if state.cursor > 0:
                    state.cursor -= 1
            elif key == ord("J"):
                state.cursor = min(len(state.items) - 1, state.cursor + 5)
            elif key == ord("K"):
                state.cursor = max(0, state.cursor - 5)
            elif key == curses.KEY_NPAGE:
                list_h = max(1, h - 5)
                state.cursor = min(len(state.items) - 1, state.cursor + list_h)
            elif key == curses.KEY_PPAGE:
                list_h = max(1, h - 5)
                state.cursor = max(0, state.cursor - list_h)
            elif key in (ord("g"), curses.KEY_HOME):
                state.cursor = 0
            elif key in (ord("G"), curses.KEY_END):
                state.cursor = max(0, len(state.items) - 1)
            elif key in (ord("o"), curses.KEY_ENTER, 10, 13):
                open_detail(gh, state)
            elif key == ord("B"):
                if state.items:
                    gh.open_in_browser(state.kind, state.items[state.cursor].get("number"))
                    state.status_msg = "Opened in browser"
            elif key == ord("i"):
                if state.kind != "issue":
                    state.kind = "issue"
                    state.state_filter = "open"
                    state.page = 1
                    fetch_list(gh, state)
            elif key == ord("p"):
                if state.kind != "pr":
                    state.kind = "pr"
                    state.state_filter = "open"
                    state.page = 1
                    fetch_list(gh, state)
            elif key == ord("/"):
                state.mode = "search"
                state.search_buf = state.search_query
            elif key == ord("#"):
                state.mode = "goto"
                state.goto_buf = ""
            elif key == ord("f"):
                cycle = ["open", "closed", "merged", "all"] if state.kind == "pr" else ["open", "closed", "all"]
                idx = cycle.index(state.state_filter) if state.state_filter in cycle else 0
                state.state_filter = cycle[(idx + 1) % len(cycle)]
                state.page = 1
                fetch_list(gh, state)
            elif key == ord("s"):
                state.sort_idx = (state.sort_idx + 1) % len(SORT_KEYS)
            elif key == ord("S"):
                state.sort_reverse = not state.sort_reverse
            elif key == ord("r"):
                fetch_list(gh, state, force=True)
            elif key == ord("n"):
                if state.has_next_page:
                    state.page += 1
                    fetch_list(gh, state)
                    if not state.items:
                        state.page -= 1
                        fetch_list(gh, state)
                        state.status_msg = "No more pages"
            elif key == ord("N"):
                if state.page > 1:
                    state.page -= 1
                    fetch_list(gh, state)

        # --- Detail ---
        elif state.mode == "detail":
            has_draft = bool(state.draft_body) and not state.viewing_diff
            banner_h = 1 if has_draft else 0

            if has_draft:
                if key in (ord("y"), ord("Y")):
                    post_draft(gh, state)
                elif key in (ord("e"), ord("E")):
                    stdscr = edit_draft(stdscr, state)
                elif key in (ord("n"), ord("N")):
                    discard_draft(state)
                elif key in (ord("q"), ord("Q")):
                    state.mode = "list"
                    state.detail_lines = []
                    state.viewing_diff = False
                elif key == ord("B"):
                    if state.detail_item:
                        gh.open_in_browser(state.kind, state.detail_item.get("number"))
                else:
                    _detail_scroll_keys(key, state, h, banner_h)
            elif state.viewing_diff:
                if key in (ord("q"), ord("Q"), ord("v")):
                    state.viewing_diff = False
                    state.detail_scroll = 0
                elif key == ord("B"):
                    if state.detail_item:
                        gh.open_in_browser(state.kind, state.detail_item.get("number"))
                else:
                    _detail_scroll_keys(key, state, h)
            else:
                if key in (ord("q"), ord("Q")):
                    state.mode = "list"
                    state.detail_lines = []
                elif key == ord("c"):
                    stdscr = start_comment(stdscr, state)
                elif key == ord("v") and state.kind == "pr" and state.detail_item:
                    fetch_diff(gh, state, state.detail_item.get("number"), w)
                elif key == ord("m") and state.kind == "pr" and state.detail_item:
                    state.status_msg = "Merge method: (M)erge  (R)ebase  (S)quash  (Esc)cancel"
                    draw_detail_view(stdscr, state, repo)
                    mk = stdscr.getch()
                    methods = {ord("m"): "merge", ord("M"): "merge",
                               ord("r"): "rebase", ord("R"): "rebase",
                               ord("s"): "squash", ord("S"): "squash"}
                    if mk in methods:
                        num = state.detail_item.get("number")
                        try:
                            gh.merge_pr(num, methods[mk])
                            state.detail_item["state"] = "MERGED"
                            state.detail_lines = []
                            state.status_msg = f"Merged #{num} ({methods[mk]})"
                            save_cache(cache_path_detail(gh.repo, state.kind, num), state.detail_item)
                        except RuntimeError as e:
                            state.status_msg = f"Merge failed: {e}"[:80]
                    else:
                        state.status_msg = ""
                elif key == ord("O"):
                    if state.detail_item:
                        num = state.detail_item.get("number")
                        try:
                            gh.reopen(state.kind, num)
                            state.detail_item["state"] = "OPEN"
                            state.detail_lines = []
                            state.status_msg = f"Reopened #{num}"
                            save_cache(cache_path_detail(gh.repo, state.kind, num), state.detail_item)
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
                            save_cache(cache_path_detail(gh.repo, state.kind, num), state.detail_item)
                        except RuntimeError as e:
                            state.status_msg = f"Close failed: {e}"[:80]
                elif key == ord("B"):
                    if state.detail_item:
                        gh.open_in_browser(state.kind, state.detail_item.get("number"))
                        state.status_msg = "Opened in browser"
                elif key == ord("r"):
                    if state.detail_item:
                        fetch_detail(gh, state, state.detail_item.get("number"), force=True)
                        state.status_msg = "Refreshed"
                else:
                    _detail_scroll_keys(key, state, h)


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
