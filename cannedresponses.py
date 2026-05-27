import curses
import csv
import json
import subprocess
import textwrap
from pathlib import Path
from dataclasses import dataclass, field

DEFAULT_CSV = Path(__file__).parent / "canned_responses.csv"
CONFIG_FILE = Path(__file__).parent / "canned_config.json"
CSV_FIELDS = ["id", "body", "keywords"]
TOP_N = 10
FUZZY_THRESHOLD = 35

try:
    from rapidfuzz import fuzz
except ImportError:
    print("pip install rapidfuzz")
    exit(1)

# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

@dataclass
class Response:
    rid: str
    body: str
    keywords: str = ""

    def searchable_text(self):
        return f"{self.body} {self.keywords}"

    def keyword_list(self):
        return [k.strip().lower() for k in self.keywords.split(",") if k.strip()]

    def snippet(self, width=50):
        first_line = self.body.split("\n")[0]
        return first_line[:width] + ("…" if len(first_line) > width else "")


def load_csv(path):
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        return [Response(
            rid=row.get("id", "").strip(),
            body=row.get("body", "").strip(),
            keywords=row.get("keywords", "").strip(),
        ) for row in reader]


def save_csv(path, responses):
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for r in responses:
            writer.writerow({"id": r.rid, "body": r.body, "keywords": r.keywords})


def ensure_csv(path):
    if not path.exists():
        save_csv(path, [])


def load_config(path):
    if not path.exists():
        return {"signature": ""}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_config(path, config):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)


def next_id(responses):
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
    if not query.strip():
        return [(r, 0.0) for r in responses[:top_n]]
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
# Clipboard
# ---------------------------------------------------------------------------

def copy_to_clipboard(text):
    try:
        subprocess.run("clip", input=text.encode("utf-16-le"), check=True, shell=True)
        return True
    except Exception:
        return False


def paste_from_clipboard():
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", "Get-Clipboard"],
            capture_output=True, text=True, timeout=3
        )
        return result.stdout.rstrip("\r\n")
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Mini text editor buffer
# ---------------------------------------------------------------------------

class TextBuffer:
    """Editable text buffer with line/col cursor tracking."""

    def __init__(self, text="", multiline=True):
        self.multiline = multiline
        self.lines = text.split("\n") if multiline else [text]
        self.row = len(self.lines) - 1
        self.col = len(self.lines[self.row])

    def get_text(self):
        return "\n".join(self.lines) if self.multiline else self.lines[0]

    def set_text(self, text):
        self.lines = text.split("\n") if self.multiline else [text.replace("\n", " ")]
        self.row = len(self.lines) - 1
        self.col = len(self.lines[self.row])

    def insert(self, ch):
        line = self.lines[self.row]
        self.lines[self.row] = line[:self.col] + ch + line[self.col:]
        self.col += 1

    def insert_newline(self):
        if not self.multiline:
            return
        line = self.lines[self.row]
        self.lines[self.row] = line[:self.col]
        self.lines.insert(self.row + 1, line[self.col:])
        self.row += 1
        self.col = 0

    def backspace(self):
        if self.col > 0:
            line = self.lines[self.row]
            self.lines[self.row] = line[:self.col - 1] + line[self.col:]
            self.col -= 1
        elif self.row > 0 and self.multiline:
            prev = self.lines[self.row - 1]
            self.col = len(prev)
            self.lines[self.row - 1] = prev + self.lines[self.row]
            del self.lines[self.row]
            self.row -= 1

    def delete(self):
        line = self.lines[self.row]
        if self.col < len(line):
            self.lines[self.row] = line[:self.col] + line[self.col + 1:]
        elif self.row < len(self.lines) - 1 and self.multiline:
            self.lines[self.row] = line + self.lines[self.row + 1]
            del self.lines[self.row + 1]

    def move_left(self):
        if self.col > 0:
            self.col -= 1
        elif self.row > 0 and self.multiline:
            self.row -= 1
            self.col = len(self.lines[self.row])

    def move_right(self):
        if self.col < len(self.lines[self.row]):
            self.col += 1
        elif self.row < len(self.lines) - 1 and self.multiline:
            self.row += 1
            self.col = 0

    def move_up(self):
        if self.row > 0:
            self.row -= 1
            self.col = min(self.col, len(self.lines[self.row]))

    def move_down(self):
        if self.row < len(self.lines) - 1:
            self.row += 1
            self.col = min(self.col, len(self.lines[self.row]))

    def home(self):
        self.col = 0

    def end(self):
        self.col = len(self.lines[self.row])

    def paste(self, text):
        for ch in text:
            if ch == "\n" and self.multiline:
                self.insert_newline()
            elif ch != "\n":
                self.insert(ch)

    def clear(self):
        self.lines = [""]
        self.row = 0
        self.col = 0

    def render(self, win, start_row, start_col, max_rows, width, active=True):
        """Render buffer into win. Returns screen row/col of cursor."""
        cur_screen_row = start_row
        cur_screen_col = start_col

        # Build display lines, tracking which screen row/col the cursor lands on
        screen_row = start_row
        for li, line in enumerate(self.lines):
            # wrap long lines
            if len(line) == 0:
                wrapped = [""]
            else:
                wrapped = textwrap.wrap(line, width) or [""]

            col_remaining = self.col if li == self.row else -1

            for wi, wl in enumerate(wrapped):
                if screen_row >= start_row + max_rows:
                    break
                # draw line — no background highlight, plain text
                try:
                    win.addstr(screen_row, start_col, wl.ljust(width)[:width])
                except curses.error:
                    pass

                # figure out cursor position within this wrapped segment
                if li == self.row and active:
                    seg_start = wi * width
                    seg_end = seg_start + width
                    if seg_start <= self.col <= seg_end:
                        cur_screen_row = screen_row
                        cur_screen_col = start_col + (self.col - seg_start)

                screen_row += 1

        return cur_screen_row, cur_screen_col


# ---------------------------------------------------------------------------
# TUI helpers
# ---------------------------------------------------------------------------

def safe_addstr(win, y, x, text, attr=0):
    h, w = win.getmaxyx()
    if y < 0 or y >= h:
        return
    max_len = max(0, w - x - 1)
    try:
        win.addstr(y, x, text[:max_len], attr)
    except curses.error:
        pass


def draw_hline(win, y, char="─"):
    h, w = win.getmaxyx()
    if 0 <= y < h:
        try:
            win.addstr(y, 0, char * (w - 1))
        except curses.error:
            pass


def clamp(val, lo, hi):
    return max(lo, min(hi, val))


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

class App:
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

        # form state
        self.body_buf = TextBuffer(multiline=True)
        self.kw_buf = TextBuffer(multiline=False)
        self.active_buf = "body"  # "body" or "kw"
        self.editing_rid = None

        # signature state
        self.sig_buf = TextBuffer(multiline=True)

        self._refresh()

    def _refresh(self):
        self.results = search(self.query, self.responses)
        self.sel = clamp(self.sel, 0, max(0, len(self.results) - 1))

    def run(self):
        curses.curs_set(1)
        curses.use_default_colors()
        self.scr.keypad(True)
        self.scr.timeout(50)
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

    # ---- Draw ----

    def draw(self):
        self.scr.erase()
        h, w = self.scr.getmaxyx()
        curses.curs_set(0)  # hide by default, show only in editor modes
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
        safe_addstr(self.scr, 0, 0, " Canned Response Launcher ".center(w - 1), curses.A_REVERSE)
        safe_addstr(self.scr, 2, 2, "Search: ", curses.A_BOLD)
        q_disp = self.query[-(w - 12):]
        safe_addstr(self.scr, 2, 10, q_disp)
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
            score_str = f" [{score:>5.1f}]" if self.query else ""
            line = f"{num}{snippet}{score_str}"
            if i == self.sel:
                safe_addstr(self.scr, row, 0, line.ljust(w - 1), curses.A_REVERSE)
                if resp.keywords and row + 1 < list_end:
                    safe_addstr(self.scr, row + 1, 0, f"       keywords: {resp.keywords}"[:w - 1], curses.A_DIM)
            else:
                safe_addstr(self.scr, row, 0, line[:w - 1])

        draw_hline(self.scr, h - 3)
        safe_addstr(self.scr, h - 2, 0,
            " [↑↓] navigate  [enter] copy  [^P] preview  [^N] new  [^E] edit  [^D] delete  [^G] signature  [^Q] quit"[:w - 1],
            curses.A_REVERSE)
        if self.status:
            safe_addstr(self.scr, h - 1, 2, self.status[:w - 3], curses.A_DIM)

    def _draw_preview(self, h, w):
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
        safe_addstr(self.scr, h - 2, 0, " [enter] copy  [^P / esc] back  [^Q] quit"[:w - 1], curses.A_REVERSE)

    def _draw_form(self, h, w):
        label = " Add New Response " if self.mode == "add" else " Edit Response "
        safe_addstr(self.scr, 0, 0, label.center(w - 1), curses.A_REVERSE)

        # Body section
        body_active = self.active_buf == "body"
        safe_addstr(self.scr, 2, 2,
            "Body (required)" + (" ◄" if body_active else ""),
            curses.A_BOLD)
        safe_addstr(self.scr, 2, w - 45,
            "arrows/home/end  [enter] newline  [ctrl+v] paste",
            curses.A_DIM)

        body_top = 3
        body_rows = (h - 10) // 2
        # draw body area border
        draw_hline(self.scr, body_top + body_rows)

        cr, cc = self.body_buf.render(
            self.scr, body_top, 2, body_rows, w - 4, active=body_active)

        # Keywords section
        kw_top = body_top + body_rows + 1
        kw_active = self.active_buf == "kw"
        safe_addstr(self.scr, kw_top, 2,
            "Keywords — optional, comma-separated" + (" ◄" if kw_active else ""),
            curses.A_BOLD)

        kw_row = kw_top + 1
        kw_text = self.kw_buf.get_text()
        kw_disp = kw_text[-(w - 6):]
        safe_addstr(self.scr, kw_row, 4, kw_disp)
        # draw cursor in kw field
        if kw_active:
            cur_col = 4 + min(self.kw_buf.col, w - 6)
            try:
                ch = (kw_text[self.kw_buf.col] if self.kw_buf.col < len(kw_text) else " ")
                self.scr.addstr(kw_row, cur_col, ch, curses.A_REVERSE)
            except curses.error:
                pass

        draw_hline(self.scr, h - 3)
        safe_addstr(self.scr, h - 2, 0,
            " [tab] switch field  [ctrl+s] save  [esc] cancel"[:w - 1],
            curses.A_REVERSE)
        if self.status:
            safe_addstr(self.scr, h - 1, 2, self.status[:w - 3], curses.A_DIM)

        # show cursor in body field
        if body_active:
            try:
                line = self.body_buf.lines[self.body_buf.row]
                ch = (line[self.body_buf.col] if self.body_buf.col < len(line) else " ")
                self.scr.addstr(cr, cc, ch, curses.A_REVERSE)
            except curses.error:
                pass

    def _draw_signature(self, h, w):
        safe_addstr(self.scr, 0, 0, " Signature ".center(w - 1), curses.A_REVERSE)
        safe_addstr(self.scr, 1, 2,
            "Appended to all responses. arrows/home/end to navigate.",
            curses.A_DIM)
        draw_hline(self.scr, 2)

        sig_rows = h - 7
        self.sig_buf.render(self.scr, 3, 2, sig_rows, w - 4, active=True)

        # draw cursor
        line = self.sig_buf.lines[self.sig_buf.row]
        # compute screen position (simple — no scroll yet)
        screen_row = 3 + self.sig_buf.row
        screen_col = 2 + self.sig_buf.col
        if screen_row < h - 4:
            try:
                ch = (line[self.sig_buf.col] if self.sig_buf.col < len(line) else " ")
                self.scr.addstr(screen_row, screen_col, ch, curses.A_REVERSE)
            except curses.error:
                pass

        draw_hline(self.scr, h - 3)
        safe_addstr(self.scr, h - 2, 0,
            " [ctrl+s] save  [esc] cancel  [ctrl+k] clear  [enter] newline  [ctrl+v] paste"[:w - 1],
            curses.A_REVERSE)
        if self.status:
            safe_addstr(self.scr, h - 1, 2, self.status[:w - 3], curses.A_DIM)

    # ---- Key handlers ----

    def _key_search(self, key):
        if key == 17:  # ctrl+q — quit
            return False
        elif key == 27:  # esc — clear search
            self.query = ""
            self.status = ""
            self._refresh()
        elif key in (curses.KEY_BACKSPACE, 127, 8):
            self.query = self.query[:-1]
            self.status = ""
            self._refresh()
        elif key == curses.KEY_UP:
            self.sel = max(0, self.sel - 1)
        elif key == curses.KEY_DOWN:
            self.sel = min(len(self.results) - 1, self.sel + 1)
        elif key == ord('\n'):
            self._copy_selected()
        elif key == 16:  # ctrl+p — preview
            if self.results:
                self.mode = "preview"
                self.status = ""
        elif key == 14:  # ctrl+n — new
            self.body_buf = TextBuffer(multiline=True)
            self.kw_buf = TextBuffer(multiline=False)
            self.active_buf = "body"
            self.editing_rid = None
            self.status = ""
            self.mode = "add"
        elif key == 5:  # ctrl+e — edit
            if self.results:
                resp, _ = self.results[self.sel]
                self.body_buf = TextBuffer(resp.body, multiline=True)
                self.kw_buf = TextBuffer(resp.keywords, multiline=False)
                self.active_buf = "body"
                self.editing_rid = resp.rid
                self.status = ""
                self.mode = "edit"
        elif key == 4:  # ctrl+d — delete
            self._delete_selected()
        elif key == 7:  # ctrl+g — signature
            self.sig_buf = TextBuffer(self.config.get("signature", ""), multiline=True)
            self.status = ""
            self.mode = "signature"
        elif 32 <= key <= 126:  # any printable char goes to search
            self.query += chr(key)
            self.sel = 0
            self.status = ""
            self._refresh()
        return True

    def _key_preview(self, key):
        if key == 17:  # ctrl+q
            return False
        elif key in (27, 16):  # esc or ctrl+p — back to search
            self.mode = "search"
            self.status = ""
        elif key == ord('\n'):
            self._copy_selected()
            self.mode = "search"
        return True

    def _route_key_to_buf(self, buf, key):
        """Send a key to a TextBuffer. Returns True if handled."""
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
        elif key == curses.KEY_DC:  # delete key
            buf.delete()
        elif key in (curses.KEY_BACKSPACE, 127, 8):
            buf.backspace()
        elif 32 <= key <= 126:
            buf.insert(chr(key))
        else:
            return False
        return True

    def _key_form(self, key):
        buf = self.body_buf if self.active_buf == "body" else self.kw_buf

        if key == 27:
            self.mode = "search"
            self.status = "Cancelled."
        elif key == 19:  # ctrl+s
            self._save_form()
        elif key == ord('\t'):
            self.active_buf = "kw" if self.active_buf == "body" else "body"
        elif key == 22:  # ctrl+v
            pasted = paste_from_clipboard()
            if pasted:
                buf.paste(pasted)
                self.status = f"Pasted {len(pasted)} chars."
            else:
                self.status = "Nothing in clipboard."
        elif key == ord('\n'):
            if self.active_buf == "body":
                buf.insert_newline()
            else:
                self._save_form()
        else:
            self._route_key_to_buf(buf, key)
        return True

    def _key_signature(self, key):
        if key == 27:
            self.mode = "search"
            self.status = "Cancelled."
        elif key == 19:  # ctrl+s
            self.config["signature"] = self.sig_buf.get_text()
            save_config(self.config_path, self.config)
            self.mode = "search"
            self.status = "Signature saved."
        elif key == 11:  # ctrl+k
            self.sig_buf.clear()
            self.status = "Cleared."
        elif key == 22:  # ctrl+v
            pasted = paste_from_clipboard()
            if pasted:
                self.sig_buf.paste(pasted)
                self.status = f"Pasted {len(pasted)} chars."
            else:
                self.status = "Nothing in clipboard."
        elif key == ord('\n'):
            self.sig_buf.insert_newline()
        else:
            self._route_key_to_buf(self.sig_buf, key)
        return True

    # ---- Actions ----

    def _copy_selected(self):
        if not self.results:
            self.status = "Nothing to copy."
            return
        resp, _ = self.results[self.sel]
        sig = self.config.get("signature", "")
        full = resp.body + ("\n\n" + sig if sig else "")
        if copy_to_clipboard(full):
            self.status = f"✓ Copied — {resp.snippet(40)}"
        else:
            self.status = "Clipboard copy failed."

    def _delete_selected(self):
        if not self.results:
            return
        resp, _ = self.results[self.sel]
        self.responses = [r for r in self.responses if r.rid != resp.rid]
        save_csv(self.csv_path, self.responses)
        self._refresh()
        self.status = f"Deleted entry #{resp.rid}."

    def _save_form(self):
        body = self.body_buf.get_text().strip()
        if not body:
            self.status = "Body is required."
            return
        keywords = self.kw_buf.get_text().strip()
        if self.mode == "add":
            self.responses.append(Response(rid=next_id(self.responses), body=body, keywords=keywords))
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
# Main
# ---------------------------------------------------------------------------

def main():
    ensure_csv(DEFAULT_CSV)
    curses.wrapper(lambda s: App(s, DEFAULT_CSV, CONFIG_FILE).run())

if __name__ == "__main__":
    main()