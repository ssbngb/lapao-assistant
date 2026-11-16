@echo off
chcp 65001 >nul
cd /d "D:\步道乐跑"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
echo [%date% %time%] 开始执行单次下午正式课外跑任务... > "D:\步道乐跑\afternoon_run_20261111.log"
"C:\Python312\python.exe" -u "D:\步道乐跑\campus_run.py" --pace 6.0 --km 2.05 --cps 2 --auto-start --auto-finish >> "D:\步道乐跑\afternoon_run_20261111.log" 2>&1
echo [%date% %time%] 任务结束，退出码=%errorlevel% >> "D:\步道乐跑\afternoon_run_20261111.log"
