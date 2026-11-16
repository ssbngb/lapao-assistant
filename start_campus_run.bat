@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
echo ============================================================
echo         步道乐跑 · 校园定向打卡全自动跑
echo ============================================================
echo.
echo [说明] 脚本将全自动处理：
echo   1. 自动识别任意随机打卡点并动态导航打卡 (共2个点)
echo   2. 实时闭环调节速度，确保最终平均配速严格锁定在 6'00" (合格区间)
echo   3. 跑满 2.05 公里且确认双打卡合格后，自动长按结束并上传成绩
echo.
"C:\Python312\python.exe" -u campus_run.py --pace 6.0 --km 2.05 --cps 2 --auto-start --auto-finish
pause
