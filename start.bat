@echo off
chcp 65001 > nul
echo.
echo ============================================================
echo   Google Photos 업로더 - 환경 설치 및 첫 실행
echo ============================================================
echo.

:: Python 확인
python --version 2>nul
if errorlevel 1 (
    echo [오류] Python이 설치되지 않았습니다.
    echo https://www.python.org/downloads/ 에서 설치하세요.
    pause
    exit /b 1
)

:: client_secrets.json 확인
if not exist "%~dp0client_secrets.json" (
    echo.
    echo [오류] client_secrets.json 파일이 없습니다.
    echo.
    echo setup_guide.md 파일을 읽고 Google Cloud Console에서
    echo OAuth 클라이언트 JSON을 다운로드하여 이 폴더에 저장하세요:
    echo %~dp0client_secrets.json
    echo.
    start "" "%~dp0setup_guide.md"
    pause
    exit /b 1
)

:: 가상환경 생성 (없으면)
if not exist "%~dp0venv" (
    echo [설치] 가상환경 생성 중...
    python -m venv "%~dp0venv"
)

:: 패키지 설치
echo [설치] 패키지 설치 중...
call "%~dp0venv\Scripts\activate.bat"
pip install -r "%~dp0requirements.txt" -q

echo.
echo [완료] 환경 설치 완료!
echo.
echo ============================================================
echo   다음 단계를 선택하세요:
echo ============================================================
echo.
echo   1. 테스트 실행 (특정 폴더, 10~20개)
echo   2. 전체 스캔만 (업로드 없음, 파일 수 확인)
echo   3. 전체 업로드 시작
echo   4. 종료
echo.

set /p choice="선택 (1~4): "

if "%choice%"=="1" (
    set /p testpath="테스트 폴더 경로 입력: "
    python "%~dp0upload_to_photos.py" --test-folder "%testpath%"
) else if "%choice%"=="2" (
    python "%~dp0upload_to_photos.py" --dry-run
) else if "%choice%"=="3" (
    python "%~dp0upload_to_photos.py"
) else (
    echo 종료합니다.
)

pause
