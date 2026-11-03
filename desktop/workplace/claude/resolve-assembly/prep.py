#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
prep.py — 컷 리스트(JSON) → ffmpeg 전처리 → Resolve 조립용 resolved JSON

  python3 prep.py sample/cutlist.json             # 전처리 + resolved JSON
  python3 prep.py sample/cutlist.json --preview   # + ffmpeg 단독 조립본(품질 비교용)
  python3 prep.py sample/cutlist.json --force     # 캐시 무시하고 전부 다시 렌더

하는 일
  1) 씬별 클립 생성 (ProRes 422 HQ, bt709 태그)
     - 이미지: 이징 줌/팬 (고해상도 업스케일 후 zoompan → 떨림 최소화)
     - 영상: 프레임레이트/해상도 통일, 길이 맞춤(부족하면 마지막 프레임 유지)
  2) 오디오
     - 음성: 씬 위치에 배치 → 2-pass loudnorm (기본 -15 LUFS)
     - BGM: 반복/길이 맞춤, 페이드, 음성 기준 사이드체인 더킹
  3) 자막 SRT 자동 생성 (음성 길이 기준, 글자수로 분할)
  4) build/cutlist.resolved.json + ~/.resolve-assembly/current.json(포인터) 기록
     → Resolve 의 Workspace > Scripts > resolve_assemble 이 이 파일을 읽어 조립/렌더
"""
import argparse
import hashlib
import json
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff", ".bmp"}
VIDEO_EXT = {".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi"}
POINTER = Path.home() / ".resolve-assembly" / "current.json"

VERBOSE = False
FF = ["ffmpeg", "-hide_banner", "-nostdin", "-y", "-loglevel", "error"]
ENC_V = [
    "-c:v", "prores_ks", "-profile:v", "3", "-vendor", "apl0",
    "-pix_fmt", "yuv422p10le",
    "-colorspace", "bt709", "-color_primaries", "bt709",
    "-color_trc", "bt709", "-color_range", "tv",
]
TO_709 = "setsar=1,scale=out_color_matrix=bt709:out_range=tv,format=yuv422p10le"


# ───────────────────────── 유틸 ─────────────────────────
def log(msg):
    print(msg, flush=True)


def run(cmd, cwd=None):
    if VERBOSE:
        log("  $ " + shlex.join(str(c) for c in cmd))
    r = subprocess.run([str(c) for c in cmd], stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE, text=True, cwd=cwd)
    if r.returncode != 0:
        sys.exit("\n[오류] 명령 실패:\n  " + shlex.join(str(c) for c in cmd)
                 + "\n" + r.stderr[-3000:])
    return r


def probe_duration(path):
    r = run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", path])
    try:
        return float(r.stdout.strip())
    except ValueError:
        sys.exit(f"[오류] 길이를 읽을 수 없음: {path}")


def has_filter(name):
    r = subprocess.run(["ffmpeg", "-hide_banner", "-filters"],
                       stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    return re.search(rf"^\s*\S+\s+{re.escape(name)}\s", r.stdout, re.M) is not None


def resolve_path(base, p):
    if p is None:
        return None
    q = Path(p).expanduser()
    q = q if q.is_absolute() else (base / q)
    q = q.resolve()
    if not q.exists():
        sys.exit(f"[오류] 파일 없음: {q}")
    return q


def cached(out, sig):
    """같은 파라미터로 이미 만든 결과가 있으면 True."""
    meta = out.with_suffix(out.suffix + ".sig")
    return out.exists() and meta.exists() and meta.read_text() == sig


def mark(out, sig):
    out.with_suffix(out.suffix + ".sig").write_text(sig)


def signature(*parts):
    h = hashlib.sha1()
    for p in parts:
        if isinstance(p, Path) and p.exists():
            st = p.stat()
            p = f"{p}|{st.st_size}|{st.st_mtime_ns}"
        h.update(json.dumps(p, sort_keys=True, default=str).encode())
    return h.hexdigest()


# ───────────────────────── 계획 ─────────────────────────
def plan(cfg, base):
    fps = int(cfg.get("fps", 24))
    pad = float(cfg.get("scene_padding", 0.4))
    default_offset = float(cfg.get("voice_offset", 0.15))
    default_still = float(cfg.get("default_still_duration", 3.0))

    scenes, cursor = [], 0
    for i, s in enumerate(cfg["scenes"]):
        sid = s.get("id") or f"s{i + 1:02d}"
        media = resolve_path(base, s["media"])
        ext = media.suffix.lower()
        if ext in IMAGE_EXT:
            kind = "image"
        elif ext in VIDEO_EXT:
            kind = "video"
        else:
            sys.exit(f"[오류] 지원하지 않는 미디어 형식: {media}")

        voice = resolve_path(base, s.get("voice"))
        voice_dur = probe_duration(voice) if voice else 0.0
        offset = float(s.get("voice_offset", default_offset))
        in_sec = float(s.get("in", 0.0))

        if "duration" in s:
            dur = float(s["duration"])
        elif voice:
            dur = offset + voice_dur + pad
        elif kind == "video":
            dur = max(0.1, probe_duration(media) - in_sec)
        else:
            dur = default_still

        frames = max(1, round(dur * fps))
        scenes.append({
            "id": sid, "kind": kind, "media": media,
            "voice": voice, "voice_dur": voice_dur, "voice_offset": offset,
            "text": s.get("text", ""), "motion": s.get("motion") or {},
            "in": in_sec, "frames": frames, "start": cursor,
        })
        cursor += frames
    return scenes, cursor


# ───────────────────────── 비디오 ─────────────────────────
def ease_expr(p, ease):
    return {
        "linear": p,
        "smooth": f"({p}*{p}*(3-2*{p}))",          # ease-in-out (smoothstep)
        "in": f"({p}*{p})",
        "out": f"(1-(1-{p})*(1-{p}))",
    }.get(ease, f"({p}*{p}*(3-2*{p}))")


def zoompan(motion, n, w, h, fps):
    mtype = motion.get("type", "zoom_in")
    a = float(motion.get("amount", 0.12))
    e = ease_expr(f"(on/{max(n - 1, 1)})", motion.get("ease", "smooth"))
    cx, cy = "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"
    panx, pany = "(iw-iw/zoom)", "(ih-ih/zoom)"
    table = {
        "zoom_in":   (f"1+{a}*{e}",     cx, cy),
        "zoom_out":  (f"1+{a}*(1-{e})", cx, cy),
        "pan_left":  (f"{1 + a}", f"{panx}*(1-{e})", cy),
        "pan_right": (f"{1 + a}", f"{panx}*{e}", cy),
        "pan_up":    (f"{1 + a}", cx, f"{pany}*(1-{e})"),
        "pan_down":  (f"{1 + a}", cx, f"{pany}*{e}"),
    }
    if mtype not in table:
        sys.exit(f"[오류] 알 수 없는 motion.type: {mtype} (가능: {', '.join(table)}, none)")
    z, x, y = table[mtype]
    return f"zoompan=z='{z}':x='{x}':y='{y}':d={n}:s={w}x{h}:fps={fps}"


def render_clip(sc, cfg, clips_dir, force):
    fps, w, h = int(cfg.get("fps", 24)), int(cfg["width"]), int(cfg["height"])
    up = int(cfg.get("motion_upscale", 3))
    n = sc["frames"]
    out = clips_dir / f"{sc['id']}.mov"
    sig = signature(sc["media"], sc["kind"], sc["motion"], sc["in"], n, fps, w, h, up, 2)
    if not force and cached(out, sig):
        log(f"  · {sc['id']}: 캐시 사용")
        return out

    if sc["kind"] == "video":
        dur = n / fps
        vf = (f"fps={fps},scale={w}:{h}:force_original_aspect_ratio=increase:flags=lanczos,"
              f"crop={w}:{h},tpad=stop_mode=clone:stop_duration={dur:.3f},{TO_709}")
        cmd = FF + ["-ss", f"{sc['in']:.3f}", "-i", sc["media"], "-vf", vf,
                    "-frames:v", str(n), "-an"] + ENC_V + [out]
    else:
        mtype = sc["motion"].get("type", "zoom_in") if sc["motion"] else "none"
        if mtype == "none":
            vf = (f"scale={w}:{h}:force_original_aspect_ratio=increase:flags=lanczos,"
                  f"crop={w}:{h},{TO_709}")
            cmd = FF + ["-loop", "1", "-framerate", str(fps), "-i", sc["media"],
                        "-vf", vf, "-frames:v", str(n)] + ENC_V + [out]
        else:
            W, H = w * up, h * up
            vf = (f"scale={W}:{H}:force_original_aspect_ratio=increase:flags=lanczos,"
                  f"crop={W}:{H},{zoompan(sc['motion'], n, w, h, fps)},{TO_709}")
            cmd = FF + ["-i", sc["media"], "-vf", vf, "-frames:v", str(n)] + ENC_V + [out]

    log(f"  · {sc['id']}: {sc['kind']} {n}f 렌더")
    run(cmd)
    mark(out, sig)
    return out


# ───────────────────────── 오디오 ─────────────────────────
AFMT = "aformat=sample_rates=48000:channel_layouts=stereo"


def loudnorm(src, dst, target, total_sec):
    tp, lra = -1.5, 11
    r = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostdin", "-i", str(src), "-af",
         f"loudnorm=I={target}:TP={tp}:LRA={lra}:print_format=json", "-f", "null", "-"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    blocks = re.findall(r"\{[^{}]*\}", r.stderr, re.S)
    m = json.loads(blocks[-1]) if blocks else {}
    if not m or "inf" in str(m.get("input_i", "")):
        log("  · 음성 라우드니스 측정 불가(무음?) → 정규화 생략")
        shutil.copyfile(src, dst)
        return
    af = (f"loudnorm=I={target}:TP={tp}:LRA={lra}:measured_I={m['input_i']}:"
          f"measured_TP={m['input_tp']}:measured_LRA={m['input_lra']}:"
          f"measured_thresh={m['input_thresh']}:offset={m['target_offset']}:linear=true,"
          f"aresample=48000,apad,atrim=0:{total_sec:.6f}")
    run(FF + ["-i", src, "-af", af, "-ar", "48000", "-c:a", "pcm_s24le", dst])
    log(f"  · 음성 라우드니스 {m['input_i']} → {target} LUFS")


def build_voice(scenes, fps, total_sec, cfg, build, force):
    voiced = [s for s in scenes if s["voice"]]
    if not voiced:
        return None
    target = float(cfg.get("audio", {}).get("voice_lufs", -15))
    out = build / "voice_mix.wav"
    sig = signature([(s["voice"], s["start"], s["voice_offset"]) for s in voiced],
                    fps, total_sec, target)
    if not force and cached(out, sig):
        log("  · 음성: 캐시 사용")
        return out

    cmd, fc, labels = list(FF), [], []
    for k, s in enumerate(voiced):
        cmd += ["-i", s["voice"]]
        ms = int(round((s["start"] / fps + s["voice_offset"]) * 1000))
        fc.append(f"[{k}:a]{AFMT},adelay={ms}|{ms}[v{k}]")
        labels.append(f"[v{k}]")
    if len(labels) > 1:
        fc.append(f"{''.join(labels)}amix=inputs={len(labels)}:duration=longest:normalize=0[m]")
        last = "[m]"
    else:
        last = labels[0]
    fc.append(f"{last}apad,atrim=0:{total_sec:.6f}[out]")
    raw = build / "voice_raw.wav"
    run(cmd + ["-filter_complex", ";".join(fc), "-map", "[out]",
               "-ar", "48000", "-c:a", "pcm_s24le", raw])
    loudnorm(raw, out, target, total_sec)
    mark(out, sig)
    return out


def build_bgm(cfg, base, total_sec, voice, build, force):
    a = cfg.get("audio", {})
    bgm = resolve_path(base, a.get("bgm"))
    if not bgm:
        return None
    gain = float(a.get("bgm_gain_db", -14))
    fi, fo = float(a.get("bgm_fade_in", 1.0)), float(a.get("bgm_fade_out", 2.0))
    duck = a.get("duck", True) and voice is not None
    d = a.get("duck_settings", {})
    thr, ratio = float(d.get("threshold", 0.02)), float(d.get("ratio", 8))
    att, rel = float(d.get("attack", 20)), float(d.get("release", 400))

    out = build / "bgm_ducked.wav"
    sig = signature(bgm, gain, fi, fo, duck, thr, ratio, att, rel, total_sec,
                    voice if voice else None)
    if not force and cached(out, sig):
        log("  · BGM: 캐시 사용")
        return out

    T = f"{total_sec:.6f}"
    chain = (f"[0:a]{AFMT},atrim=0:{T},asetpts=PTS-STARTPTS,volume={gain}dB,"
             f"afade=t=in:d={fi},afade=t=out:st={max(0.0, total_sec - fo):.3f}:d={fo}")
    cmd = FF + ["-stream_loop", "-1", "-i", bgm]
    if duck:
        cmd += ["-i", voice]
        fc = (f"{chain}[b];[1:a]{AFMT}[sc];"
              f"[b][sc]sidechaincompress=threshold={thr}:ratio={ratio}:attack={att}:release={rel}[d];"
              f"[d]apad,atrim=0:{T}[out]")
    else:
        fc = f"{chain},apad,atrim=0:{T}[out]"
    run(cmd + ["-filter_complex", fc, "-map", "[out]", "-ar", "48000", "-c:a", "pcm_s24le", out])
    log(f"  · BGM {gain}dB, 더킹 {'ON' if duck else 'OFF'}")
    mark(out, sig)
    return out


# ───────────────────────── 자막 ─────────────────────────
def split_text(text, max_chars):
    chunks = []
    for line in re.split(r"\n|\|", text):
        cur = ""
        for word in line.split():
            if cur and len(cur) + 1 + len(word) > max_chars:
                chunks.append(cur)
                cur = word
            else:
                cur = f"{cur} {word}".strip()
            if re.search(r"[.!?。…]$", cur):
                chunks.append(cur)
                cur = ""
        if cur:
            chunks.append(cur)
    return [c for c in chunks if c]


def tc(sec):
    ms = int(round(sec * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def build_srt(scenes, fps, cfg, build):
    sub = cfg.get("subtitles", {})
    if not sub.get("enabled", True):
        return None
    portrait = int(cfg["height"]) > int(cfg["width"])
    max_chars = int(sub.get("max_chars", 16 if portrait else 28))
    cues = []
    for s in scenes:
        if not s["text"].strip():
            continue
        start = s["start"] / fps + (s["voice_offset"] if s["voice"] else 0)
        dur = s["voice_dur"] if s["voice"] else s["frames"] / fps
        parts = split_text(s["text"], max_chars)
        weights = [max(1, len(p.replace(" ", ""))) for p in parts]
        t = start
        for p, wgt in zip(parts, weights):
            d = dur * wgt / sum(weights)
            cues.append((t, t + d, p))
            t += d
    if not cues:
        return None
    out = build / "subtitles.srt"
    out.write_text("\n".join(f"{i}\n{tc(a)} --> {tc(b)}\n{txt}\n"
                             for i, (a, b, txt) in enumerate(cues, 1)), encoding="utf-8")
    log(f"  · 자막 {len(cues)}개 큐")
    return out


# ───────────────────────── ffmpeg 단독 조립(비교용) ─────────────────────────
def ffmpeg_preview(cfg, clips, voice, bgm, srt, total_sec, out_dir, name, build):
    lst = build / "concat.txt"
    lst.write_text("".join("file '{}'\n".format(str(c).replace("'", "'\\''")) for c in clips))
    cmd = FF + ["-f", "concat", "-safe", "0", "-i", lst]
    audio = [p for p in (voice, bgm) if p]
    for p in audio:
        cmd += ["-i", p]

    vf = "format=yuv420p"
    if srt and cfg.get("subtitles", {}).get("burn_in_preview", True):
        if has_filter("subtitles"):
            shutil.copyfile(srt, build / "preview.srt")
            portrait = int(cfg["height"]) > int(cfg["width"])
            style = cfg.get("subtitles", {}).get("style") or (
                "FontName=Apple SD Gothic Neo,Bold=1,Outline=2,Shadow=0,BorderStyle=1,"
                + ("FontSize=11,MarginV=40" if portrait else "FontSize=18,MarginV=24"))
            vf = f"subtitles=preview.srt:force_style='{style}'," + vf
        else:
            log("  · ffmpeg에 subtitles(libass) 필터가 없어 자막 번인 생략")

    if len(audio) == 2:
        fc = f"[1:a][2:a]amix=inputs=2:duration=longest:normalize=0[a]"
        cmd += ["-filter_complex", fc, "-map", "0:v", "-map", "[a]"]
    elif len(audio) == 1:
        cmd += ["-map", "0:v", "-map", "1:a"]
    else:
        cmd += ["-map", "0:v"]

    out = out_dir / f"{name}_ffmpeg.mp4"
    cmd += ["-vf", vf, "-c:v", "libx264", "-preset", "slow", "-crf", "18",
            "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709",
            "-c:a", "aac", "-b:a", "256k", "-t", f"{total_sec:.6f}",
            "-movflags", "+faststart", out.resolve()]
    run(cmd, cwd=build)
    log(f"  · ffmpeg 단독 조립본: {out}")
    return out


# ───────────────────────── main ─────────────────────────
def main():
    global VERBOSE
    ap = argparse.ArgumentParser(description="컷 리스트 → ffmpeg 전처리 → Resolve 조립 준비")
    ap.add_argument("cutlist")
    ap.add_argument("--preview", action="store_true", help="ffmpeg 단독 조립본도 렌더(품질 비교용)")
    ap.add_argument("--force", action="store_true", help="캐시 무시")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    VERBOSE = args.verbose

    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            sys.exit(f"[오류] {tool} 가 PATH에 없습니다 (brew install ffmpeg)")

    cl = Path(args.cutlist).expanduser().resolve()
    base = cl.parent
    cfg = json.loads(cl.read_text(encoding="utf-8"))
    for k in ("width", "height", "scenes"):
        if k not in cfg:
            sys.exit(f"[오류] cutlist에 '{k}' 필요")

    name = cfg.get("project", cl.stem)
    fps = int(cfg.get("fps", 24))
    build = (base / cfg.get("build_dir", "build")).resolve()
    out_dir = (base / cfg.get("render", {}).get("dir", "output")).resolve()
    clips_dir = build / "clips"
    for d in (build, out_dir, clips_dir):
        d.mkdir(parents=True, exist_ok=True)

    log(f"[1/4] 계획: {name}  {cfg['width']}x{cfg['height']} @{fps}fps")
    scenes, total = plan(cfg, base)
    total_sec = total / fps
    log(f"  · 씬 {len(scenes)}개, 총 {total}f ({total_sec:.2f}s)")

    log("[2/4] 클립 렌더 (ProRes 422 HQ / bt709)")
    clips = [render_clip(s, cfg, clips_dir, args.force) for s in scenes]

    log("[3/4] 오디오 · 자막")
    voice = build_voice(scenes, fps, total_sec, cfg, build, args.force)
    bgm = build_bgm(cfg, base, total_sec, voice, build, args.force)
    srt = build_srt(scenes, fps, cfg, build)

    log("[4/4] Resolve용 resolved JSON")
    r = cfg.get("render", {})
    lut = resolve_path(base, cfg.get("lut")) if cfg.get("lut") else None
    resolved = {
        "version": 1,
        "project": name,
        "fps": fps, "width": int(cfg["width"]), "height": int(cfg["height"]),
        "total_frames": total,
        "video": [{"id": s["id"], "path": str(c), "frames": s["frames"], "record": s["start"]}
                  for s, c in zip(scenes, clips)],
        "audio": [{"name": n, "path": str(p), "frames": total}
                  for n, p in (("voice", voice), ("bgm", bgm)) if p],
        "subtitles": str(srt) if srt else None,
        "subtitles_to_timeline": cfg.get("subtitles", {}).get("to_timeline", True),
        "lut": str(lut) if lut else None,
        "render": {
            "auto": r.get("auto", True),
            "dir": str(out_dir),
            "name": r.get("name", f"{name}_resolve"),
            "format": r.get("format", "mp4"),
            "codec": r.get("codec", "H264"),
            "quality": r.get("quality", "Best"),
        },
        "result_path": str(build / "resolve_result.json"),
        "log_path": str(build / "resolve_assemble.log"),
    }
    rp = build / "cutlist.resolved.json"
    rp.write_text(json.dumps(resolved, ensure_ascii=False, indent=2), encoding="utf-8")
    POINTER.parent.mkdir(parents=True, exist_ok=True)
    POINTER.write_text(json.dumps({"resolved": str(rp)}, ensure_ascii=False), encoding="utf-8")
    log(f"  · {rp}")
    log(f"  · 포인터 갱신: {POINTER}")

    if args.preview:
        log("[+] ffmpeg 단독 조립본 렌더")
        ffmpeg_preview(cfg, clips, voice, bgm, srt, total_sec, out_dir, name, build)

    log("\n완료 → Resolve에서 Workspace > Scripts > resolve_assemble 실행")


if __name__ == "__main__":
    main()
