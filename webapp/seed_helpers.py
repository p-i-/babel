"""
helpers.py — your standard library. (Seeded by the app; after that it is YOURS:
read it, extend it, restyle it, replace it. It will not be overwritten.)

Conventions:
  print(...)          → text back to you in the job receipt/report
  feed(...)           → rich things back to you (images, annotated text) — "print for things"
  show(...)           → put something on the STUDENT's stage (a deliberate act)

Environment available to your scripts:
  WORKSPACE     — your persistent home directory (also the cwd)
  FEED_FILE     — manifest path used by feed() (set per job; don't touch)
  SERVER_PORT   — the app's local HTTP port (used by helpers)
  GEMINI_API_KEY, SEARLO_API_KEY — for your own API calls
"""
import io
import json
import os
import time
import html as _html
from pathlib import Path

import requests

WORKSPACE = Path(os.environ.get("WORKSPACE", ".")).resolve()
PORT = os.environ.get("SERVER_PORT", "8642")
_BASE = f"http://127.0.0.1:{PORT}"
_UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}


# ── feed: print for things ───────────────────────────────────────────────────
def feed(*items, engage=True):
    """Send rich items back to YOUR eyes. Accepts strings (text), paths (sniffed),
    or dicts {'type':'text'|'image', 'text'|'path':..., 'caption':...}."""
    ff = os.environ.get("FEED_FILE")
    if not ff:
        print("[feed unavailable outside a job]")
        return
    with open(ff, "a") as f:
        for it in items:
            if isinstance(it, dict):
                d = dict(it)
            elif isinstance(it, (str, Path)) and Path(str(it)).suffix.lower() in (
                    ".png", ".jpg", ".jpeg", ".gif", ".webp") and Path(str(it)).exists():
                d = {"type": "image", "path": str(it)}
            else:
                d = {"type": "text", "text": str(it)}
            d.setdefault("engage", engage)
            f.write(json.dumps(d, ensure_ascii=False) + "\n")


# ── the stage: what the student sees ─────────────────────────────────────────
# THE STAGE IS ONE SURFACE: it displays exactly one page at a time, and every
# show/show_html/show_text call REPLACES whatever is on it. There is no fixed
# board widget — you DESIGN each page as HTML (see show_html).
def _post(endpoint, payload, timeout=5):
    """POST to the app; on failure raise WITH the server's reason (fail fast,
    fail informative — a bare '400 Client Error' teaches nothing)."""
    r = requests.post(f"{_BASE}{endpoint}", json=payload, timeout=timeout)
    if r.status_code != 200:
        raise RuntimeError(f"{endpoint} failed ({r.status_code}): {r.text[:300]}")
    return r.json()


def show(path):
    """Display a workspace file (html or image) on the student's stage —
    REPLACING whatever the stage was showing. Prints what happened, including
    a LOUD warning if no browser is connected (the student then sees nothing
    until one connects — it will load the current stage when it does)."""
    rel = str(Path(path).resolve().relative_to(WORKSPACE))
    d = _post("/stage", {"path": rel})
    print(f"[stage → {d['showing']}"
          + (f", replaced {d['replaced']}" if d.get("replaced") else "") + "]")
    if not d.get("browser", True):
        print("[⚠ NO BROWSER CONNECTED — the student currently sees NOTHING; "
              "it will appear when their browser connects]")


def show_html(html, name="current"):
    """Put a page you DESIGN on the student's stage (REPLACES it — one surface).
    THIS IS YOUR MAIN CREATIVE SURFACE — a blank canvas, not a fixed widget.
    Compose whatever the moment needs, fresh for THIS student: a welcome card, a
    vocab table, an end-of-lesson recap, a matching game, a big-emoji picture grid.
    Ordinary HTML/CSS. Your page automatically gets two JS helpers:
      speak(text, lang)        — the student TAPS a word to hear it (warm() it first)
      feed(payload, {engage})  — send events from the page back to your eyes
    Make a word click-to-hear like:
      <button onclick="speak('кіт','Ukrainian')">кіт 🔊</button>
    Keep it legible on the dark stage (light text, generous size, nothing clipped,
    overlapping, or dark-on-dark), then peek() and fix what looks wrong."""
    d = WORKSPACE / ".stage"
    d.mkdir(exist_ok=True)
    p = d / f"{name}.html"
    p.write_text(html)
    show(p)
    return p


def show_text(text, note=""):
    """Big text card on the stage (unspoken — for reading exercises).
    REPLACES the current stage page — never use it to point at the board!"""
    body = _html.escape(text).replace("\n", "<br>")
    n = f'<div class="note">{_html.escape(note)}</div>' if note else ""
    show_html(f"""<!doctype html><html><head><meta charset="utf-8"><style>
body{{margin:0;height:100vh;display:flex;align-items:center;justify-content:center;
background:#111;color:#e8e8ea;font:15px -apple-system,Helvetica,sans-serif}}
.card{{max-width:70%;background:#1a1a1e;border:1px solid #2e2e36;border-radius:16px;
padding:36px 44px;font-size:30px;line-height:1.5;text-align:center}}
.note{{font-size:14px;color:#9a9aa2;margin-top:14px}}</style></head>
<body><div class="card">{body}{n}</div></body></html>""", name="text")


def warm(words, lang=None):
    """Pre-generate the click-to-hear clips for these target words NOW, so the
    student's first tap plays instantly instead of a ~2s lazy generation. Call it
    alongside a show_html page whose elements have onclick="speak('слово','Ukrainian')".
    words: the list of target strings on the page; lang: the target language
    (pronounces homographs right AND keys the clip cache). Fire-and-forget."""
    words = [w for w in (words or []) if isinstance(w, str) and w.strip()]
    if not words:
        return
    try:
        _post("/warm", {"words": words, "lang": lang}, timeout=3)
        print(f"[warming {len(words)} clip(s)]")
    except Exception as e:
        print(f"[warm skipped: {e}]")


# ── the keyboard: colorable keys under the stage ─────────────────────────────
def set_keys(keys, color):
    """Color keys on the student's on-screen keyboard (always visible below the
    stage). keys: a string of characters, e.g. 'йцу' — case-insensitive (folded
    to the lowercase key glyphs). color: any CSS color ('#8fd18f', 'yellow');
    color=None restores keys to the default look. RAISES loudly if a character
    isn't on the keyboard (with the valid glyph list) — nothing was painted then.
    Prints the FULL color map now in force: that map IS what the student sees.
    The colors mean NOTHING to the widget — you own the color language; keep it
    consistent and tell the student what your colors mean. Colors reset when the
    app restarts: re-paint from your notes at session start."""
    d = _post("/keyboard", {"keys": "".join(keys), "color": color})
    now = " ".join(f"{k}={v}" for k, v in sorted(d["colors"].items())) or "(none)"
    print(f"[keyboard now: {now}]")
    if not d.get("browser", True):
        print("[⚠ NO BROWSER CONNECTED — the student cannot see the keyboard yet]")


def clear_keys():
    """Reset ALL keyboard keys to the default look."""
    _post("/keyboard", {"clear": True})
    print("[keyboard colors cleared]")


def flash_keys(keys, color="#7c9cff", seconds=2.0):
    """Momentarily light keyboard keys up, decaying back to their current color
    over `seconds` — attention direction while you speak ("this one!"), without
    disturbing your persistent set_keys color scheme. keys: string of characters
    (case-insensitive). Raises loudly on unknown keys."""
    d = _post("/keyboard", {"flash": "".join(keys), "color": color,
                            "seconds": seconds})
    print(f"[flashed {len(set(''.join(keys).lower()))} key(s)]"
          + ("" if d.get("browser", True) else
             " [⚠ NO BROWSER CONNECTED — nobody saw it]"))


def set_layout(lang):
    """Switch the on-screen keyboard to a different TARGET-language layout.
    Available: de, el (Greek), en, es, fr, it, ru, tr, uk. Raises with that list
    on an unknown code (RTL/AltGr/CJK layouts don't exist yet). Switching to a
    DIFFERENT alphabet clears all key colors (the old glyphs are gone) — re-paint
    for the new one. Calling it for the layout you are ALREADY on is a no-op that
    KEEPS your colors, so it is safe to call defensively. The small bottom-left
    labels (the student's physical keycaps) persist across switches."""
    d = _post("/keyboard", {"layout": lang})
    if "colors cleared" in str(d.get("note", "")):
        print(f"[keyboard layout → {d['layout']}; colors cleared]")
    else:
        print(f"[keyboard already {d['layout']}; colors preserved]")


# ── voice & sight ────────────────────────────────────────────────────────────
def speak(text, lang=None, voice=None):
    """The student hears target words by TAPPING them on screen — this is the core
    audio primitive, and it lives IN THE PAGE, not here. In any HTML you show_html,
    make a word click-to-hear:
        <button onclick="speak('кіт','Ukrainian')">кіт 🔊</button>
    That speak() runs in the student's BROWSER — echo-cancelled, on THEIR tap, no
    collision with your live voice. Always pass the language ('Ukrainian', 'French':
    it disambiguates cross-language homographs). warm([...], lang) the page's words
    so the first tap is instant. For an auditory-recall drill, stage a page with a
    single play button that calls speak(word, lang); the student taps to hear.
    (Calling speak() here in run_python is a mistake — it would play through the
    backend and collide with your live voice, badly mistimed (field: the 'сон'
    incident); that's why it raises. Put it in the page instead.)"""
    raise RuntimeError(
        "speak() can't be called directly from run_python (it collides with your live "
        "voice). Make the word click-to-hear IN THE PAGE instead: in staged HTML use "
        "onclick=\"speak('word','Language')\", and warm([...], lang) the words first.")


def run_js(code, timeout=5):
    """Run JS inside the current stage page; returns its value (use 'return ...').
    Your escape hatch for interrogating or patching the live page. To check what
    the student is ACTUALLY seeing: run_js("return document.body.innerText.slice(0,300)")."""
    r = requests.post(f"{_BASE}/js", json={"code": code, "timeout": timeout}, timeout=timeout + 2)
    d = r.json()
    if not d.get("ok"):
        raise RuntimeError(d.get("error", "run_js failed"))
    return d.get("result")


def peek(note=""):
    """SEE THE STUDENT'S SCREEN. Takes a screenshot of exactly what the student is
    looking at right now — your staged page, the live keyboard colors, even a tab
    outside this app — and feeds it back to your eyes as a follow-up image message
    (it arrives a moment after this returns; it is NOT in the receipt). Use it to
    VERIFY that what you staged actually rendered, that key colors took, or to see
    what the student is doing. `note` rides along as the image caption (say what you
    are checking). Raises if capture failed (e.g. Screen-Recording permission not
    granted) — then you are blind and must not pretend otherwise."""
    d = _post("/peek", {"note": note})
    print(f"[screen snapshot queued → your eyes]" + (f" — {note}" if note else ""))
    return d


# ── web images: the exp-06 pipeline ──────────────────────────────────────────
def search_images(query, limit=10, gl=None, hl=None):
    """Searlo image search (1 credit/call, max 10 results).
    RULE: for target-language material, WRITE THE QUERY IN THE TARGET LANGUAGE
    (and set gl/hl, e.g. gl='ua', hl='uk'). English queries return the anglophone web."""
    params = {"q": query, "limit": limit}
    if gl: params["gl"] = gl
    if hl: params["hl"] = hl
    r = requests.get("https://api.searlo.tech/api/v1/search/images", params=params,
                     headers={"x-api-key": os.environ["SEARLO_API_KEY"]}, timeout=20)
    return r.json().get("images", [])


def download(url, path):
    """Download a URL to a workspace path (browser UA; some hosts block — catch failures)."""
    p = WORKSPACE / path
    p.parent.mkdir(parents=True, exist_ok=True)
    r = requests.get(url, headers=_UA, timeout=20)
    r.raise_for_status()
    p.write_bytes(r.content)
    return p


def contact_sheet(image_urls, out="sheet.jpg", cols=4, cell=290):
    """Download images and composite a NUMBERED grid. Returns (sheet_path, kept_urls) —
    kept_urls[i] is cell i+1 (failed downloads are skipped, numbering stays aligned)."""
    from PIL import Image, ImageDraw, ImageFont
    cells, kept = [], []
    for u in image_urls:
        try:
            r = requests.get(u, headers=_UA, timeout=15)
            r.raise_for_status()
            cells.append(Image.open(io.BytesIO(r.content)).convert("RGB"))
            kept.append(u)
        except Exception:
            continue
    if not cells:
        raise RuntimeError("no images downloadable")
    rows = (len(cells) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * cell, rows * cell), "#111111")
    font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 40)
    for i, im in enumerate(cells):
        im.thumbnail((cell - 8, cell - 8))
        x, y = (i % cols) * cell, (i // cols) * cell
        sheet.paste(im, (x + (cell - im.width) // 2, y + (cell - im.height) // 2))
        dr = ImageDraw.Draw(sheet)
        dr.rectangle([x + 4, y + 4, x + 58, y + 52], fill="#7c9cff")
        dr.text((x + 16, y + 6), str(i + 1), fill="black", font=font)
    p = WORKSPACE / out
    sheet.save(p, quality=88)
    return p, kept


def vision_pick(sheet_path, brief):
    """Ask a vision model to pick the best cell of a contact sheet for your brief.
    Returns dict like {'pick': N or 0, 'reason': ..., 'better_query': ...}.
    pick=0 means NOTHING qualifies — re-search with better terms (that is a valid,
    common outcome; don't force a bad pick)."""
    from google import genai as _genai
    from google.genai import types as _t
    client = _genai.Client()
    r = client.models.generate_content(
        model="gemini-3.1-flash-lite",
        contents=[_t.Part.from_bytes(data=Path(sheet_path).read_bytes(), mime_type="image/jpeg"),
                  _t.Part(text=f"{brief}\nThe grid cells are numbered. If NO cell qualifies, "
                               f"say pick=0 and suggest a better query. Reply as pure JSON: "
                               f'{{"pick": N, "reason": "...", "better_query": "..."}}')])
    txt = r.text.strip()
    if "```" in txt:
        txt = txt.split("```")[1].removeprefix("json").strip()
    return json.loads(txt)
def update_board_and_keyboard(html, keys=None, snapshot=True, note="board + keyboard"):
    """Change the stage AND repaint the keyboard in ONE call, then auto-snapshot — so the
    board and the keyboard colours never drift out of sync (recurring bug: updating the board
    and forgetting to recolour the keyboard). PREFER this over calling show_html + set_keys
    separately whenever a board change should be reflected on the keyboard.
      html:  the stage page (same as show_html).
      keys:  {css_color: "characters", ...} = the FULL keyboard state from your notes, e.g.
             {'#4caf50': 'аоуеі', '#e6c05a': 'пр'} (mastered green, in-progress yellow).
             Characters you don't list are reset to default. keys=None leaves it untouched.
      snapshot: auto-peek after (default True); the screenshot reaches your eyes shortly."""
    p = show_html(html)
    if keys is not None:
        clear_keys()
        for color, chars in keys.items():
            if chars:
                set_keys(chars, color)
    if snapshot:
        peek(note)
    return p
