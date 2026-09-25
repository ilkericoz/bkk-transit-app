@echo off
rem Daily BKK timetable update - started by the Windows scheduled task
rem "BKK timetable update" (see update_gtfs.py). Output goes to the log.
cd /d "%~dp0.."
".venv\Scripts\python.exe" scripts\update_gtfs.py >> "..\bkk-backend\gtfs-data\update_task_output.txt" 2>&1
