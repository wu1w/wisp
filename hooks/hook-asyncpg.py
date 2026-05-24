# hooks/hook-asyncpg.py
# PyInstaller hook for asyncpg (async PostgreSQL driver)
from PyInstaller.utils.hooks import collect_submodules, collect_data_files

hiddenimports = collect_submodules('asyncpg')
datas = collect_data_files('asyncpg')
