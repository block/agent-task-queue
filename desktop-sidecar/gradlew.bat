@echo off
set SCRIPT_DIR=%~dp0
call "%SCRIPT_DIR%..\intellij-plugin\gradlew.bat" -p "%SCRIPT_DIR%" %*
