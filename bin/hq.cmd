@echo off
rem hq Windows entry: locate bin/hq.py via %~dp0 and pick a Python >= 3.11.
rem Keeps the caller working directory and exit code. ASCII only: cmd parses
rem this file in the OEM codepage, which is not necessarily UTF-8.
rem Interpreter executable and launcher args are kept separate so quoted
rem paths with spaces survive; delayed expansion is forced off so a parent
rem shell cannot corrupt paths containing "!". No CALL on the interpreter:
rem CALL re-expands percent sequences inside business arguments (verified
rem regression), and per design HQ_PYTHON only accepts a real interpreter.
rem Line endings MUST stay CRLF, and substring syntax like VAR:~-4 must only
rem run on lines reached after the variable is known defined (goto flow):
rem inside a parenthesized block cmd percent-expands the whole block before
rem "if defined" executes, and an undefined variable there mangles the line
rem into "The syntax of the command is incorrect."
setlocal DisableDelayedExpansion EnableExtensions
set "HQ_BOOT=%~dp0hq.py"
set "HQ_RUN="
set "HQ_ARGS="
set "HQ_PROBE=import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)"

if not defined HQ_PYTHON goto :pick_fallback
set "HQ_EXT=%HQ_PYTHON:~-4%"
if /i "%HQ_EXT%"==".bat" goto :bad_py
if /i "%HQ_EXT%"==".cmd" goto :bad_py
"%HQ_PYTHON%" -c "%HQ_PROBE%" >nul 2>&1 && set "HQ_RUN=%HQ_PYTHON%"
:pick_fallback
if defined HQ_RUN goto :run
py -3 -c "%HQ_PROBE%" >nul 2>&1 && (set "HQ_RUN=py" & set "HQ_ARGS=-3")
if defined HQ_RUN goto :run
python -c "%HQ_PROBE%" >nul 2>&1 && set "HQ_RUN=python"
if defined HQ_RUN goto :run
echo hq: Python 3.11+ required. Install Python or set HQ_PYTHON to its full path. 1>&2
goto :no_python

:run
"%HQ_RUN%" %HQ_ARGS% -X utf8 "%HQ_BOOT%" %*
endlocal & exit /b %errorlevel%

:bad_py
echo hq: HQ_PYTHON must point to a real interpreter executable (.exe), not a .bat/.cmd wrapper - CALL would re-expand %% in arguments. 1>&2
goto :no_python

:no_python
rem Old gate/hook entries keep the JSON contract even with no Python installed:
rem stdout {} and exit 0, no business. Any other action fails explicitly above.
if /i "%~1"=="gate" goto :hook_json
if /i "%~1"=="hook" goto :hook_json
exit /b 1
:hook_json
echo {}
exit /b 0
