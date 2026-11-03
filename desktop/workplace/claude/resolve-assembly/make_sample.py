#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""make_sample.py — 파이프라인 검증용 더미 에셋 생성 (sample/assets/)

  python3 make_sample.py && python3 prep.py sample/cutlist.json --preview

 - s01.png : 만델브로트(디테일 많음 → 줌 떨림 확인용)
 - s02.mp4 : 움직이는 테스트 패턴 (i2v 클립 대용)
 - s03.png : 가는 격자 테스트 패턴 (팬 떨림 확인용)
 - v01/v02.wav : 말소리 흉내 톤 (TTS 대용)
 - bgm.wav : 화음 루프
"""
import subprocess
from pathlib import Path

OUT = Path(__file__).resolve().parent / "sample" / "assets"
OUT.mkdir(parents=True, exist_ok=True)
FF = ["ffmpeg", "-hide_banner", "-nostdin", "-y", "-loglevel", "error"]


def ff(*args):
    subprocess.run(FF + [str(a) for a in args], check=True)


ff("-f", "lavfi", "-i", "mandelbrot=s=1600x2400:end_pts=1:start_scale=1.5",
   "-frames:v", "1", OUT / "s01.png")
ff("-f", "lavfi", "-i", "testsrc2=s=1280x720:r=30:d=4", "-c:v", "libx264",
   "-pix_fmt", "yuv420p", OUT / "s02.mp4")
ff("-f", "lavfi", "-i", "testsrc=s=2400x1600:r=1", "-frames:v", "1", OUT / "s03.png")
for name, f, d in (("v01", 190, 2.6), ("v02", 230, 3.4)):
    ff("-f", "lavfi", "-i", f"sine=f={f}:d={d}:sample_rate=48000",
       "-af", "tremolo=f=6:d=0.9,volume=0.5,afade=t=in:d=0.05,afade=t=out:st={:.2f}:d=0.1".format(d - 0.1),
       OUT / f"{name}.wav")
ff("-f", "lavfi", "-i",
   "aevalsrc=0.25*sin(2*PI*220*t)+0.18*sin(2*PI*277.2*t)+0.15*sin(2*PI*329.6*t):s=48000:d=8",
   "-af", "tremolo=f=0.5:d=0.4", "-ac", "2", OUT / "bgm.wav")
print("샘플 에셋 생성:", OUT)
