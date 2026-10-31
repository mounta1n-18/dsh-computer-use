@echo off
REM 用**独立 user-data-dir** 启动一个带 CDP 调试端口的 Chrome。
REM
REM 为什么不能直接给正在用的 Chrome 加 --remote-debugging-port：
REM   Chrome 136 起，默认 profile 上开调试端口会被拒绝（安全策略收紧）。
REM   唯一可靠的姿势是：新起一个实例 + 独立的 user-data-dir。
REM   代价是这个实例看不到你原来登录态里的标签页；需要登录的站点要重新登一次。
REM
REM 用法：
REM   scripts\chrome-debug.cmd            默认 9333 端口
REM   scripts\chrome-debug.cmd 9444       指定端口
REM 之后在 DSH 里调 browser_use { action: "status" } 应该能扫到端点。

setlocal
set PORT=%~1
if "%PORT%"=="" set PORT=9333
set PROFILE=%LOCALAPPDATA%\dsh-computer-use\chrome-profile

set CHROME=
if exist "%ProgramFiles%\Google\Chrome\Application\chrome.exe" set CHROME=%ProgramFiles%\Google\Chrome\Application\chrome.exe
if not defined CHROME if exist "%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe" set CHROME=%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe
if not defined CHROME if exist "%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe" set CHROME=%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe

if not defined CHROME (
  echo [chrome-debug] 没找到 chrome.exe，先装 Chrome 或手动改这里的路径。
  exit /b 1
)

if not exist "%PROFILE%" mkdir "%PROFILE%"

echo [chrome-debug] chrome = %CHROME%
echo [chrome-debug] port   = %PORT%
echo [chrome-debug] profile= %PROFILE%
echo [chrome-debug] 起来之后在 DSH 里调 browser_use { action: "status" } 确认。
start "" "%CHROME%" --remote-debugging-port=%PORT% --user-data-dir="%PROFILE%" --no-first-run --no-default-browser-check --disable-extensions about:blank
endlocal
