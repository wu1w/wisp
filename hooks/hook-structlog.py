# hooks/hook-structlog.py
# PyInstaller hook for structlog
from PyInstaller.utils.hooks import collect_submodules, collect_data_files

hiddenimports = collect_submodules('structlog')
datas = collect_data_files('structlog')
