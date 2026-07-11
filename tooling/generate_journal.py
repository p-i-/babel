#!/usr/bin/env python3
"""generate_journal.py — render a raw session capture into a readable timeline.

Reads a session folder (logs/NNNNNN/{meta.json, ws.jsonseq, log.jsonseq}) and
prints a chronological timeline on ONE clock: the log narrative + the audio
firehose collapsed into run summaries. Doctrine: the raw capture is the source of
truth; this is a disposable, evolvable VIEW over it.

Principles it holds to:
  * Elide bloat — mic + model PCM (base64) become "N pkts → X.Xs" summaries.
  * Never paper over a gap — an audio run breaks on any inter-arrival gap
    > GAP_S, and the gap is printed as its own line (the mid-turn stall shows up).
  * Never lose vital info — every log line passes through (only HTTP-access-log
    runs are collapsed, with a count); WARN/ERROR are marked; and a coverage
    footer accounts for EVERY ws frame type, so nothing is silently dropped.

Usage: python tooling/generate_journal.py webapp/logs/000001
"""
import base64
import collections
import json
import re
import sys
import time
from pathlib import Path

GAP_S = 1.5          # audio inter-arrival gap above this splits a run (collapse granularity)
STALL_MIN = 8.0      # a mid-turn model-audio gap must exceed this to count as a STALL —
                     # below it is normal think/pacing (field 2026-07-10: 1.5s over-flagged
                     # tool pauses + thinking). Tool-call gaps are excluded regardless.
DEAD_S = 8.0         # you spoke + no MEANINGFUL model frame for this long = a DEAD TURN
                     # (the live watchdog auto-kicks here). resumptionUpdate/usage/empty
                     # serverContent do NOT count as activity — they mask a stuck engine.
MIC_RATE, MODEL_RATE = 16000, 24000
_HTTP = re.compile(r'\] "(GET|POST|PUT|DELETE) ')


def parse_seq(path):
    """RS-framed → [(wall, mono, payload)], payload verbatim."""
    if not path.exists():
        return []
    recs = []
    for c in path.read_text().split("\x1e")[1:]:
        if c.endswith("\n"):
            c = c[:-1]
        try:
            w, m, rest = c.split(" ", 2)
            recs.append((float(w), float(m), rest))
        except ValueError:
            pass
    return recs


def audio_secs(b64, rate):
    try:
        return len(base64.b64decode(b64)) / 2 / rate
    except Exception:
        return 0.0


_SNAP_CAP = re.compile(r"\[screen snapshot — (.+?)\]")


def _b64(s):
    pad = "=" * (-len(s) % 4)
    try:
        return base64.urlsafe_b64decode(s + pad)   # the genai SDK uses url-safe base64
    except Exception:
        return base64.b64decode(s + pad)


def extract_images(obj, out_dir, n):
    """Write image parts of an outgoing client_content frame into the session
    folder as snap_NN.png/jpg → [(filename, caption), …]. The bytes are already
    byte-exact in ws.jsonseq; this just puts them one click from the report so a
    future agent never has to decode. (Screenshots delivered to the model; also
    catches look_at artifacts.)"""
    cc = obj.get("client_content") or {}
    text, blobs = "", []
    for turn in cc.get("turns", []) or []:
        for p in turn.get("parts", []) or []:
            if p.get("text"):
                text = p["text"]
            idl = p.get("inline_data") or p.get("inlineData")
            mime = str((idl or {}).get("mime_type") or (idl or {}).get("mimeType") or "")
            if idl and idl.get("data") and mime.startswith("image"):
                blobs.append((idl["data"], "jpg" if "jpeg" in mime else "png"))
    cap = _SNAP_CAP.search(text)
    caption = cap.group(1) if cap else (text.strip().splitlines() or [""])[0][:70]
    out = []
    for data, ext in blobs:
        fname = f"snap_{n + len(out):02d}.{ext}"
        try:
            (out_dir / fname).write_bytes(_b64(data))
            out.append((fname, caption))
        except Exception:
            pass
    return out


def classify_ws(payload):
    """→ (kind, seconds, obj). kind ∈ {mic, model, other}. `seconds` for audio."""
    dir_, frame = payload.split(" ", 1)
    obj = json.loads(frame)
    if dir_ == "out" and "realtime_input" in obj:
        a = obj["realtime_input"].get("audio")
        if a and "data" in a:
            return "mic", audio_secs(a["data"], MIC_RATE), obj
    sc = obj.get("serverContent")
    if isinstance(sc, dict):
        secs = 0.0
        for p in (sc.get("modelTurn") or {}).get("parts", []) or []:
            idl = p.get("inlineData") or p.get("inline_data")
            if idl and idl.get("data"):
                secs += audio_secs(idl["data"], MODEL_RATE)
        # audio-only serverContent (no transcript/thought/turn signal) → collapse
        if secs and not any(k in sc for k in
                            ("inputTranscription", "outputTranscription",
                             "turnComplete", "interrupted", "generationComplete")):
            has_text = any(p.get("text") for p in
                           (sc.get("modelTurn") or {}).get("parts", []) or [])
            if not has_text:
                return "model", secs, obj
    return "other", 0.0, obj


def main(session_dir):
    d = Path(session_dir)
    meta = json.loads((d / "meta.json").read_text())
    t0 = meta["t0_mono"]
    ws = parse_seq(d / "ws.jsonseq")
    lg = parse_seq(d / "log.jsonseq")

    # turn boundaries: a model-audio gap WITH a turnComplete inside it is a normal
    # wait (the model finished, ball's in the student's court); a gap with NO
    # turnComplete is the model going silent MID-TURN — the real stall. (Field
    # 2026-07-10: without this the tool cries wolf on every between-turn pause.)
    turn_completes = []
    tool_calls = []
    for _w, m, payload in ws:
        if payload.startswith("in "):
            try:
                o = json.loads(payload[3:])
            except Exception:
                continue
            if o.get("toolCall"):
                tool_calls.append(m)
            sc = o.get("serverContent")
            if isinstance(sc, dict) and sc.get("turnComplete"):
                turn_completes.append(m)
    def tc_in(a, b):
        return any(a < t <= b for t in turn_completes)
    def tool_in(a, b):
        return any(a - 0.3 <= t <= b for t in tool_calls)   # a tool executing in the gap

    # ── DEAD-TURN detector (mirrors the live is-dead watchdog exactly) ───────────
    # You spoke, and the model produced NO real output for > DEAD_S. The clock resets on
    # any meaningful frame (inputTranscription / model audio-text-thought / outputTranscription
    # / toolCall / turnComplete / interrupted). NOT resumptionUpdate/usage/empty.
    # owes = student spoke; it CLEARS only on a REAL reply (audio / non-thought text /
    # outputTranscription / toolCall) — NOT on turnComplete, because an interrupted turn
    # ends "interrupted → turnComplete" with no output and would falsely clear owes,
    # hiding the dead turn (field 000006/000007 — same bug the live code had).
    dead_turns = []
    t_last_act, owes = None, False
    for _w, m, payload in ws:
        if not payload.startswith("in "):
            continue
        try:
            o = json.loads(payload[3:])
        except Exception:
            continue
        sc = o.get("serverContent") if isinstance(o.get("serverContent"), dict) else None
        is_input = bool(sc and sc.get("inputTranscription", {}).get("text"))
        is_tc = bool(sc and sc.get("turnComplete"))
        is_intr = bool(sc and sc.get("interrupted"))
        parts = (sc.get("modelTurn") or {}).get("parts", []) if sc else []
        model_produced = bool(o.get("toolCall")) or bool(sc and (
            sc.get("outputTranscription") or
            any(p.get("inlineData") or (p.get("text") and not p.get("thought"))
                for p in parts)))
        thinking = bool(sc and any(p.get("thought") for p in parts))
        resets = is_input or is_tc or is_intr or model_produced or thinking
        if resets and owes and t_last_act is not None and (m - t_last_act) > DEAD_S:
            dead_turns.append((t_last_act, m - t_last_act))
        if is_input:
            owes = True
        if model_produced:
            owes = False
        if resets:
            t_last_act = m

    events = []          # (mono, text) — the merged timeline
    def emit(mono, text):
        events.append((mono, text))

    # ── log narrative (collapse HTTP-access-log runs; mark WARN/ERROR) ──────────
    http_run = []
    def flush_http():
        if http_run:
            emit(http_run[0], f"· {len(http_run)} HTTP requests")
            http_run.clear()
    for _w, m, payload in lg:
        o = json.loads(payload)
        msg, lvl = o.get("msg", ""), o.get("lvl", "INFO")
        if _HTTP.search(msg):
            http_run.append(m)
            continue
        flush_http()
        mark = {"WARNING": "⚠️  ", "ERROR": "❌ ", "CRITICAL": "❌ "}.get(lvl, "")
        emit(m, f"{mark}{msg}")
    flush_http()

    # ── ws: collapse audio into runs, split on gaps, tally coverage ─────────────
    kinds = collections.Counter()
    runs = {"mic": None, "model": None}    # open run per stream
    stalls = []                            # (mono, seconds) mid-turn model silences
    shots = []                             # (filename, caption) images pulled into the folder
    tallies = {"mic": [0, 0.0], "model": [0, 0.0]}   # [pkts, secs] totals

    def flush_run(stream):
        r = runs[stream]
        if not r:
            return
        arrow = "→ mic" if stream == "mic" else "← model audio ▶"
        wall = r["last"] - r["start"]
        note = "" if stream == "mic" else f" (over {wall:.1f}s wall)"
        emit(r["start"], f"{arrow} {r['secs']:.1f}s  [{r['pkts']} pkts{note}]")
        runs[stream] = None

    for _w, m, payload in ws:
        try:
            kind, secs, obj = classify_ws(payload)
        except Exception:
            kinds["<parse-error>"] += 1
            continue
        kinds[_topkey(obj, payload)] += 1
        if payload.startswith("out "):        # extract any images → session folder
            for fname, cap in extract_images(obj, d, len(shots)):
                emit(m, f"📸 {fname} — {cap}")
                shots.append((fname, cap))
        if kind in ("mic", "model"):
            tallies[kind][0] += 1
            tallies[kind][1] += secs
            r = runs[kind]
            if r and (m - r["last"]) <= GAP_S:
                r["pkts"] += 1; r["secs"] += secs; r["last"] = m
            else:
                if r:
                    prev_end = r["last"]
                    gap = m - prev_end
                    flush_run(kind)
                    if kind == "model" and gap >= GAP_S:   # a model-audio discontinuity
                        if tc_in(prev_end, m):             # turn ended in the gap → normal
                            emit(prev_end, f"— {gap:.1f}s quiet (turn done, awaiting student)")
                        elif tool_in(prev_end, m):         # a tool ran in the gap → not a stall
                            emit(prev_end, f"— {gap:.1f}s pause (tool executing)")
                        elif gap >= STALL_MIN:             # mid-turn silence beyond think/pace
                            stalls.append((prev_end, gap))
                            emit(prev_end, f"⚠️  STALL {gap:.1f}s — model went silent "
                                           f"MID-TURN (no turnComplete)")
                        # else: sub-8s gap = normal thinking/pacing — not flagged
                runs[kind] = {"start": m, "last": m, "pkts": 1, "secs": secs}
    for s in ("mic", "model"):
        flush_run(s)

    for t, g in dead_turns:
        emit(t, f"💀 DEAD TURN {g:.1f}s — you spoke, model produced NOTHING "
                f"(live watchdog would auto-kick at {DEAD_S:.0f}s)")

    # ── render: header + merged timeline + footer ───────────────────────────────
    events.sort(key=lambda e: e[0])
    started = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(meta["t0_wall"]))
    out = [
        f"═══ session {meta['session']} · {started} · {meta['model']} ═══",
        f"temp={meta.get('temp')}  think={meta.get('think')}  "
        f"voice={meta.get('voice')}  langs={','.join(meta.get('langs', []))}"
        f"   (full config → meta.json)",
        "",
    ]
    if dead_turns:
        out.append(f"💀 {len(dead_turns)} DEAD TURN(S) — you spoke, model went silent: " +
                   ", ".join(f"{g:.0f}s @ {t - t0:.0f}s" for t, g in dead_turns))
    if stalls:
        out.append(f"⚠️  {len(stalls)} MID-TURN STALL(S): " +
                   ", ".join(f"{g:.1f}s @ {m - t0:.0f}s" for m, g in stalls))
    if dead_turns or stalls:
        out.append("")
    out += [f"[{m - t0:7.2f}] {text}" for m, text in events]
    out += [
        "",
        f"── audio: model {tallies['model'][1]:.1f}s in speech "
        f"({tallies['model'][0]} pkts) · mic {tallies['mic'][1]:.1f}s "
        f"({tallies['mic'][0]} pkts) · {len(stalls)} stall(s) · "
        f"{len(dead_turns)} dead-turn(s) · "
        f"{len(shots)} screenshot(s) → snap_*.png ──",
        f"── ws frames seen: " +
        ", ".join(f"{k}={v}" for k, v in kinds.most_common()) + " ──",
    ]
    report = "\n".join(out)
    print(report)
    try:
        (d / "report.txt").write_text(report + "\n")
    except OSError:
        pass


def _topkey(obj, payload):
    dir_ = payload.split(" ", 1)[0]
    for k in obj:
        return f"{dir_}:{k}"
    return f"{dir_}:?"


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: generate_journal.py <session-dir>")
    main(sys.argv[1])
