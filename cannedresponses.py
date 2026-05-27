import curses
import csv
import os
import json
import subprocess
import textwrap
from pathlib import Path
from dataclasses import dataclass

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
# Clipboard (Windows clip.exe — works in testing via subprocess)
# ---------------------------------------------------------------------------

def copy_to_clipboard(text):
    try:
        subprocess.run("clip", input=text.encode("utf-16-le"), check=True, shell=True)
        return True
    except Exception:
        return False


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
# Screens
# ---------------------------------------------------------------------------

class App:
    def __init__(self, stdscr, csv_path, config_path):
        self.scr = stdscr
        self.csv_path = csv_path
        self.config_path = config_path
        self.responses = load_csv(csv_path)
        self.config = load_config(config_path)

        # search state
        self.query = ""
        self.results = []
        self.sel = 0
        self.status = ""
        self.mode = "search"  # search | preview | add | edit | signature

        # add/edit state
        self.form = {"body": "", "keywords": ""}
        self.form_field = 0   # 0=body, 1=keywords
        self.editing_rid = None

        # signature state
        self.sig_buf = ""

        self._refresh()

    def _refresh(self):
        self.results = search(self.query, self.responses)
        self.sel = clamp(self.sel, 0, max(0, len(self.results) - 1))

    def run(self):
        curses.curs_set(0)
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
        header = " Canned Response Launcher "
        safe_addstr(self.scr, 0, 0, header.center(w - 1), curses.A_REVERSE)

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
                # show keywords below if present
                if resp.keywords and row + 1 < list_end:
                    kw = f"       keywords: {resp.keywords}"
                    safe_addstr(self.scr, row + 1, 0, kw[:w - 1], curses.A_DIM)
            else:
                safe_addstr(self.scr, row, 0, line[:w - 1])

        draw_hline(self.scr, h - 3)
        footer = " [↑↓] navigate  [enter] copy  [p] preview  [a] add  [e] edit  [d] delete  [q] quit  [s] signature"
        safe_addstr(self.scr, h - 2, 0, footer[:w - 1], curses.A_REVERSE)
        if self.status:
            safe_addstr(self.scr, h - 1, 2, self.status[:w - 3], curses.A_DIM)

    def _draw_preview(self, h, w):
        if not self.results:
            self.mode = "search"
            return
        resp, score = self.results[self.sel]

        header = " Preview "
        safe_addstr(self.scr, 0, 0, header.center(w - 1), curses.A_REVERSE)

        if resp.keywords:
            safe_addstr(self.scr, 1, 2, f"keywords: {resp.keywords}", curses.A_DIM)

        draw_hline(self.scr, 2)

        row = 3
        sig = self.config.get("signature", "")
        full_text = resp.body + ("\n\n" + sig if sig else "")

        for line in full_text.split("\n"):
            wrapped = textwrap.wrap(line, w - 4) or [""]
            for wl in wrapped:
                if row >= h - 3:
                    break
                safe_addstr(self.scr, row, 2, wl)
                row += 1
            if row >= h - 3:
                break

        draw_hline(self.scr, h - 3)
        footer = " [enter] copy & exit  [p / esc] back  [q] quit"
        safe_addstr(self.scr, h - 2, 0, footer[:w - 1], curses.A_REVERSE)

    def _draw_form(self, h, w):
        label = " Add New Response " if self.mode == "add" else " Edit Response "
        safe_addstr(self.scr, 0, 0, label.center(w - 1), curses.A_REVERSE)

        fields = ["body", "keywords"]
        labels = {"body": "Body (required)", "keywords": "Keywords — optional, comma-separated"}

        row = 2
        for i, f in enumerate(fields):
            safe_addstr(self.scr, row, 2, labels[f] + ":", curses.A_BOLD)
            row += 1
            val = self.form[f]
            # word-wrap body for display
            lines = textwrap.wrap(val, w - 6) if f == "body" else [val[-(w - 6):]]
            if not lines:
                lines = [""]
            for li, l in enumerate(lines):
                attr = curses.A_REVERSE if i == self.form_field else curses.A_NORMAL
                disp = l.ljust(w - 5)
                safe_addstr(self.scr, row, 4, disp[:w - 5], attr)
                row += 1
            row += 1

        draw_hline(self.scr, h - 3)
        footer = " [tab] next field  [enter] save  [esc] cancel"
        safe_addstr(self.scr, h - 2, 0, footer[:w - 1], curses.A_REVERSE)
        if self.status:
            safe_addstr(self.scr, h - 1, 2, self.status[:w - 3], curses.A_DIM)

    def _draw_signature(self, h, w):
        safe_addstr(self.scr, 0, 0, " Signature ".center(w - 1), curses.A_REVERSE)
        safe_addstr(self.scr, 2, 2, "Current signature (appended to all copied responses):", curses.A_BOLD)

        row = 4
        lines = textwrap.wrap(self.sig_buf, w - 6) or [""]
        for l in lines:
            if row >= h - 3:
                break
            safe_addstr(self.scr, row, 4, l.ljust(w - 5), curses.A_REVERSE)
            row += 1

        draw_hline(self.scr, h - 3)
        footer = " [enter] save  [esc] cancel  [ctrl+k] clear"
        safe_addstr(self.scr, h - 2, 0, footer[:w - 1], curses.A_REVERSE)
        if self.status:
            safe_addstr(self.scr, h - 1, 2, self.status[:w - 3], curses.A_DIM)

    # ---- Key handlers ----

    def _key_search(self, key):
        if key in (ord('q'), ord('Q')):
            return False
        elif key == 27:  # esc — clear query
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
            return False
        elif key in (ord('p'), ord('P')):
            if self.results:
                self.mode = "preview"
                self.status = ""
        elif key in (ord('a'), ord('A')):
            self.form = {"body": "", "keywords": ""}
            self.form_field = 0
            self.editing_rid = None
            self.status = ""
            self.mode = "add"
        elif key in (ord('e'), ord('E')):
            if self.results:
                resp, _ = self.results[self.sel]
                self.form = {"body": resp.body, "keywords": resp.keywords}
                self.form_field = 0
                self.editing_rid = resp.rid
                self.status = ""
                self.mode = "edit"
        elif key in (ord('d'), ord('D')):
            self._delete_selected()
        elif key in (ord('s'), ord('S')):
            self.sig_buf = self.config.get("signature", "")
            self.status = ""
            self.mode = "signature"
        elif 32 <= key <= 126:
            self.query += chr(key)
            self.sel = 0
            self.status = ""
            self._refresh()
        return True

    def _key_preview(self, key):
        if key in (ord('q'), ord('Q')):
            return False
        elif key in (27, ord('p'), ord('P')):
            self.mode = "search"
            self.status = ""
        elif key == ord('\n'):
            self._copy_selected()
            return False
        return True

    def _key_form(self, key):
        fields = ["body", "keywords"]
        cf = fields[self.form_field]

        if key == 27:
            self.mode = "search"
            self.status = "Cancelled."
        elif key == ord('\t'):
            self.form_field = (self.form_field + 1) % len(fields)
        elif key == ord('\n'):
            self._save_form()
        elif key in (curses.KEY_BACKSPACE, 127, 8):
            self.form[cf] = self.form[cf][:-1]
        elif 32 <= key <= 126:
            self.form[cf] += chr(key)
        return True

    def _key_signature(self, key):
        if key == 27:
            self.mode = "search"
            self.status = "Cancelled."
        elif key == ord('\n'):
            self.config["signature"] = self.sig_buf
            save_config(self.config_path, self.config)
            self.mode = "search"
            self.status = "Signature saved."
        elif key == 11:  # ctrl+k — clear
            self.sig_buf = ""
        elif key in (curses.KEY_BACKSPACE, 127, 8):
            self.sig_buf = self.sig_buf[:-1]
        elif 32 <= key <= 126:
            self.sig_buf += chr(key)
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
            self.status = f"Copied entry #{resp.rid}"
        else:
            self.status = "Clipboard copy failed (clip.exe not available here)."

    def _delete_selected(self):
        if not self.results:
            return
        resp, _ = self.results[self.sel]
        self.responses = [r for r in self.responses if r.rid != resp.rid]
        save_csv(self.csv_path, self.responses)
        self._refresh()
        self.status = f"Deleted entry #{resp.rid}."

    def _save_form(self):
        body = self.form["body"].strip()
        if not body:
            self.status = "Body is required."
            return
        keywords = self.form["keywords"].strip()

        if self.mode == "add":
            new = Response(rid=next_id(self.responses), body=body, keywords=keywords)
            self.responses.append(new)
        else:
            for r in self.responses:
                if r.rid == self.editing_rid:
                    r.body = body
                    r.keywords = keywords
                    break

        save_csv(self.csv_path, self.responses)
        self._refresh()
        self.mode = "search"
        self.status = "Saved." if self.mode == "search" else ""


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ensure_csv(DEFAULT_CSV)
    curses.wrapper(lambda s: App(s, DEFAULT_CSV, CONFIG_FILE).run())

if __name__ == "__main__":
    main()