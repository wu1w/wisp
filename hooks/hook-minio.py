# hooks/hook-minio.py
# PyInstaller hook for minio (S3-compatible object storage)
from PyInstaller.utils.hooks import collect_submodules, collect_data_files

hiddenimports = collect_submodules('minio')
datas = collect_data_files('minio')
