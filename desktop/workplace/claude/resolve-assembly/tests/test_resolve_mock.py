# -*- coding: utf-8 -*-
"""가짜 Resolve API로 resolve_assemble.py 로직 검증 (Resolve 없이 실행 가능)

  python3 make_sample.py && python3 prep.py sample/cutlist.json && python3 tests/test_resolve_mock.py
"""
import json, os, sys
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import resolve_assemble as ra  # noqa

calls = []

class Item:
    def __init__(s, p): s.p = p
    def GetClipProperty(s, k): return s.p if k == "File Path" else ""
    def GetName(s): return os.path.basename(s.p)

class TLItem:
    def SetLUT(s, i, p): return True

class Timeline:
    def __init__(s): s.tracks = {"video": 1, "audio": 1, "subtitle": 0}; s.end = 86400
    def GetStartFrame(s): return 86400
    def GetEndFrame(s): return s.end
    def GetTrackCount(s, k): return s.tracks[k]
    def AddTrack(s, k, sub=None): s.tracks[k] += 1; return True
    def SetTrackName(s, k, i, n): calls.append(("trackname", k, i, n)); return True

class MediaPool:
    def __init__(s): s.tl = None
    def GetRootFolder(s): return "root"
    def AddSubFolder(s, r, n): return n
    def SetCurrentFolder(s, f): return True
    def ImportMedia(s, paths): return [Item(p) for p in paths]
    def CreateEmptyTimeline(s, n): s.tl = Timeline(); return s.tl
    def AppendToTimeline(s, infos):
        out = []
        for i in infos:
            if isinstance(i, dict) and "endFrame" in i:
                assert i["endFrame"] >= i["startFrame"]
                assert i["recordFrame"] >= 86400
                s.tl.end = max(s.tl.end, i["recordFrame"] + i["endFrame"] - i["startFrame"] + 1)
            calls.append(("append", i.get("trackIndex"), i.get("mediaType"), i.get("recordFrame")) if isinstance(i, dict) else ("append-plain",))
            out.append(TLItem())
        return out

class Project:
    def __init__(s): s.mp = MediaPool(); s.n = 0
    def SetSetting(s, k, v): calls.append(("set", k, v)); return True
    def GetMediaPool(s): return s.mp
    def SetCurrentTimeline(s, t): return True
    def GetRenderCodecs(s, f): return {"H.264": "H264", "H.265": "H265"}
    def SetCurrentRenderFormatAndCodec(s, f, c): calls.append(("codec", f, c)); return True
    def SetRenderSettings(s, d): calls.append(("render", d)); return True
    def DeleteAllRenderJobs(s): return True
    def AddRenderJob(s): return "job1"
    def StartRendering(s, j): return True
    def IsRenderingInProgress(s): s.n += 1; return s.n < 3
    def GetRenderJobStatus(s, j): return {"JobStatus": "Complete" if s.n >= 3 else "Rendering", "CompletionPercentage": min(100, s.n * 40)}

class PM:
    def GetProjectListInCurrentFolder(s): return ["poc_sample"]
    def CreateProject(s, n): calls.append(("project", n)); return Project()
    def SaveProject(s): return True

class Resolve:
    def GetProjectManager(s): return PM()

ra.time.sleep = lambda x: None
ra.resolve = Resolve()
res = ra.assemble()
cfg = ra.load_config()

assert res["ok"], res
assert res["project"] == "poc_sample_2", res          # 중복 이름 회피
assert res["timeline_frames"] == cfg["total_frames"], res
v = [c for c in calls if c[0] == "append" and c[2] == 1]
assert len(v) == len(cfg["video"])
assert [c[3] - 86400 for c in v] == [x["record"] for x in cfg["video"]]
a = [c for c in calls if c[0] == "append" and c[2] == 2]
assert [c[1] for c in a] == [1, 2]
assert ("codec", "mp4", "H264") in calls
assert not res["warnings"], res["warnings"]
assert json.load(open(cfg["result_path"]))["ok"]
print("OK — mock Resolve 조립/렌더 흐름 통과:", json.dumps(res, ensure_ascii=False))
