# ghui

A terminal interface for browsing GitHub issues and pull requests.

Single Python file, no mandatory dependencies, uses the `gh` CLI for authentication and API calls.

## Features

- List issues and PRs with state indicators, author, labels, and timestamps
- Filter by state (open/closed/merged/all) and search with GitHub query syntax
- Sort by updated, created, or comment count
- View detail with body, comments, and markdown rendering
- Syntax-highlighted code blocks (requires pygments)
- Write comments via `$EDITOR` with draft review before posting
- View PR diffs, merge PRs (merge/rebase/squash)
- Open/close/reopen issues and PRs
- Disk cache — never hits the network unless you press `r`
- Paginated browsing with `n`/`N`

## Requirements

- Python 3
- [gh CLI](https://cli.github.com/) installed and authenticated

Optional: `pip install pygments` for syntax highlighting in code blocks.

## Usage

```
python3 ghui.py                     # auto-detect repo from git remote
python3 ghui.py cli/cli             # explicit repo
python3 ghui.py facebook/react      # any public repo
```

## Keybindings

### List view

| Key | Action |
|-----|--------|
| `j`/`k` | Move cursor |
| `J`/`K` | Scroll 5 lines |
| `PgUp`/`PgDn` | Page scroll |
| `g`/`G` | Top / bottom |
| `o` / `Enter` | Open detail |
| `B` | Open in browser |
| `#` | Jump to issue/PR number |
| `i`/`p` | Switch to issues/PRs |
| `/` | Search |
| `f` | Cycle state filter |
| `s` | Cycle sort |
| `S` | Reverse sort |
| `r` | Refresh |
| `n`/`N` | Next/previous page |
| `q` | Quit |

### Detail view

| Key | Action |
|-----|--------|
| `j`/`k` | Scroll |
| `J`/`K` | Scroll 5 lines |
| `PgUp`/`PgDn` | Page scroll |
| `c` | Write comment |
| `v` | View diff (PRs) |
| `m` | Merge (PRs) |
| `O` | Reopen |
| `C` | Close |
| `B` | Open in browser |
| `r` | Refresh |
| `q` | Back |

## License

GPL-3.0-or-later
