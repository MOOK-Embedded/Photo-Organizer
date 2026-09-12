# 📸 Photo-Organizer

> **10만 장 규모 대용량 사진 라이브러리 100% 무손실 정제 및 Google Photos 클라우드 동기화 시스템**  
> *Lossless Photo Organization, 3-Stage Visual Rotation Audit, Metadata Standardization, and Google Photos API Bulk Automation*

---

## 🌟 개요 (Overview)

**Photo-Organizer**는 외장하드, 스마트폰, 디지털카메라 등 다양한 소스에서 수집된 10만 장 이상의 대용량 사진/동영상 라이브러리를 **100% 무손실(Zero-Loss)** 원칙하에 정밀하게 분류, 보정, 표준화하고 Google Photos 클라우드와 양방향 동기화하는 전문 엔지니어링 파이프라인입니다.

### 핵심 차별점 (Core Capabilities)
1. **100% 무손실 처리 (Zero Pixel Re-encoding)**:
   - JPEG 화질 저하(Generation Loss)가 발생하는 픽셀 재압축을 일체 배제.
   - `piexif` 바이너리 레벨 EXIF Orientation 및 DateTime 태그 주입.
   - 원본 파일의 NTFS 파일 수정 시각(`os.utime`) 100% 일치 보존.
2. **3단계 시각적 회전 감사 및 자동 보정 (3-Stage Visual Rotation Engine)**:
   - **1단계 (인물 기준)**: OpenCV Haar Cascade (정면·프로필 등 17개 모델 앙상블)로 사람 안면을 감지하여 기립 방향(0°, 90°, 180°, 270°) 판정.
   - **2단계 (풍경/하늘 기준)**: HSV 색공간 스카이 세그멘테이션(Sky Segmentation)으로 하늘이 위로 가도록 정렬.
   - **3단계 (소실점 기준)**: 허프 변환(Hough Lines) 및 원근 소실점(Vanishing Point) 분석으로 수렴 지점이 상단을 향하도록 정렬.
3. **고장/방전 카메라 타임라인 정밀 복원**:
   - Canon PowerShot G7 X Mark II 등 CMOS 배터리 방전으로 `1980:01:01`로 리셋된 사진들을 전후 촬영 시퀀스 대조를 통해 실촬영일로 100% 복원.
4. **Google Photos API 대용량 벌크 업로더 & 다운로더**:
   - **Producer-Consumer Multi-threading**: 스트리밍 업로드 토큰 획득 즉시 배치 등록(`mediaItems.batchCreate`)하여 토큰 만료 방지.
   - **API 쿼터 안전 제어**: 일일 10,000건 중 9,800건 자동 임계치 제어.
   - **윈도우 소켓 프리징 방지**: 50MB 초과 대용량 파일 분리 전송.
   - **최근 사진 안전 회수 (`download_recent_from_photos.py`)**: 클라우드 정제 전 구글 포토에만 존재하는 최근 촬영본(2024~2026) 안전 선별 다운로드.

---

## 📂 저장소 구조 (Repository Structure)

```text
Photo-Organizer/
├── upload_to_photos.py              # [핵심] Google Photos v2.0 벌크 업로더 (Producer-Consumer)
├── upload_large_files.py            # 50MB 초과 대용량 파일 전용 분할 업로더
├── download_recent_from_photos.py   # [안전회수] 구글 포토 최근 사진(2024~2026) 자동 다운로더
├── audit_3stage_rotation.py         # 3단계 시각적 회전 감사 엔진 (Face + Sky + VP)
├── apply_3stage_rotation.py         # 100% 무손실 EXIF 회전 태그 바이너리 주입기
├── execute_1980_standardization.py  # 1980년 배터리 방전 사진 실촬영일 복원기
├── exifmeta.py                      # 무손실 EXIF 파싱 및 타임스탬프 유틸리티
├── refresh_token.py                 # OAuth 2.0 토큰 갱신 헬퍼
├── flush_token_ready.py             # 업로드 토큰 버퍼 즉시 플러시 도구
├── haarcascades/                    # OpenCV 얼굴/프로필 인식 사전학습 가중치 XML (17종)
├── docs/                            # 아키텍처 원장 및 상세 감사 보고서
│   ├── PHOTO_MASTER_LEDGER_2026.md  # 사진 라이브러리 통합 마스터 원장
│   └── FINAL_3STAGE_ROTATION_AUDIT_REPORT.md # 8.2만장 회전 감사 결과 보고서
├── requirements.txt                 # 실행 의존성 패키지 목록
└── README.md                        # 프로젝트 설명서
```

---

## 🛠️ 설치 및 환경 설정 (Installation)

### 1. 필수 환경
- Python 3.10 이상
- Windows 10/11 (PowerShell 권장)

### 2. 의존성 패키지 설치
```bash
git clone git@github.com:MOOK-Embedded/Photo-Organizer.git
cd Photo-Organizer
pip install -r requirements.txt
```

### 3. Google Photos API 인증 설정
1. [Google Cloud Console](https://console.cloud.google.com/)에서 프로젝트 생성 후 `Photos Library API` 활성화.
2. `OAuth 클라이언트 ID` (데스크톱 앱) 생성 후 `client_secrets.json` 파일로 다운로드하여 본 프로젝트 루트 디렉터리에 저장.

---

## 🚀 사용법 (Usage Guide)

### 1. Google Photos 최근 사진 안전 회수 (클라우드 정제 전 필수)
구글 포토에만 존재하는 최근 사진(2024~2026년)을 로컬로 안전하게 다운로드합니다.
```powershell
python download_recent_from_photos.py --since 2024-01-01
```

### 2. 3단계 시각적 회전 감사 및 무손실 교정
인물 얼굴, 하늘, 소실점을 종합 분석하여 누운 사진을 검출하고 무손실 EXIF 태그를 주입합니다.
```powershell
# 1) 전체 라이브러리 시각적 회전 감사 실행 (결과 SQLite DB 생성)
python audit_3stage_rotation.py --source "D:\Photos_Merged"

# 2) 검출된 후보군에 대해 100% 무손실 EXIF 주입 실행 (재인코딩 0%)
python apply_3stage_rotation.py --candidates "D:\_Reports\rotation_3stage_candidates.csv"
```

### 3. Google Photos 대용량 벌크 업로드
로컬의 정본 라이브러리를 구글 포토의 연도별 앨범으로 자동 업로드합니다.
```powershell
# 1) 업로드 시뮬레이션 (신규/기등록 수량 사전 점검)
python upload_to_photos.py --source "D:\Photos_Merged" --dry-run

# 2) 실제 벌크 업로드 실행 (권장 워커 2~3개)
python upload_to_photos.py --source "D:\Photos_Merged" --workers 3

# 3) 50MB 초과 대용량 동영상 분할 전송
python upload_large_files.py --source "D:\Photos_Merged"
```

---

## 🛡️ 무손실 원칙 및 데이터 안전 규약 (Safety & Integrity)

- **재인코딩 0% 보장**: 이미지는 절대 디코딩 후 다시 압축하지 않으며, EXIF 메타데이터 세그먼트만 바이너리 레벨에서 수정합니다.
- **NTFS Timestamp 보존**: 수정 시 파일의 `st_mtime`이 갱신되는 것을 방지하기 위해 `os.utime()`으로 원본 촬영 시각을 100% 동기화합니다.
- **감사 추적성(Audit Trail)**: 모든 변경 사항(파일명 변경, 회전 보정, 다운로드, 업로드)은 `D:\_Reports\`에 CSV/JSON 저널로 영구 기록됩니다.
- **보안 격리**: API 인증 토큰(`token*.json`), 클라이언트 시크릿, 로컬 SQLite 상태 DB(`*.db`)는 `.gitignore`를 통해 Git 추적에서 엄격히 차단됩니다.

---

## 📜 라이선스 (License)

This project is licensed under the MIT License - see the LICENSE file for details.
