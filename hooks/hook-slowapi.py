# hooks/hook-slowapi.py
# PyInstaller hook for slowapi (rate limiting)
from PyInstaller.utils.hooks import collect_submodules, collect_data_files

hiddenimports = collect_submodules('slowapi')
datas = collect_data_files('slowapi')
