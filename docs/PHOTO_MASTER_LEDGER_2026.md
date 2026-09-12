---
name: google-photos-bulk-uploader
description: >-
  사진 라이브러리 통합 관리 및 구글 포토 대용량 업로드를 위한 통합 마스터 원장(Ledger)이자 운용 스킬. 100% 무손실 EXIF 메타데이터 보정, Dual Face AI 회전 보정 내역, IQA 품질 평가 및 스크린샷/리사이즈 중복 격리, Producer-Consumer 업로드 아키텍처, SQLite 상태 추적 DB, 대용량 파일 소켓 방지 및 API 할당량 관리 완비.
---

# 📸 사진 라이브러리 통합 관리 및 구글 포토 벌크 업로드 통합 원장 (Master Ledger)

> **원장 최종 갱신일**: 2026-09-06 (품질 평가 1·2단계 완료 및 4,089장 안전 격리 적용)  
> **총괄 책임**: AGY Pair-Programming Engine  
> **정본 보관소**: `D:\_Reports\PHOTO_MASTER_LEDGER_2026.md` 및 Agent Skill (`google-photos-bulk-uploader`)  
> **상태 요약**: `D:\Photos_Merged` 104,271장 (689.28 GB) 고품질 정본 확립, 품질 선별 4,089장 (4.30 GB) 검토소 격리 완료

---

## ⛔ 제1장. 원장의 최우선 지위 및 라이브러리 구조

본 원장은 수개월에 걸쳐 진행된 외장 드라이브 사진 병합, 중복 정리, 무손실 회전 보정, 품질 진단 및 격리, 그리고 구글 포토 업로드 진행 상태를 기록한 **최상위 단일 진실 공급원(Single Source of Truth)** 입니다.

### 1.1 드라이브 및 핵심 디렉터리 현황 (2026-09-06 최신 실측)

| 경로 | 규모 | 성격 및 역할 | 안전 수칙 |
|---|---:|---|---|
| **`D:\Photos_Merged`** | **104,271 파일 (689.28 GB)** | ⭐ **가족 사진·동영상 최종 통합 고품질 정본 라이브러리** (1980~2026년, 27개 연도별 폴더) | **정본 라이브러리.** 삭제 절대 금지 |
| **`D:\_TO_REVIEW_QUALITY_CANDIDATES`** | **4,089 파일 (4.30 GB)** | 품질 진단(IQA) 결과 선별된 스크린샷, 저화질 축소 중복본, 웹 썸네일 검토 보관소 | **격리 보관소.** 사용자 직접 검토용 |
| **`D:\Photos_Merged\_Rescued_Missing`** | **2,956 파일 (12.84 GB)** | 1차 병합 과정에서 누락될 뻔했으나 전수 감사로 구출된 순수 원본 | 전수 EXIF 보존 완료 |
| **`D:\_TO_DELETE`** | **131,432 파일 (853.43 GB)** | 병합 완료 후 남은 구 원본, 내부 중복본 및 `Notebook`(45개) 격리소 | **절대 임의 삭제 금지.** (사용자 직접 승인 시에만 처리) |
| **`D:\_Reports`** | **62개 감사 파일** | 전체 병합 저널, 복사/회전 저널, 품질 격리 저널, 무결성 리포트 전수 보관소 | 영구 보존 대상 |
| **`D:\haarcascades`** | **17개 모델 파일** | OpenCV 얼굴 검출 AI 모델 (정면/측면 캐스케이드) | AI 회전 검출 엔진용 |
| **`D:\photos_uploader`** | **클린 전용 환경 (11개 도구 파일, 최신 DB 153.8 MB)** | 구글 포토 대용량 업로더, OAuth 인증 토큰, 129,434건 마스터 DB 동기화 완료 | 업로더 전용 클린 환경 확립 |
| **`E:\` (PhotoHDD_1G, 외장 드라이브)** | **931.35 GB 전체 가용 (포맷 완료)** | 신규 보충용 원본 전수 D: 이관 대조 완료 후 클린 빠른 포맷 완료 | 새로운 백업용으로 100% 가용 상태 |

---

## 🛡️ 제2장. 100% 무손실 미디어 처리 절대 원칙 (Zero-Loss Principle)

사용자의 핵심 요구사항: **"어떤 작업에서도 원본 이미지의 화질에 영향이 있으면 안된다."**

### 2.1 픽셀 재인코딩 절대 금지
- 일반적인 이미지 라이브러리(`PIL.Image.save(..., quality=95)` 또는 `cv2.imwrite()`)는 JPEG DCT 블록을 디코딩 후 재압축하므로 영구적인 화질 열화(Generation Loss)를 발생시킵니다.
- **절대 규칙**: 이미지 픽셀 데이터(SOF, SOS 블록)는 1바이트도 수정하지 않으며, 오직 JPEG 헤더의 메타데이터 세그먼트(`APP1` EXIF)만을 바이너리 레벨에서 직접 수정합니다.

### 2.2 표준 무손실 메타데이터 주입 파이프라인
```python
import os
import piexif

def apply_lossless_orientation(filepath: str, target_orientation: int):
    # 1. 원본 파일 수정 시간(mtime) 보존
    stat = os.stat(filepath)
    mtime = stat.st_mtime

    # 2. EXIF 바이너리 로드
    exif_dict = piexif.load(filepath)
    
    # 3. 0th IFD Orientation 태그 설정 (Tag 274)
    if "0th" not in exif_dict:
        exif_dict["0th"] = {}
    exif_dict["0th"][piexif.ImageIFD.Orientation] = target_orientation

    # 4. 특수 기종 호환성 처리 (삼성 Note 3 등 비표준 태그 37510 제거)
    if "Exif" in exif_dict:
        exif_dict["Exif"].pop(37510, None)
    
    # 5. 썸네일 오버플로우 방지 (필요 시 제거)
    exif_dict.pop("thumbnail", None)

    # 6. APP1 세그먼트에 바이너리 주입 (픽셀 데이터 무수정)
    exif_bytes = piexif.dump(exif_dict)
    piexif.insert(exif_bytes, filepath)

    # 7. 원본 파일 수정 시간 완벽 복원
    os.utime(filepath, (mtime, mtime))
```

---

## 🔄 제3장. 라이브러리 변천 및 감사 기록 (Audit History)

### 3.1 조치 2: 과거 회전본 이중 회전 결함 정상화 (Action 2 Completed)
- `D:\Photos_Merged` 내 과거 회전 이력이 있는 사진 **11,954장** 전수 조사.
- 11,853장 `Orientation = 1` 정방향 바이너리 주입 완료 (에러 0건).
- 감사 로그: `D:\_Reports\exif_double_rotation_fix_log.csv`

### 3.2 3단계: 30,455장 미회전 사진 AI 분석 및 선별 무손실 회전 (Phase 3 Completed)
- 미회전 사진 **30,455장** Dual Face AI 스캔.
- 28,982장 (95.2%) 정방향/자연 풍경 사진 보존.
- 1,473장 (4.8%) 실제 옆으로 누운 사진 확정 검출 및 무손실 EXIF 태그 6/8 주입 완료.
- 감사 로그: `D:\_Reports\not_rotated_candidates_rotation_log.csv`

### 3.3 4단계: E: 드라이브 신규 4,214장 편입 및 무손실 통합 (Phase 4 Completed)
- 순수 신규 파일 4,214장 표준 파일명 통일 및 복사 (누락 0장).
- 하드웨어 자이로 기태그 253장 보존, AI 회전 53장 무손실 적용, 정방향 159장 보호, 풍경/HEIC 2,276장 보존.
- 감사 로그: `D:\_Reports\e_import_copy_journal.csv`, `D:\_Reports\e_import_verification_summary.json`

### 3.4 5단계: IQA 품질 진단 및 저가치 미디어 안전 격리 (Phase 5 Completed - 2026-09-06)
- **전수 진단 (108,360장)**:
  - 스마트폰 스크린샷/1회성 캡처 (`SCREENSHOT_OR_CAPTURE`): **1,491장 (2.76 GB)**
  - 고화질 원본이 존재하는 리사이즈 축소 중복본 (`REDUNDANT_DOWNSCALED_COPY`): **2,587장 (1.54 GB)**
  - 초저해상도 웹 썸네일 (`LOW_RES_WEB_TRASH`): **11장 (0.3 MB)**
  - 합계: **4,089장 (4.30 GB)**
- **옵션 A 안전 격리 실행**:
  - `D:\_TO_REVIEW_QUALITY_CANDIDATES\` 하위 카테고리별·연도별 폴더로 원자적 이동 완료 (이동 에러: 0건).
  - 정본 라이브러리: 108,360장 → **104,271장 (689.28 GB)**으로 고순도 정화.
  - 100% 원복 가능 저널: `D:\_Reports\quality_quarantine_journal.csv`
  - 감사 로그: `D:\_Reports\photo_quality_audit_candidates.csv`, `D:\_Reports\photo_quality_audit_summary.json`

---

### 3.5 6단계: 3단계 시각적 회전(사람·하늘·소실점) 전수 감사 및 26,483장 무손실 보정 (Phase 6 Completed - 2026-09-06)
- **감사 대상**: `D:\Photos_Merged` 20개 연도 폴더 전체 82,148장 전수 시각적 감사.
- **적용 결과**: 1단계 인물 기준(Haar Cascade) 22,235장 + 2단계 하늘 기준(HSV Sky Segmentation) 3,456장 + 3단계 소실점 기준(Hough Line / Vanishing Point) 792장 = **총 26,483장**에 대해 무손실 EXIF Orientation 태그 주입(Zero Pixel Re-encoding) 및 NTFS mtime 100% 복원 완료.
- **감사 저널**: `D:\_Reports\3stage_rotation_execution_journal.csv` 보관.

### 3.6 7단계: Canon G7X CMOS 배터리 방전 사진 58장 실촬영일 정밀 복원 (Phase 7 Completed - 2026-09-12)
- **정리 대상**: `19800101...` 접두사로 남아 있던 Canon PowerShot G7 X Mark II 촬영본 58장 (`2019`: 55장, `2020`: 1장, `2021`: 2장).
- **조치 내역**:
  1. 인접 실촬영 사진과의 타임라인/이벤트 대조를 통해 실촬영일(2019-10-25, 2019-10-26, 2019-11-06, 2020-04-19, 2021-04-21, 2021-06-09) 복원.
  2. 표준 파일명 규칙(`YYYYMMDDHHmmss_[Device_Resolution_GPS].jpg`) 충돌 없이 100% 표준화 변경.
  3. `piexif` 무손실 EXIF 주입을 통해 `DateTimeOriginal`, `DateTimeDigitized`, `DateTime` 태그 동기화 (재인코딩 0%).
  4. NTFS 파일 수정 시각(`os.utime`) 100% 일치 보존.
- **결과**: `D:\Photos_Merged` 내 `1980*` 잔여 파일 **0건 달성**.
- **감사 저널**: `D:\_Reports\canon_g7x_1980_rename_journal.csv` 보관.

---

## 📊 제4장. 정제된 라이브러리 연도별 / 단말별 최종 현황 (104,271장 정본)

> 전체 상세 내역 CSV 원장: [`D:\_Reports\Photos_Merged_inventory_by_year_device.csv`](file:///D:/_Reports/Photos_Merged_inventory_by_year_device.csv)

| 연도 | 정제 후 수량 | 주요 촬영 단말 및 기종별 사진 수량 |
|---|---:|---|
| **1980** | 2장 | Canon PowerShot G7 X Mark II: 2장 |
| **2003** | 1장 | Canon DIGITAL IXUS 400: 1장 |
| **2005** | 1장 | Canon DIGITAL IXUS 400: 1장 |
| **2006** | 876장 | Nikon Coolpix P2 (469장), Canon PowerShot A60 (395장), 기타 (12장) |
| **2008** | 1장 | Unknown: 1장 |
| **2009** | 151장 | Canon EOS 450D: 151장 |
| **2010** | 3,123장 | **Canon EOS 5D (2,015장)**, Canon EOS 450D (858장), 기타 (250장) *(축소 중복 967장 격리)* |
| **2011** | 1,247장 | Canon EOS 450D (569장), Apple iPhone 4 (358장), Canon 기타 (320장) |
| **2012** | 316장 | Canon EOS 450D (194장), Apple iPhone 4 (73장), 기타 (49장) |
| **2013** | 2,415장 | Apple iPhone 5 (1,774장), Canon EOS 450D (302장), 기타 (339장) |
| **2014** | 7,236장 | Samsung NX mini (3,151장), **Canon EOS 5D Mark II (결혼식 본식 등 1,495장)**, Nikon Df (628장), Apple iPhone 5 (608장) |
| **2015** | 3,068장 | Sony ILCE-5000 (884장), Samsung Galaxy S6 (461장), Samsung Galaxy Note 3 (313장), 기타 (1,410장) |
| **2016** | 12,740장 | **Samsung Galaxy S7 (8,203장)**, Apple iPhone 5 (719장), Samsung Galaxy S6 (564장), 기타 (3,254장) |
| **2017** | 14,429장 | **Samsung Galaxy S7 (10,665장)**, Canon EOS 750D (1,990장), 기타 (1,774장) |
| **2018** | 5,730장 | **Samsung Galaxy S7 (2,392장)**, Canon EOS 750D (981장), Apple iPhone X (583장), 기타 (1,774장) |
| **2019** | 15,293장 | **Apple iPhone X (5,591장)**, Samsung Galaxy Note 10 5G (1,459장), Canon EOS 750D (917장), 기타 (7,326장) |
| **2020** | 14,405장 | **Apple iPhone X (5,666장)**, Samsung Galaxy Note 20 Ultra (2,306장), 기타 (6,433장) |
| **2021** | 8,329장 | **Apple iPhone X (2,203장)**, Samsung Galaxy S20+ (1,723장), Apple iPhone 11 (277장), 기타 (4,126장) |
| **2022** | 4,411장 | **Apple iPhone 13 (3,135장)**, Samsung Galaxy S20+ (313장), Apple (286장), Samsung Galaxy Note 10+ (209장), 기타 (468장) |
| **2023** | 4,638장 | **Apple iPhone 13 (312장)**, Samsung Galaxy Note 10+ (156장), Apple iPhone 12 Pro (145장), 기타 (4,025장) |
| **2024** | 3,119장 | Samsung Galaxy Z Fold 5 (119장), Apple iPhone 13 (68장), Apple iPhone 12 Pro (62장), 기타 (2,870장) |
| **2026** | 5장 | Unknown: 5장 |
| **기타 (특수/구출)** | 2,735장 | Samsung (2,036장), Nikon (273장), Apple (160장), 기타 (266장) |
| **총계** | **104,271장** | **총 라이브러리 용량: 689.28 GB (고순도 정본)** |

---

## ☁️ 제5장. 구글 포토 대용량 업로더 아키텍처 및 운용 가이드

### 5.1 Producer-Consumer Worker Pool
1. **Producer (디렉터리 스캐너)**:
   - 대상 디렉터리(`D:\Photos_Merged`)를 순회하며 부분 해시(`compute_partial_hash`)를 산출.
   - 로컬 DB(`upload_progress.db`)를 조회하여 이미 업로드 성공(`status='success'`)한 파일은 즉시 매핑하여 중복 업로드 방지.
2. **Consumer (업로드 워커 쓰레드)**:
   - 멀티스레드 큐에서 미업로드(`pending`) 작업을 인출하여 구글 포토 API로 바이트 스트림 전송. (권장 워커: 2 ~ 3개)
3. **윈도우 소켓 프리징 방지**:
   - 50MB 초과 동영상은 기본 업로더에서 분리(`skipped`), `upload_large_files.py` 전용 분할 스크립트로 전송.
4. **API 할당량 관리**:
   - 일일 10,000건 중 **9,800건** 도달 시 자동 정지.

### 5.2 표준 실행 명령어 (CLI Reference)

```powershell
# 1. 기본 전체 라이브러리 벌크 업로드 실행 (권장 워커 3개)
python "C:\Users\갑상선외과\.gemini\config\skills\google-photos-bulk-uploader\upload_to_photos.py" --source "D:\Photos_Merged" --workers 3

# 2. 업로드 시뮬레이션 (신규/기등록 수량 확인)
python "C:\Users\갑상선외과\.gemini\config\skills\google-photos-bulk-uploader\upload_to_photos.py" --source "D:\Photos_Merged" --dry-run

# 3. 50MB 초과 대용량 파일 분할 전송 실행
python "C:\Users\갑상선외과\.gemini\config\skills\google-photos-bulk-uploader\upload_large_files.py" --source "D:\Photos_Merged"
```

---

## 🔄 제6장. 구글 포토 클라우드 정제 및 최근 사진 안전 회수 전략 (2026-09-12 수립)

### 6.1 구글 포토 클라우드 현황 및 교체 당위성
1. **과거 상태**: 예전 외장하드(`E:\...`) 시절 무작위 폴더 구조로 업로드되어 구글 포토 내 **467개의 난잡한 앨범** 및 누운 사진(미회전본), 중복본이 산재함.
2. **현재 정본 (`D:\Photos_Merged`)**: AI 안면/하늘/소실점 기반 **26,483장 무손실 회전 보정**, Canon G7X **1980년 배터리 방전 사진 58장 실촬영일 복원**, 중복 제거 및 연도별(`2003`~`2023`) 단일 체계로 완벽히 정제된 **105,045장 정본**.
3. **최종 목표**: 구글 포토의 흐트러진 467개 앨범 및 과거 미회전/중복 사진을 전면 정리하고, 현재의 정제된 정본 사진으로 교체.

### 6.2 사전 필수 안전 조치: 최근 사진(2024~2026년) 안전 회수
- **현황 확인**: `D:\Photos_Merged`는 2024년 초까지(3,091장)만 보유하고 있으며, **2025~2026년 신규 사진은 로컬에 부재(0장)**. 스마트폰 등에서 구글 포토로 자동 백업된 최근 사진들이 클라우드에만 단독 존재할 위험이 높음.
- **안전 제1원칙**: 구글 포토의 기존 사진을 정리하거나 앨범을 비우기 전에, **구글 포토의 최근 사진(2024~2026년)을 로컬 `D:\`로 100% 안전하게 먼저 다운로드**하여 정본에 통합해야 함.

### 6.3 최근 사진 다운로드 3대 파이프라인
1. **파이프라인 1 (API 자동 다운로더: `download_recent_from_photos.py`)**:
   - 위치: `D:\photos_uploader\download_recent_from_photos.py`
   - 스코프: `photoslibrary.readonly`
   - 기능: 2024-01-01 이후 신규 사진 자동 검색, 원본 바이트(`baseUrl=d`/`baseUrl=dv`) 스트리밍 다운로드, 촬영일시 기준 mtime 복원, 중복 스킵, 저널(`D:\_Reports\google_photos_recent_downloads_journal.csv`) 기록.
2. **파이프라인 2 (구글 테이크아웃: `takeout.google.com`)**:
   - Google 포토 선택 후 `2024년 사진`, `2025년 사진`, `2026년 사진` 앨범만 선택하여 ZIP 다운로드.
   - Live Photo, 원본 HEIC, JSON 메타데이터 100% 무손실 보존 가능.
3. **파이프라인 3 (구글 포토 웹 직접 다운로드)**:
   - [photos.google.com](https://photos.google.com)에서 최근 사진 선택 후 `Shift + D` 일괄 다운로드.

