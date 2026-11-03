# -*- coding: utf-8 -*-
"""
resolve_assemble.py — DaVinci Resolve 안에서 실행하는 조립 + 렌더 스크립트

무료 버전: 외부(터미널)에서 Resolve에 붙을 수 없으므로 이 파일을
  ~/Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts/Utility/
에 두고(install.sh 가 복사), Resolve 메뉴 Workspace > Scripts > resolve_assemble 로 실행한다.

입력: prep.py 가 기록한 포인터 ~/.resolve-assembly/current.json → cutlist.resolved.json
출력: 새 프로젝트/타임라인, (옵션) 렌더 파일, build/resolve_result.json (에이전트가 결과 확인용)

Studio 버전이라면 터미널에서 `python3 resolve_assemble.py [resolved.json]` 로도 실행 가능.
Resolve 내장 파이썬 호환을 위해 표준 라이브러리만, Python 3.6 문법으로 작성.
"""
import json
import os
import sys
import time
import traceback

POINTER = os.path.expanduser("~/.resolve-assembly/current.json")


class Log(object):
    def __init__(self):
        self.lines = []
        self.warnings = []
        self.path = None

    def __call__(self, msg):
        line = "[assemble] " + msg
        print(line)
        self.lines.append(line)
        self.flush()

    def warn(self, msg):
        self.warnings.append(msg)
        self("경고: " + msg)

    def flush(self):
        if self.path:
            try:
                with open(self.path, "w") as f:
                    f.write("\n".join(self.lines) + "\n")
            except Exception:
                pass


log = Log()


# ───────────────────────── Resolve 연결 ─────────────────────────
def get_resolve():
    g = globals()
    if g.get("resolve"):
        return g["resolve"]                      # Scripts 메뉴 실행 시 주입됨
    for name in ("bmd", "app"):
        obj = g.get(name)
        try:
            if name == "bmd" and obj:
                r = obj.scriptapp("Resolve")
                if r:
                    return r
            if name == "app" and obj:
                r = obj.GetResolve()
                if r:
                    return r
        except Exception:
            pass
    try:                                         # Studio: 외부 실행
        import DaVinciResolveScript as dvr      # noqa
        return dvr.scriptapp("Resolve")
    except Exception:
        return None


# ───────────────────────── 헬퍼 ─────────────────────────
def load_config(path=None):
    if not path:
        if not os.path.exists(POINTER):
            raise RuntimeError("포인터 파일이 없습니다. 먼저 `python3 prep.py cutlist.json` 실행: " + POINTER)
        with open(POINTER) as f:
            path = json.load(f)["resolved"]
    with open(path) as f:
        cfg = json.load(f)
    cfg["_path"] = path
    return cfg


def unique_project_name(pm, base):
    try:
        existing = set(pm.GetProjectListInCurrentFolder() or [])
    except Exception:
        existing = set()
    name, i = base, 2
    while name in existing:
        name = "{}_{}".format(base, i)
        i += 1
    return name


def norm(p):
    return os.path.normcase(os.path.realpath(p))


def import_media(mp, paths):
    items = mp.ImportMedia(list(paths)) or []
    by_path, by_name = {}, {}
    for it in items:
        fp = it.GetClipProperty("File Path") or ""
        if fp:
            by_path[norm(fp)] = it
        by_name[os.path.basename(fp or it.GetName() or "")] = it
    out = {}
    for p in paths:
        it = by_path.get(norm(p)) or by_name.get(os.path.basename(p))
        if not it:
            raise RuntimeError("미디어 임포트 실패: " + p)
        out[p] = it
    return out


def ensure_tracks(tl, kind, count, sub_type=None):
    have = tl.GetTrackCount(kind)
    while have < count:
        ok = tl.AddTrack(kind, sub_type) if sub_type else tl.AddTrack(kind)
        if not ok:
            raise RuntimeError("{} 트랙 추가 실패".format(kind))
        have += 1


def pick_codec(project, fmt, want):
    try:
        codecs = project.GetRenderCodecs(fmt) or {}
    except Exception:
        codecs = {}
    vals = list(codecs.values())
    if want in vals:
        return want
    for desc, val in codecs.items():
        d = (desc + val).replace(".", "").upper()
        if want.replace(".", "").upper() in d and "265" not in d:
            return val
    return want


def add_subtitles(mp, srt, tl_start):
    """SRT를 미디어 풀로 임포트 후 타임라인 맨 앞에 배치 (Resolve 18.5+)."""
    items = mp.ImportMedia([srt]) or []
    if not items:
        return False
    it = items[0]
    res = mp.AppendToTimeline([{"mediaPoolItem": it, "recordFrame": tl_start}])
    if not res:
        res = mp.AppendToTimeline([it])
    return bool(res)


# ───────────────────────── 메인 ─────────────────────────
def assemble(resolved_path=None):
    t0 = time.time()
    cfg = load_config(resolved_path)
    log.path = cfg.get("log_path")
    result = {"ok": False, "resolved": cfg["_path"], "warnings": log.warnings}
    try:
        resolve = get_resolve()
        if not resolve:
            raise RuntimeError("Resolve에 연결할 수 없습니다 (무료 버전은 Workspace > Scripts 메뉴에서 실행)")
        pm = resolve.GetProjectManager()

        name = unique_project_name(pm, cfg["project"])
        project = pm.CreateProject(name)
        if not project:
            raise RuntimeError("프로젝트 생성 실패: " + name)
        log("프로젝트 생성: " + name)
        result["project"] = name

        fps, w, h = cfg["fps"], cfg["width"], cfg["height"]
        for k, v in (("timelineFrameRate", str(fps)), ("timelinePlaybackFrameRate", str(fps)),
                     ("timelineResolutionWidth", str(w)), ("timelineResolutionHeight", str(h))):
            if not project.SetSetting(k, v):
                log.warn("설정 실패 {}={}".format(k, v))

        mp = project.GetMediaPool()
        folder = mp.AddSubFolder(mp.GetRootFolder(), "assembly")
        if folder:
            mp.SetCurrentFolder(folder)

        paths = [c["path"] for c in cfg["video"]] + [a["path"] for a in cfg["audio"]]
        seen, uniq = set(), []
        for p in paths:
            if p not in seen:
                seen.add(p)
                uniq.append(p)
        items = import_media(mp, uniq)
        log("미디어 임포트: {}개".format(len(items)))

        tl = mp.CreateEmptyTimeline(name)
        if not tl:
            raise RuntimeError("타임라인 생성 실패")
        project.SetCurrentTimeline(tl)
        start = tl.GetStartFrame()
        result["timeline"] = name

        # 자막은 타임라인이 비어 있을 때 먼저 넣어야 맨 앞(0초)에 정렬된다
        if cfg.get("subtitles") and cfg.get("subtitles_to_timeline", True):
            try:
                if add_subtitles(mp, cfg["subtitles"], start):
                    log("자막 트랙 추가")
                else:
                    log.warn("자막 자동 추가 실패 → File > Import > Subtitle 로 수동 임포트: " + cfg["subtitles"])
            except Exception as e:
                log.warn("자막 추가 오류({}) → 수동 임포트: {}".format(e, cfg["subtitles"]))

        # 비디오: V1에 씬 순서대로 (endFrame 은 포함 프레임)
        v_infos = [{
            "mediaPoolItem": items[c["path"]], "startFrame": 0, "endFrame": c["frames"] - 1,
            "trackIndex": 1, "recordFrame": start + c["record"], "mediaType": 1,
        } for c in cfg["video"]]
        v_items = mp.AppendToTimeline(v_infos) or []
        if len(v_items) != len(v_infos):
            raise RuntimeError("비디오 배치 실패 ({}/{})".format(len(v_items), len(v_infos)))
        log("비디오 클립 배치: {}개".format(len(v_items)))

        # 오디오: A1=voice, A2=bgm ...
        if cfg["audio"]:
            ensure_tracks(tl, "audio", len(cfg["audio"]), "stereo")
            a_infos = [{
                "mediaPoolItem": items[a["path"]], "startFrame": 0, "endFrame": a["frames"] - 1,
                "trackIndex": i + 1, "recordFrame": start, "mediaType": 2,
            } for i, a in enumerate(cfg["audio"])]
            a_items = mp.AppendToTimeline(a_infos) or []
            if len(a_items) != len(a_infos):
                raise RuntimeError("오디오 배치 실패 ({}/{})".format(len(a_items), len(a_infos)))
            for i, a in enumerate(cfg["audio"]):
                try:
                    tl.SetTrackName("audio", i + 1, a["name"])
                except Exception:
                    pass
            log("오디오 트랙 배치: " + ", ".join(a["name"] for a in cfg["audio"]))

        if cfg.get("lut"):
            bad = 0
            for it in v_items:
                try:
                    if not it.SetLUT(1, cfg["lut"]):
                        bad += 1
                except Exception:
                    bad += 1
            if bad:
                log.warn("LUT 적용 실패 {}개 클립".format(bad))
            else:
                log("LUT 적용: " + os.path.basename(cfg["lut"]))

        end = tl.GetEndFrame()
        if end - start != cfg["total_frames"]:
            log.warn("타임라인 길이 {}f ≠ 예상 {}f".format(end - start, cfg["total_frames"]))
        result["timeline_frames"] = end - start
        pm.SaveProject()
        log("프로젝트 저장")

        r = cfg["render"]
        if r.get("auto", True):
            result.update(render(project, r, w, h, fps))
        result["ok"] = result.get("render_status", "Complete") == "Complete"
    except Exception as e:
        result["error"] = str(e)
        log("실패: " + str(e))
        log(traceback.format_exc())
    finally:
        result["elapsed_sec"] = round(time.time() - t0, 1)
        try:
            with open(cfg["result_path"], "w") as f:
                json.dump(result, f, ensure_ascii=False, indent=2)
        except Exception:
            pass
        log("결과: {}".format("성공" if result["ok"] else "실패") + " → " + str(cfg.get("result_path")))
    return result


def render(project, r, w, h, fps):
    fmt = r.get("format", "mp4")
    codec = pick_codec(project, fmt, r.get("codec", "H264"))
    if not project.SetCurrentRenderFormatAndCodec(fmt, codec):
        log.warn("렌더 포맷/코덱 설정 실패: {}/{}".format(fmt, codec))
    if not os.path.isdir(r["dir"]):
        os.makedirs(r["dir"])
    base = {"SelectAllFrames": True, "TargetDir": r["dir"], "CustomName": r["name"],
            "ExportVideo": True, "ExportAudio": True}
    full = dict(base, FormatWidth=w, FormatHeight=h, FrameRate=float(fps),
                VideoQuality=r.get("quality", "Best"), AudioSampleRate=48000)
    if not project.SetRenderSettings(full):
        log.warn("상세 렌더 설정 일부 거부 → 기본 설정으로 재시도")
        if not project.SetRenderSettings(base):
            raise RuntimeError("렌더 설정 실패")
    project.DeleteAllRenderJobs()
    job = project.AddRenderJob()
    if not job:
        raise RuntimeError("렌더 잡 추가 실패")
    log("렌더 시작: {}/{} → {}".format(fmt, codec, os.path.join(r["dir"], r["name"])))
    t = time.time()
    project.StartRendering(job)
    last = -1
    while project.IsRenderingInProgress():
        st = project.GetRenderJobStatus(job) or {}
        pct = int(st.get("CompletionPercentage", 0))
        if pct >= last + 10:
            log("  렌더 {}%".format(pct))
            last = pct
        time.sleep(1)
    st = project.GetRenderJobStatus(job) or {}
    status = st.get("JobStatus", "Unknown")
    out = os.path.join(r["dir"], r["name"] + "." + fmt)
    log("렌더 {} ({:.1f}s) → {}".format(status, time.time() - t, out))
    return {"render_status": status, "output": out, "render_sec": round(time.time() - t, 1)}


if __name__ == "__main__":
    assemble(sys.argv[1] if len(sys.argv) > 1 and sys.argv[1].endswith(".json") else None)
