"""
canned_response_launcher.py
============================
A terminal-based (TUI) canned response tool for Windows.

Usage:
    python canned_response_launcher.py

Requirements:
    pip install rapidfuzz windows-curses

Files (created automatically in same directory as this script if missing):
    canned_responses.csv  — the response database
    canned_config.json    — signature and other config

Keybindings (search screen):
    Type anything       — filter results
    Up / Down           — navigate results
    Enter               — copy selected to clipboard (app stays open)
    Ctrl-P              — toggle preview of full response body
    Ctrl-N              — new entry
    Ctrl-E              — edit selected entry
    Ctrl-D              — delete selected entry
    Ctrl-G              — edit signature
    Ctrl-K / Esc        — clear search query
    Ctrl-Q              — quit

Architecture overview:
    Response            — dataclass representing one canned response entry
    load_csv/save_csv   — read/write the CSV database
    load_config/save_config — read/write JSON config (signature)
    search()            — hybrid fuzzy + keyword scoring via rapidfuzz;
                          when no query is active, sorts by usage count
    TextBuffer          — mini text editor: tracks lines[], cursor row/col,
                          handles insert/delete/navigation/paste
    App                 — main TUI controller; owns all state and drives the
                          curses event loop. Modes: search, preview, add,
                          edit, signature. Each mode has a _draw_* and
                          _key_* method pair.
"""

import curses
import csv
import json
import subprocess
import textwrap
from pathlib import Path
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Config / constants
# ---------------------------------------------------------------------------

# Both files live next to this script, created automatically if missing
DEFAULT_CSV = Path(__file__).parent / "canned_responses.csv"
CONFIG_FILE = Path(__file__).parent / "canned_config.json"

# Column order for the CSV
CSV_FIELDS = ["id", "body", "keywords", "uses"]

# Max results shown in search list
TOP_N = 10

# Minimum combined fuzzy+keyword score (0-100+) to include a result.
# Lower = more permissive matching. Raise if too many irrelevant results appear.
FUZZY_THRESHOLD = 35

try:
    from rapidfuzz import fuzz
except ImportError:
    print("pip install rapidfuzz")
    exit(1)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class Response:
    """One canned response entry loaded from the CSV."""
    rid: str        # unique string ID (auto-incremented integer stored as str)
    body: str       # the full response text
    keywords: str   # optional comma-separated search tags
    uses: int = 0   # copy count — used to rank results when no query is active

    def searchable_text(self):
        """Combined text that fuzzy search runs against."""
        return f"{self.body} {self.keywords}"

    def keyword_list(self):
        """Keywords as a cleaned list of lowercase strings."""
        return [k.strip().lower() for k in self.keywords.split(",") if k.strip()]

    def snippet(self, width=50):
        """First line of body, truncated to width, for display in the list."""
        first_line = self.body.split("\n")[0]
        return first_line[:width] + ("…" if len(first_line) > width else "")


# ---------------------------------------------------------------------------
# CSV persistence
# ---------------------------------------------------------------------------

def load_csv(path):
    """
    Load all responses from CSV. The 'uses' column is optional — rows missing
    it (e.g. from an older CSV) default to 0 so existing databases work fine.
    """
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        results = []
        for row in reader:
            try:
                uses = int(row.get("uses", 0) or 0)
            except ValueError:
                uses = 0
            results.append(Response(
                rid=row.get("id", "").strip(),
                body=row.get("body", "").strip(),
                keywords=row.get("keywords", "").strip(),
                uses=uses,
            ))
        return results


def save_csv(path, responses):
    """Write all responses to CSV, overwriting the file."""
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for r in responses:
            writer.writerow({"id": r.rid, "body": r.body, "keywords": r.keywords, "uses": r.uses})


def ensure_csv(path):
    """Create an empty CSV with headers if the file doesn't exist yet."""
    if not path.exists():
        save_csv(path, [])


# ---------------------------------------------------------------------------
# Config persistence (signature, etc.)
# ---------------------------------------------------------------------------

def load_config(path):
    """Load config JSON. Returns defaults if file is missing."""
    if not path.exists():
        return {"signature": ""}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_config(path, config):
    """Write config dict to JSON."""
    with open(path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)


def next_id(responses):
    """
    Return the next available integer ID as a string.
    Scans existing IDs and returns max+1, or "1" if the list is empty.
    """
    nums = []
    for r in responses:
        try:
            nums.append(int(r.rid))
        except ValueError:
            pass
    return str(max(nums, default=0) + 1)


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

def search(query, responses, top_n=TOP_N):
    """
    Hybrid fuzzy + keyword search using rapidfuzz.

    Scoring breakdown per result:
      fuzzy_score   — fuzz.WRatio against full body+keywords text (0-100)
      keyword_bonus — +20 per query word that exactly matches a keyword tag
      title_bonus   — extra weight if the query strongly matches the first line

    With no query: returns all responses sorted by uses descending (most-used first).
    With a query: returns top_n results above FUZZY_THRESHOLD, sorted by score.
    """
    if not query.strip():
        # No active search — rank by copy frequency so most-used float to top
        sorted_by_use = sorted(responses, key=lambda r: r.uses, reverse=True)
        return [(r, 0.0) for r in sorted_by_use[:top_n]]

    q = query.lower()
    q_words = set(q.split())
    scored = []
    for r in responses:
        fuzzy = fuzz.WRatio(q, r.searchable_text().lower())
        kw_hits = len(q_words & set(r.keyword_list()))
        kw_bonus = kw_hits * 20
        title_bonus = max(0, fuzz.partial_ratio(q, r.snippet(100).lower()) - 50) * 0.4
        score = fuzzy + kw_bonus + title_bonus
        if score >= FUZZY_THRESHOLD:
            scored.append((r, round(score, 1)))
    scored.sort(key=lambda x: x[1], reverse=True)
    return scored[:top_n]


# ---------------------------------------------------------------------------
# Clipboard (Windows)
# ---------------------------------------------------------------------------

def copy_to_clipboard(text):
    """
    Write text to the Windows clipboard via clip.exe.
    Uses UTF-16-LE encoding which clip.exe handles correctly for unicode.
    """
    try:
        subprocess.run("clip", input=text.encode("utf-16-le"), check=True, shell=True)
        return True
    except Exception:
        return False


def paste_from_clipboard():
    """
    Read text from the Windows clipboard via PowerShell Get-Clipboard.
    Returns empty string on failure.
    """
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", "Get-Clipboard"],
            capture_output=True, text=True, timeout=3
        )
        return result.stdout.rstrip("\r\n")
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# TextBuffer — mini text editor
# ---------------------------------------------------------------------------

class TextBuffer:
    """
    An editable text buffer that tracks cursor position by (row, col).

    Internally stores text as a list of strings (one per line). Supports
    character insert/delete, newlines, cursor movement, and clipboard paste.
    Used for the body field, keywords field, and signature editor.

    multiline=False restricts to a single line (used for keywords).
    """

    def __init__(self, text="", multiline=True):
        self.multiline = multiline
        self.lines = text.split("\n") if multiline else [text]
        # Start cursor at end of text
        self.row = len(self.lines) - 1
        self.col = len(self.lines[self.row])

    def get_text(self):
        """Return full text as a single string."""
        return "\n".join(self.lines) if self.multiline else self.lines[0]

    def insert(self, ch):
        """Insert a single character at the current cursor position."""
        line = self.lines[self.row]
        self.lines[self.row] = line[:self.col] + ch + line[self.col:]
        self.col += 1

    def insert_newline(self):
        """
        Split the current line at the cursor and insert a new line below.
        No-op in single-line mode.
        """
        if not self.multiline:
            return
        line = self.lines[self.row]
        self.lines[self.row] = line[:self.col]
        self.lines.insert(self.row + 1, line[self.col:])
        self.row += 1
        self.col = 0

    def backspace(self):
        """
        Delete the character before the cursor. If at the start of a line,
        merge with the line above (joining lines).
        """
        if self.col > 0:
            line = self.lines[self.row]
            self.lines[self.row] = line[:self.col - 1] + line[self.col:]
            self.col -= 1
        elif self.row > 0 and self.multiline:
            # Join this line onto the end of the previous line
            prev = self.lines[self.row - 1]
            self.col = len(prev)
            self.lines[self.row - 1] = prev + self.lines[self.row]
            del self.lines[self.row]
            self.row -= 1

    def delete(self):
        """
        Delete the character under the cursor (forward delete). If at end
        of a line, merge the next line down into this one.
        """
        line = self.lines[self.row]
        if self.col < len(line):
            self.lines[self.row] = line[:self.col] + line[self.col + 1:]
        elif self.row < len(self.lines) - 1 and self.multiline:
            self.lines[self.row] = line + self.lines[self.row + 1]
            del self.lines[self.row + 1]

    def move_left(self):
        """Move cursor left one character, wrapping to end of previous line."""
        if self.col > 0:
            self.col -= 1
        elif self.row > 0 and self.multiline:
            self.row -= 1
            self.col = len(self.lines[self.row])

    def move_right(self):
        """Move cursor right one character, wrapping to start of next line."""
        if self.col < len(self.lines[self.row]):
            self.col += 1
        elif self.row < len(self.lines) - 1 and self.multiline:
            self.row += 1
            self.col = 0

    def move_up(self):
        """Move cursor up one line, clamping col to line length."""
        if self.row > 0:
            self.row -= 1
            self.col = min(self.col, len(self.lines[self.row]))

    def move_down(self):
        """Move cursor down one line, clamping col to line length."""
        if self.row < len(self.lines) - 1:
            self.row += 1
            self.col = min(self.col, len(self.lines[self.row]))

    def home(self):
        """Move cursor to start of current line."""
        self.col = 0

    def end(self):
        """Move cursor to end of current line."""
        self.col = len(self.lines[self.row])

    def paste(self, text):
        """Insert pasted text character by character, handling newlines."""
        for ch in text:
            if ch == "\n" and self.multiline:
                self.insert_newline()
            elif ch != "\n":
                self.insert(ch)

    def clear(self):
        """Reset buffer to empty."""
        self.lines = [""]
        self.row = 0
        self.col = 0

    def render(self, win, start_row, start_col, max_rows, width, active=True):
        """
        Draw buffer contents into a curses window region and return the
        screen (row, col) where the cursor currently sits.

        Long lines are word-wrapped to fit within width. The cursor position
        is tracked across wrapped segments so draw_cursor() knows where to
        render the highlight.
        """
        cur_screen_row = start_row
        cur_screen_col = start_col
        screen_row = start_row
        for li, line in enumerate(self.lines):
            wrapped = textwrap.wrap(line, width) if line else [""]
            for wi, wl in enumerate(wrapped):
                if screen_row >= start_row + max_rows:
                    break
                try:
                    win.addstr(screen_row, start_col, wl.ljust(width)[:width])
                except curses.error:
                    pass
                # Track which screen row/col the cursor falls on
                if li == self.row and active:
                    seg_start = wi * width
                    if seg_start <= self.col <= seg_start + width:
                        cur_screen_row = screen_row
                        cur_screen_col = start_col + (self.col - seg_start)
                screen_row += 1
        return cur_screen_row, cur_screen_col


# ---------------------------------------------------------------------------
# TUI helpers
# ---------------------------------------------------------------------------

def safe_addstr(win, y, x, text, attr=0):
    """addstr that silently ignores out-of-bounds or clipping errors."""
    h, w = win.getmaxyx()
    if y < 0 or y >= h:
        return
    try:
        win.addstr(y, x, text[:max(0, w - x - 1)], attr)
    except curses.error:
        pass


def draw_hline(win, y, char="─"):
    """Draw a full-width horizontal line at row y."""
    h, w = win.getmaxyx()
    if 0 <= y < h:
        try:
            win.addstr(y, 0, char * (w - 1))
        except curses.error:
            pass


def clamp(val, lo, hi):
    return max(lo, min(hi, val))


def draw_cursor(win, buf, screen_row, screen_col):
    """
    Highlight the character under the cursor by drawing it in reverse video.
    If the cursor is at end of line, highlights a space placeholder.
    """
    try:
        line = buf.lines[buf.row]
        ch = line[buf.col] if buf.col < len(line) else " "
        win.addstr(screen_row, screen_col, ch, curses.A_REVERSE)
    except curses.error:
        pass


# ---------------------------------------------------------------------------
# App — main TUI controller
# ---------------------------------------------------------------------------

class App:
    """
    Main application class. Owns all state and drives the curses event loop.

    Modes:
      "search"    — default; type to filter, navigate with arrows, Enter to copy
      "preview"   — full-screen view of the selected response body + signature
      "add"       — form to create a new response (body + optional keywords)
      "edit"      — same form, pre-populated with the selected entry
      "signature" — multi-line editor for the global signature appended to all copies

    State:
      self.responses  — full list of Response objects loaded from CSV
      self.results    — current search results as [(Response, score), ...]
      self.sel        — index into self.results for the highlighted row
      self.query      — current search string
      self.status     — one-line message shown at the bottom of the screen
      self.mode       — which screen is active
      self.body_buf   — TextBuffer for the body field in add/edit
      self.kw_buf     — TextBuffer for the keywords field in add/edit
      self.sig_buf    — TextBuffer for the signature editor
      self.active_buf — which of body_buf/kw_buf has focus ("body" or "kw")
      self.editing_rid — rid of the entry being edited (None when adding)
    """

    def __init__(self, stdscr, csv_path, config_path):
        self.scr = stdscr
        self.csv_path = csv_path
        self.config_path = config_path
        self.responses = load_csv(csv_path)
        self.config = load_config(config_path)

        self.query = ""
        self.results = []
        self.sel = 0
        self.status = ""
        self.mode = "search"

        self.body_buf = TextBuffer(multiline=True)
        self.kw_buf = TextBuffer(multiline=False)
        self.active_buf = "body"
        self.editing_rid = None

        self.sig_buf = TextBuffer(multiline=True)

        self._refresh()

    def _refresh(self):
        """Re-run search with current query and clamp selection to valid range."""
        self.results = search(self.query, self.responses)
        self.sel = clamp(self.sel, 0, max(0, len(self.results) - 1))

    def run(self):
        """Main event loop. Dispatches keypresses to the active mode handler."""
        curses.curs_set(0)
        curses.use_default_colors()
        self.scr.keypad(True)
        self.scr.timeout(50)  # non-blocking getch; returns -1 if no key within 50ms
        while True:
            self.draw()
            key = self.scr.getch()
            if key == -1:
                continue
            if self.mode == "search":
                if not self._key_search(key):
                    break
            elif self.mode == "preview":
                if not self._key_preview(key):
                    break
            elif self.mode in ("add", "edit"):
                if not self._key_form(key):
                    break
            elif self.mode == "signature":
                if not self._key_signature(key):
                    break

    # ---- Draw methods (one per mode) ----

    def draw(self):
        """Erase screen and redraw the active mode."""
        self.scr.erase()
        h, w = self.scr.getmaxyx()
        if self.mode == "search":
            self._draw_search(h, w)
        elif self.mode == "preview":
            self._draw_preview(h, w)
        elif self.mode in ("add", "edit"):
            self._draw_form(h, w)
        elif self.mode == "signature":
            self._draw_signature(h, w)
        self.scr.refresh()

    def _draw_search(self, h, w):
        """
        Search screen layout:
          Row 0       — header bar
          Row 2       — search input + result count
          Row 3       — horizontal divider
          Rows 4..h-4 — result list; selected row shows keywords below it
          Row h-3     — horizontal divider
          Row h-2     — keybinding footer
          Row h-1     — status message
        """
        safe_addstr(self.scr, 0, 0, " Canned Response Launcher ".center(w - 1), curses.A_REVERSE)
        safe_addstr(self.scr, 2, 2, "Search: ", curses.A_BOLD)
        safe_addstr(self.scr, 2, 10, self.query[-(w - 12):])
        count_str = f"{len(self.results)} result{'s' if len(self.results) != 1 else ''}"
        safe_addstr(self.scr, 2, w - len(count_str) - 3, count_str, curses.A_DIM)
        draw_hline(self.scr, 3)

        list_start = 4
        list_end = h - 3
        for i, (resp, score) in enumerate(self.results):
            row = list_start + i
            if row >= list_end:
                break
            num = f"  {i+1:>2}. "
            snippet = resp.snippet(w - len(num) - 10)
            # Only show score when a query is active; score is meaningless in usage-sort mode
            score_str = f" [{score:>5.1f}]" if self.query else ""
            line = f"{num}{snippet}{score_str}"
            if i == self.sel:
                safe_addstr(self.scr, row, 0, line.ljust(w - 1), curses.A_REVERSE)
                # Show keywords as a dim hint beneath the selected entry
                if resp.keywords and row + 1 < list_end:
                    safe_addstr(self.scr, row + 1, 0,
                        f"       keywords: {resp.keywords}"[:w - 1], curses.A_DIM)
            else:
                safe_addstr(self.scr, row, 0, line[:w - 1])

        draw_hline(self.scr, h - 3)
        safe_addstr(self.scr, h - 2, 0,
            " [↑↓] navigate  [Enter] copy  [Ctrl-P] preview  [Ctrl-N] new  [Ctrl-E] edit  [Ctrl-D] delete  [Ctrl-G] signature  [Ctrl-K] clear  [Ctrl-Q] quit"[:w - 1],
            curses.A_REVERSE)
        if self.status:
            safe_addstr(self.scr, h - 1, 2, self.status[:w - 3], curses.A_DIM)

    def _draw_preview(self, h, w):
        """
        Preview screen: shows the full body of the selected response, word-wrapped,
        with the configured signature appended and separated by a blank line.
        """
        if not self.results:
            self.mode = "search"
            return
        resp, _ = self.results[self.sel]
        safe_addstr(self.scr, 0, 0, " Preview ".center(w - 1), curses.A_REVERSE)
        if resp.keywords:
            safe_addstr(self.scr, 1, 2, f"keywords: {resp.keywords}", curses.A_DIM)
        draw_hline(self.scr, 2)
        row = 3
        sig = self.config.get("signature", "")
        full_text = resp.body + ("\n\n" + sig if sig else "")
        for line in full_text.split("\n"):
            for wl in textwrap.wrap(line, w - 4) or [""]:
                if row >= h - 3:
                    break
                safe_addstr(self.scr, row, 2, wl)
                row += 1
            if row >= h - 3:
                break
        draw_hline(self.scr, h - 3)
        safe_addstr(self.scr, h - 2, 0,
            " [Enter] copy  [Ctrl-P / Esc] back  [Ctrl-Q] quit"[:w - 1],
            curses.A_REVERSE)

    def _draw_form(self, h, w):
        """
        Add/edit form layout:
          - Body field occupies roughly the top half of the screen (multiline TextBuffer)
          - Keywords field occupies a single line below a divider
          - Active field marked with ◄ indicator
          - Cursor rendered as a reversed character in the active field
        """
        label = " New Response " if self.mode == "add" else " Edit Response "
        safe_addstr(self.scr, 0, 0, label.center(w - 1), curses.A_REVERSE)

        body_active = self.active_buf == "body"
        kw_active = self.active_buf == "kw"

        safe_addstr(self.scr, 2, 2,
            "Body (required)" + (" ◄" if body_active else ""),
            curses.A_BOLD)
        safe_addstr(self.scr, 2, w - 48,
            "[Enter] newline  [Ctrl-V] paste  [Tab] switch field",
            curses.A_DIM)

        body_top = 3
        body_rows = (h - 10) // 2
        draw_hline(self.scr, body_top + body_rows)

        cr, cc = self.body_buf.render(self.scr, body_top, 2, body_rows, w - 4, active=body_active)
        if body_active:
            draw_cursor(self.scr, self.body_buf, cr, cc)

        kw_top = body_top + body_rows + 1
        safe_addstr(self.scr, kw_top, 2,
            "Keywords — optional, comma-separated" + (" ◄" if kw_active else ""),
            curses.A_BOLD)

        kw_row = kw_top + 1
        kw_text = self.kw_buf.get_text()
        kw_disp = kw_text[-(w - 6):]
        safe_addstr(self.scr, kw_row, 4, kw_disp)
        if kw_active:
            cur_col = 4 + min(self.kw_buf.col, w - 6)
            try:
                ch = kw_text[self.kw_buf.col] if self.kw_buf.col < len(kw_text) else " "
                self.scr.addstr(kw_row, cur_col, ch, curses.A_REVERSE)
            except curses.error:
                pass

        draw_hline(self.scr, h - 3)
        safe_addstr(self.scr, h - 2, 0,
            " [Ctrl-S] save  [Esc] cancel  [Tab] switch field"[:w - 1],
            curses.A_REVERSE)
        if self.status:
            safe_addstr(self.scr, h - 1, 2, self.status[:w - 3], curses.A_DIM)

    def _draw_signature(self, h, w):
        """
        Signature editor: full-screen multiline TextBuffer. The signature is
        stored in canned_config.json and appended to every copied response.
        """
        safe_addstr(self.scr, 0, 0, " Signature ".center(w - 1), curses.A_REVERSE)
        safe_addstr(self.scr, 1, 2,
            "Appended to all responses.  Use arrow keys, Home, End to navigate.",
            curses.A_DIM)
        draw_hline(self.scr, 2)

        sig_rows = h - 7
        cr, cc = self.sig_buf.render(self.scr, 3, 2, sig_rows, w - 4, active=True)
        draw_cursor(self.scr, self.sig_buf, cr, cc)

        draw_hline(self.scr, h - 3)
        safe_addstr(self.scr, h - 2, 0,
            " [Ctrl-S] save  [Esc] cancel  [Ctrl-K] clear  [Enter] newline  [Ctrl-V] paste"[:w - 1],
            curses.A_REVERSE)
        if self.status:
            safe_addstr(self.scr, h - 1, 2, self.status[:w - 3], curses.A_DIM)

    # ---- Key handlers (one per mode) ----

    def _clear_search(self):
        """Reset search query and refresh results."""
        self.query = ""
        self.status = ""
        self._refresh()

    def _key_search(self, key):
        """
        Handle keypresses on the search screen.
        All printable characters are appended to the search query.
        Control sequences trigger commands.
        Returns False to signal the event loop to exit.
        """
        if key == 17:       # Ctrl-Q — quit
            return False
        elif key == 27:     # Esc — clear search
            self._clear_search()
        elif key == 11:     # Ctrl-K — clear search
            self._clear_search()
        elif key in (curses.KEY_BACKSPACE, 127, 8):  # Backspace
            self.query = self.query[:-1]
            self.status = ""
            self._refresh()
        elif key == curses.KEY_UP:
            self.sel = max(0, self.sel - 1)
        elif key == curses.KEY_DOWN:
            self.sel = min(len(self.results) - 1, self.sel + 1)
        elif key == ord('\n'):  # Enter — copy and stay open
            self._copy_selected()
        elif key == 16:     # Ctrl-P — preview
            if self.results:
                self.mode = "preview"
                self.status = ""
        elif key == 14:     # Ctrl-N — new entry
            self.body_buf = TextBuffer(multiline=True)
            self.kw_buf = TextBuffer(multiline=False)
            self.active_buf = "body"
            self.editing_rid = None
            self.status = ""
            self.mode = "add"
        elif key == 5:      # Ctrl-E — edit selected
            if self.results:
                resp, _ = self.results[self.sel]
                self.body_buf = TextBuffer(resp.body, multiline=True)
                self.kw_buf = TextBuffer(resp.keywords, multiline=False)
                self.active_buf = "body"
                self.editing_rid = resp.rid
                self.status = ""
                self.mode = "edit"
        elif key == 4:      # Ctrl-D — delete selected
            self._delete_selected()
        elif key == 7:      # Ctrl-G — signature editor
            self.sig_buf = TextBuffer(self.config.get("signature", ""), multiline=True)
            self.status = ""
            self.mode = "signature"
        elif 32 <= key <= 126:  # Any printable character goes to the search query
            self.query += chr(key)
            self.sel = 0
            self.status = ""
            self._refresh()
        return True

    def _key_preview(self, key):
        """Handle keypresses on the preview screen."""
        if key == 17:           # Ctrl-Q — quit
            return False
        elif key in (27, 16):   # Esc or Ctrl-P — back to search
            self.mode = "search"
            self.status = ""
        elif key == ord('\n'):  # Enter — copy and return to search
            self._copy_selected()
            self.mode = "search"
        return True

    def _route_key_to_buf(self, buf, key):
        """
        Forward a navigation or editing key to a TextBuffer.
        Covers arrows, Home/End, Delete, Backspace, and printable characters.
        """
        if key == curses.KEY_LEFT:
            buf.move_left()
        elif key == curses.KEY_RIGHT:
            buf.move_right()
        elif key == curses.KEY_UP:
            buf.move_up()
        elif key == curses.KEY_DOWN:
            buf.move_down()
        elif key == curses.KEY_HOME:
            buf.home()
        elif key == curses.KEY_END:
            buf.end()
        elif key == curses.KEY_DC:      # Delete key (forward delete)
            buf.delete()
        elif key in (curses.KEY_BACKSPACE, 127, 8):
            buf.backspace()
        elif 32 <= key <= 126:
            buf.insert(chr(key))

    def _key_form(self, key):
        """
        Handle keypresses in the add/edit form.
        Tab switches between body and keywords fields.
        Enter inserts a newline in body, or saves when on keywords.
        Ctrl-S always saves.
        """
        buf = self.body_buf if self.active_buf == "body" else self.kw_buf
        if key == 27:           # Esc — cancel, back to search
            self.mode = "search"
            self.status = "Cancelled."
        elif key == 19:         # Ctrl-S — save
            self._save_form()
        elif key == ord('\t'):  # Tab — switch field
            self.active_buf = "kw" if self.active_buf == "body" else "body"
        elif key == 22:         # Ctrl-V — paste from clipboard
            pasted = paste_from_clipboard()
            if pasted:
                buf.paste(pasted)
                self.status = f"Pasted {len(pasted)} chars."
            else:
                self.status = "Nothing in clipboard."
        elif key == ord('\n'):  # Enter — newline in body, save in keywords
            if self.active_buf == "body":
                buf.insert_newline()
            else:
                self._save_form()
        else:
            self._route_key_to_buf(buf, key)
        return True

    def _key_signature(self, key):
        """Handle keypresses in the signature editor."""
        if key == 27:           # Esc — cancel
            self.mode = "search"
            self.status = "Cancelled."
        elif key == 19:         # Ctrl-S — save signature to config
            self.config["signature"] = self.sig_buf.get_text()
            save_config(self.config_path, self.config)
            self.mode = "search"
            self.status = "Signature saved."
        elif key == 11:         # Ctrl-K — clear signature buffer
            self.sig_buf.clear()
            self.status = "Cleared."
        elif key == 22:         # Ctrl-V — paste from clipboard
            pasted = paste_from_clipboard()
            if pasted:
                self.sig_buf.paste(pasted)
                self.status = f"Pasted {len(pasted)} chars."
            else:
                self.status = "Nothing in clipboard."
        elif key == ord('\n'):  # Enter — insert newline
            self.sig_buf.insert_newline()
        else:
            self._route_key_to_buf(self.sig_buf, key)
        return True

    # ---- Actions ----

    def _copy_selected(self):
        """
        Copy the selected response (body + signature) to the Windows clipboard.
        Increments the usage counter and saves the CSV so ranking updates over time.
        App stays open after copying.
        """
        if not self.results:
            self.status = "Nothing to copy."
            return
        resp, _ = self.results[self.sel]
        sig = self.config.get("signature", "")
        full = resp.body + ("\n\n" + sig if sig else "")
        if copy_to_clipboard(full):
            # Increment use count on the master list (not just the search result)
            for r in self.responses:
                if r.rid == resp.rid:
                    r.uses += 1
                    break
            save_csv(self.csv_path, self.responses)
            self._refresh()
            self.status = f"✓ Copied — {resp.snippet(40)}"
        else:
            self.status = "Clipboard copy failed."

    def _delete_selected(self):
        """Remove the selected response from the list and save."""
        if not self.results:
            return
        resp, _ = self.results[self.sel]
        self.responses = [r for r in self.responses if r.rid != resp.rid]
        save_csv(self.csv_path, self.responses)
        self._refresh()
        self.status = f"Deleted entry #{resp.rid}."

    def _save_form(self):
        """
        Save the add/edit form. For new entries, appends to the list with
        a fresh auto-incremented ID. For edits, updates the matching entry in place.
        """
        body = self.body_buf.get_text().strip()
        if not body:
            self.status = "Body is required."
            return
        keywords = self.kw_buf.get_text().strip()
        if self.mode == "add":
            self.responses.append(Response(
                rid=next_id(self.responses), body=body, keywords=keywords, uses=0))
        else:
            for r in self.responses:
                if r.rid == self.editing_rid:
                    r.body = body
                    r.keywords = keywords
                    break
        save_csv(self.csv_path, self.responses)
        self._refresh()
        self.mode = "search"
        self.status = "Saved."


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    ensure_csv(DEFAULT_CSV)
    curses.wrapper(lambda s: App(s, DEFAULT_CSV, CONFIG_FILE).run())

if __name__ == "__main__":
    main()