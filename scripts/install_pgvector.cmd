@echo off
echo ===================================================
echo  Installing pgvector for PostgreSQL 17 on Windows
echo ===================================================
echo.

set "SRC=C:\Users\Alan\.gemini\antigravity\brain\dfe5f73c-7d56-4f85-8690-ed2bcf0c1ccc\scratch\pgvector-extracted"
set "PGTARGET=C:\Program Files\PostgreSQL\17"

echo Copying vector.dll...
copy /Y "%SRC%\lib\vector.dll" "%PGTARGET%\lib\"
if errorlevel 1 goto ERROR

echo Copying extension control and SQL scripts...
copy /Y "%SRC%\share\extension\*" "%PGTARGET%\share\extension\"
if errorlevel 1 goto ERROR

if exist "%SRC%\include\server\extension" (
    echo Copying extension headers...
    if not exist "%PGTARGET%\include\server\extension\vector" mkdir "%PGTARGET%\include\server\extension\vector"
    copy /Y "%SRC%\include\server\extension\vector\*" "%PGTARGET%\include\server\extension\vector\"
)

echo.
echo Restarting PostgreSQL 17 service...
net stop postgresql-x64-17
net start postgresql-x64-17

echo.
echo ===================================================
echo  pgvector installation completed successfully!
echo ===================================================
pause
exit /b 0

:ERROR
echo.
echo ERROR: Installation failed. Please ensure you ran this script as Administrator.
echo Right-click this file and select "Run as administrator".
pause
exit /b 1
