#!/usr/bin/env python3
"""ws_yaml.py — dump a session's WebSocket traffic as a readable YAML timeline,
with raw audio + image payloads elided (collapsed to one-line summaries).

Usage: python tooling/ws_yaml.py webapp/logs/000005   → writes <dir>/ws.yaml
"""
import base64
import json
import sys
from pathlib import Path

MIC_RATE, MODEL_RATE = 16000, 24000


def _preview(s, n=200):
    s = str(s).replace("\n", "\\n")
    return s if len(s) <= n else s[:n] + f"…(+{len(s)-n})"


def secs(b64, rate):
    try:
        return len(base64.b64decode(b64)) / 2 / rate
    except Exception:
        return 0.0


def classify(dir_, obj):
    """→ dict of the event (or None to skip), plus is_audio flag + audio secs."""
    if dir_ == "out":
        if "realtime_input" in obj:
            a = obj["realtime_input"].get("audio")
            if a and "data" in a:
                return {"audio_mic": secs(a["data"], MIC_RATE)}, True
            if obj["realtime_input"].get("text") is not None:
                return {"realtime_text": _preview(obj["realtime_input"]["text"])}, False
            return {"realtime_input": "(other)"}, False
        if "client_content" in obj:
            txt = ""
            for turn in obj["client_content"].get("turns", []) or []:
                for p in turn.get("parts", []) or []:
                    if p.get("text"):
                        txt += p["text"]
                    if p.get("inline_data") or p.get("inlineData"):
                        txt += "[IMAGE]"
            return {"clientContent": _preview(txt, 300),
                    "turnComplete": obj["client_content"].get("turnComplete") or
                                    obj["client_content"].get("turn_complete")}, False
        if "tool_response" in obj:
            outs = []
            for r in obj["tool_response"].get("functionResponses", obj["tool_response"].get("function_responses", [])) or []:
                outs.append({r.get("name"): _preview(r.get("response"))})
            return {"toolResponse": outs}, False
        return {"out_other": list(obj.keys())}, False

    # incoming
    if "sessionResumptionUpdate" in obj:
        return {"resumptionUpdate": True}, False
    if "usageMetadata" in obj:
        return {"usage_tokens": obj["usageMetadata"].get("totalTokenCount")}, False
    if obj.get("toolCall"):
        calls = [{fc.get("name"): {k: _preview(v, 300) for k, v in (fc.get("args") or {}).items()}}
                 for fc in obj["toolCall"].get("functionCalls", [])]
        return {"toolCall": calls}, False
    if obj.get("goAway"):
        return {"goAway": obj["goAway"]}, False
    sc = obj.get("serverContent")
    if isinstance(sc, dict):
        if not sc:
            return {"serverContent": "(empty)"}, False
        parts = (sc.get("modelTurn") or {}).get("parts", []) or []
        au = sum(secs(p["inlineData"]["data"], MODEL_RATE)
                 for p in parts if (p.get("inlineData") or {}).get("data"))
        if au and not any(p.get("thought") or p.get("text") for p in parts):
            return {"audio_model": au}, True
        ev = {}
        for p in parts:
            if p.get("thought") and p.get("text"):
                ev.setdefault("thought", []).append(_preview(p["text"], 120))
        if sc.get("inputTranscription", {}).get("text"):
            ev["inputTranscription"] = _preview(sc["inputTranscription"]["text"])
        if sc.get("outputTranscription", {}).get("text"):
            ev["outputTranscription"] = _preview(sc["outputTranscription"]["text"])
        if sc.get("generationComplete"):
            ev["generationComplete"] = True
        if sc.get("turnComplete"):
            ev["turnComplete"] = True
        if sc.get("interrupted"):
            ev["interrupted"] = True
        return (ev or {"serverContent": "(other)"}), False
    return {"in_other": list(obj.keys())}, False


def yaml_line(t, dir_, ev):
    # compact flow mapping, one per line, unicode preserved
    items = [f"t: {t:.2f}", f"dir: {dir_}"]
    for k, v in ev.items():
        items.append(f"{k}: {json.dumps(v, ensure_ascii=False)}")
    return "- {" + ", ".join(items) + "}"


def main(session_dir):
    d = Path(session_dir)
    t0 = json.loads((d / "meta.json").read_text())["t0_mono"]
    out = ["# WebSocket timeline (audio/image elided). t = seconds from session start.", ""]
    # two INDEPENDENT audio runs so interleaved mic-out/model-in don't break each
    # other. Each: [t0, tlast, n, secs]. Flushed on any non-audio event or >1.5s gap.
    runs = {"audio_mic": None, "audio_model": None}
    pending = []   # (t, dir, key, run) to emit in time order

    def flush(key):
        r = runs[key]
        if r:
            a, b, n, s = r
            pending.append((a, "out" if key == "audio_mic" else "in", key,
                            f"{s:.1f}s of audio, {n} chunks over {b-a:.1f}s wall"))
            runs[key] = None

    def drain():
        for a, dr, k, v in sorted(pending):
            out.append(yaml_line(a, dr, {k: v}))
        pending.clear()

    for c in (d / "ws.jsonseq").read_text().split("\x1e")[1:]:
        if c.endswith("\n"):
            c = c[:-1]
        try:
            w, m, rest = c.split(" ", 2)
            dir_, frame = rest.split(" ", 1)
            obj = json.loads(frame)
        except (ValueError, json.JSONDecodeError):
            continue
        t = float(m) - t0
        ev, is_audio = classify(dir_, obj)
        if is_audio:
            key = next(iter(ev))
            r = runs[key]
            if r and (t - r[1]) < 1.5:
                r[1] = t; r[2] += 1; r[3] += ev[key]
            else:
                flush(key); runs[key] = [t, t, 1, ev[key]]
            continue
        flush("audio_mic"); flush("audio_model"); drain()
        out.append(yaml_line(t, dir_, ev))
    flush("audio_mic"); flush("audio_model"); drain()
    path = d / "ws.yaml"
    path.write_text("\n".join(out) + "\n")
    print(f"wrote {path}  ({len(out)} lines)")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: ws_yaml.py <session-dir>")
    main(sys.argv[1])
