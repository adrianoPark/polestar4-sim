# resolve-assembly — ffmpeg 전처리 + DaVinci Resolve(무료) 자동 조립 PoC

기존 ffmpeg 조립 파이프라인을 유지한 채 **Resolve로 조립하고 렌더하는** 1단계 PoC입니다.
같은 컷 리스트로 **ffmpeg 단독 조립본도 함께 뽑아서** 두 방식의 품질을 직접 비교할 수 있습니다.

```
cutlist.json ──► prep.py (ffmpeg)
                   ├─ build/clips/*.mov      씬 클립 (ProRes 422 HQ, bt709, 이징 줌/팬)
                   ├─ build/voice_mix.wav    음성 배치 + 2-pass loudnorm (-15 LUFS)
                   ├─ build/bgm_ducked.wav   BGM 루프/페이드 + 음성 사이드체인 더킹
                   ├─ build/subtitles.srt    음성 길이 기준 자막 자동 분할
                   ├─ build/cutlist.resolved.json  ◄── ~/.resolve-assembly/current.json (포인터)
                   └─ output/<name>_ffmpeg.mp4      (--preview, 비교용)
                                │
      Resolve: Workspace > Scripts > resolve_assemble   (클릭 한 번)
                   ├─ 새 프로젝트/타임라인 (fps·해상도 자동)
                   ├─ V1 씬 클립, A1 voice, A2 bgm, 자막 트랙, (옵션) LUT
                   ├─ output/<name>_resolve.mp4 렌더
                   └─ build/resolve_result.json   ◄── 에이전트가 결과 확인
```

## 맥 설치 (한 번만)

1. 이 폴더를 `~/Desktop/workplace/claude/resolve-assembly/`에 둡니다.
2. `brew install ffmpeg`
3. **Python 3를 python.org 설치판으로 설치합니다.** Resolve 내장 스크립트가 이 Python을 사용합니다. 설치 후 Resolve의 Workspace > Console에서 `Py3` 탭이 보이면 정상입니다.
4. `./install.sh`를 실행합니다. Resolve Scripts 메뉴에 `resolve_assemble`이 등록되니, Resolve를 재시작하세요.

## 사용법

```bash
# 0) (처음 한 번) 샘플로 전체 흐름 확인
python3 make_sample.py
python3 prep.py sample/cutlist.json --preview

# 1) Resolve에서: Workspace > Scripts > resolve_assemble
#    → 조립 + 렌더 → sample/output/poc_sample_resolve.mp4

# 2) 비교
#    sample/output/poc_sample_ffmpeg.mp4  vs  sample/output/poc_sample_resolve.mp4
```

실제 작업에서는 에피소드 폴더에 `cutlist.json`을 만들고 `python3 prep.py <경로>/cutlist.json`을 실행합니다.
`prep.py`는 항상 마지막으로 처리한 컷 리스트를 포인터에 기록하므로, Resolve에서는 같은 메뉴만 누르면 됩니다.
씬 클립은 캐시되므로, 컷 하나만 고치면 그 컷만 다시 렌더됩니다(`--force`를 주면 전부 다시 렌더).

## cutlist.json 형식

예시는 `sample/cutlist.json`에 있습니다.

| 키 | 설명 |
|---|---|
| `project`, `fps`, `width`, `height` | 프로젝트 이름과 규격 (세로 쇼츠는 1080×1920) |
| `scene_padding` | 음성이 끝난 뒤 다음 씬까지의 여유(초) |
| `voice_offset` | 씬이 시작되고 음성이 나오기까지의 지연(초) |
| `motion_upscale` | 줌/팬을 계산할 때 내부 업스케일 배수 (클수록 떨림이 줄지만 느려짐, 기본 3) |
| `audio.bgm` / `bgm_gain_db` / `voice_lufs` / `duck` | BGM 경로, BGM 볼륨, 음성 목표 라우드니스, 더킹 여부 |
| `subtitles.max_chars` | 자막 한 줄 최대 글자 수. `|`나 줄바꿈을 넣으면 그 위치에서 강제로 나눔 |
| `subtitles.to_timeline` | Resolve 타임라인에 자막 트랙을 추가할지 여부 |
| `lut` | (옵션) .cube LUT 경로. 모든 클립의 1번 노드에 적용 |
| `render.auto` | `false`면 조립까지만 하고 렌더는 수동 |
| `scenes[]` | 씬 목록. 순서대로 V1에 배치됨 |

씬 하나의 키:

| 키 | 설명 |
|---|---|
| `media` | 이미지(png/jpg/webp…) 또는 영상(mp4/mov…) |
| `voice`, `text` | 씬 음성(TTS)과 자막 텍스트 |
| `duration` | 씬 길이를 직접 지정. 없으면 음성 길이+여유, 영상이면 영상 길이, 둘 다 없으면 3초 |
| `in` | 영상 시작 지점(초) |
| `motion.type` | `zoom_in` `zoom_out` `pan_left` `pan_right` `pan_up` `pan_down` `none` |
| `motion.amount` / `motion.ease` | 움직임 강도(0.1~0.25 권장) / `smooth`(기본) `linear` `in` `out` |

## 품질 관련 설계 포인트

- **색:** 모든 중간 파일을 bt709로 명시해서 변환합니다. RGB 이미지가 bt601로 잘못 변환돼 색이 틀어지는 흔한 문제를 막기 위해서입니다.
- **카메라 무브:** 원본을 3배로 업스케일한 상태에서 zoompan을 계산합니다. 정수 좌표 때문에 생기는 미세한 떨림을 1/3픽셀 수준으로 줄이고, smoothstep 이징을 적용합니다.
- **오디오:** 음성은 2-pass loudnorm으로 맞추고, BGM은 음성 기준 사이드체인 더킹을 겁니다. Resolve에서는 voice와 bgm이 **별도 트랙**으로 들어가므로 볼륨을 손으로 다시 조절할 수 있습니다.
- **중간 코덱:** ProRes 422 HQ를 씁니다. 편집 중 화질 손실이 없고, 맥에서 Resolve가 가볍게 디코딩합니다.

## 무료 버전의 제약과 확인 방법

- 외부(터미널, 에이전트)에서 Resolve를 직접 호출할 수 없어서 **메뉴 클릭 한 번이 필요합니다.**
  (Studio라면 `python3 resolve_assemble.py`로 완전 자동 실행 가능)
- 실행 결과는 `build/resolve_result.json`과 `build/resolve_assemble.log`에 남습니다. 에이전트는 이 파일로 성공 여부, 출력 경로, 경고를 확인합니다.
- 자막 자동 추가는 Resolve 18.5 이상에서 동작합니다. 실패하면 경고를 남기니 `File > Import > Subtitle`로 `build/subtitles.srt`를 넣으세요.

## 테스트

```bash
python3 make_sample.py && python3 prep.py sample/cutlist.json --preview
python3 tests/test_resolve_mock.py   # 가짜 Resolve API로 조립/렌더 로직 검증
```

## 다음 단계 (2단계 후보)

- 캐릭터/배경 레이어 분리: V2 오버레이와 패럴랙스
- 입 모양 스프라이트 립싱크(Rhubarb) 클립 생성을 `prep.py`에 통합
- Fusion 템플릿(카메라 무브, 자막 스타일) 적용
- 기존 ffmpeg 스크립트의 출력 → `cutlist.json` 자동 변환
