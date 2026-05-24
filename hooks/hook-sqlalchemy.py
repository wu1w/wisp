# hooks/hook-sqlalchemy.py
# PyInstaller hook for sqlalchemy (with asyncio support)
from PyInstaller.utils.hooks import collect_submodules, collect_data_files

hiddenimports = collect_submodules('sqlalchemy')
datas = collect_data_files('sqlalchemy')
