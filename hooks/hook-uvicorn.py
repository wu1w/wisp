# hooks/hook-uvicorn.py
# PyInstaller hook for uvicorn (ASGI server)
from PyInstaller.utils.hooks import collect_submodules, collect_data_files

hiddenimports = collect_submodules('uvicorn')
datas = collect_data_files('uvicorn')
