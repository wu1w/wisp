# hooks/hook-redis.py
# PyInstaller hook for redis
from PyInstaller.utils.hooks import collect_submodules, collect_data_files

hiddenimports = collect_submodules('redis')
datas = collect_data_files('redis')
