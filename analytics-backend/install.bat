@echo off
echo === Kapi Backend Install (Python 3.14 / Windows) ===
echo.

:: Step 1: Core packages
echo [1/3] Installing core packages...
pip install fastapi==0.111.0 uvicorn[standard]==0.29.0 python-multipart==0.0.9 ^
    pydantic==2.7.1 pydantic-settings==2.2.1 ^
    sqlalchemy==2.0.30 aiosqlite==0.20.0 ^
    pandas==3.0.1 numpy scipy ^
    anthropic openai ^
    jinja2 httpx aiofiles python-dateutil ^
    playwright pytest pytest-asyncio

:: Step 2: PyTorch (CPU) — must use PyTorch index for Python 3.14 wheels
echo.
echo [2/3] Installing PyTorch CPU (from PyTorch index)...
pip install torch --index-url https://download.pytorch.org/whl/cpu

:: Step 3: RAG packages (depend on torch)
echo.
echo [3/3] Installing RAG packages...
pip install sentence-transformers faiss-cpu

:: Step 4: Playwright browser
echo.
echo [4/4] Installing Playwright Chromium (for ChatGPT browser login)...
playwright install chromium

echo.
echo === Install complete! ===
echo Run: python main.py
pause
