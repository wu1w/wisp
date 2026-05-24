# hooks/hook-pydantic.py
# PyInstaller hook for pydantic (required by many modules)
from PyInstaller.utils.hooks import collect_submodules, collect_data_files

hiddenimports = collect_submodules('pydantic')
datas = collect_data_files('pydantic')
