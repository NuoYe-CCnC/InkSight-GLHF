@echo off
REM InkSight 后端一键启动（Windows）
REM 用法：双击，或命令行：run-backend.bat [port]
setlocal
cd /d "%~dp0backend"
if not exist .venv\Scripts\python.exe (
    echo [1/4] 创建虚拟环境...
    py -3 -m venv .venv 2>nul || python -m venv .venv
)
echo [2/4] 安装依赖（首次较慢）...
call .venv\Scripts\activate.bat
pip install --no-compile -r requirements.txt
if not exist .env (
    copy .env.example .env >nul
    echo [提示] 已生成 .env，请编辑填入 DEEPSEEK_API_KEY 与 SYNC_TOKEN 后重新运行
)
set PORT=%1
if "%PORT%"=="" set PORT=8080
echo [3/4] 启动后端 0.0.0.0:%PORT% （Ctrl+C 停止）
echo [4/4] 校验：浏览器打开 http://127.0.0.1:%PORT%/api/modes
python -m uvicorn api.index:app --host 0.0.0.0 --port %PORT%
endlocal
