# wisp.spec
# PyInstaller spec for Wisp AI Coding Agent (Windows build)
# Usage: pyinstaller wisp.spec --clean
#
# Note: On Windows, the .so proprietary modules (Linux-only) are excluded.
# The open-source Python stubs in src/services/ will be used instead.

# -*- mode: python ; coding: utf-8 -*-

import os
import sys
from pathlib import Path

from PyInstaller.utils.hooks import (
    collect_submodules,
    collect_data_files,
)

block_cipher = None

# ── Collect hidden imports ─────────────────────────────────────────────

def _collect_unchecked(modname):
    """Safely collect submodules, return empty list on failure."""
    try:
        return collect_submodules(modname)
    except Exception:
        return []


asyncpg_hidden = _collect_unchecked('asyncpg')
redis_hidden = _collect_unchecked('redis')
minio_hidden = _collect_unchecked('minio')
docker_hidden = _collect_unchecked('docker')
uvicorn_hidden = _collect_unchecked('uvicorn')
structlog_hidden = _collect_unchecked('structlog')
sqlalchemy_hidden = _collect_unchecked('sqlalchemy')
pydantic_hidden = _collect_unchecked('pydantic')
slowapi_hidden = _collect_unchecked('slowapi')

# ── Collect data files ────────────────────────────────────────────────

def _collect_data_unchecked(modname):
    try:
        return collect_data_files(modname)
    except Exception:
        return []


asyncpg_datas = _collect_data_unchecked('asyncpg')
redis_datas = _collect_data_unchecked('redis')
minio_datas = _collect_data_unchecked('minio')
docker_datas = _collect_data_unchecked('docker')
uvicorn_datas = _collect_data_unchecked('uvicorn')
structlog_datas = _collect_data_unchecked('structlog')
sqlalchemy_datas = _collect_data_unchecked('sqlalchemy')
pydantic_datas = _collect_data_unchecked('pydantic')
slowapi_datas = _collect_data_unchecked('slowapi')

# ── Project root (where wisp.spec lives) ─────────────────────────────

ROOT = Path.cwd()

# ── Additional data files to bundle ──────────────────────────────────

config_datas = [(str(ROOT / 'config'), 'config')]
templates_datas = [(str(ROOT / 'templates'), 'templates')]

# Skills: include all manifest.yaml files
skills_src = ROOT / 'skills'
if skills_src.exists():
    skills_datas = [(str(skills_src), 'skills')]
else:
    skills_datas = []

# ── Hidden imports (core modules that PyInstaller can't auto-detect) ─

LLM_HIDDEN_IMPORTS = [
    'src.core.llm.openai',
    'src.core.llm.anthropic',
    'src.core.llm.ollama',
    'src.core.llm.factory',
    'src.core.llm.gateway',
    'src.core.llm.mock',
    'src.core.llm.embeddings',
    'src.core.llm.embeddings.base',
    'src.core.llm.embeddings.factory',
    'src.core.llm.embeddings.minimax',
    'src.core.llm.embeddings.siliconflow',
    'src.core.llm.embeddings.openai',
    'src.core.skills.adapter',
    'src.core.skills.manifest',
    'src.core.skills.registry',
    'src.core.dreaming.worker',
    'src.core.dreaming.validator',
    'src.core.agent',
    'src.core.tools',
    'src.core.prompts',
    'src.services.etl',
    'src.services.evolution',
    'src.services.memory',
    'src.services.file_versioning',
    'src.services.scheduler',
    'src.services.worker',
    'src.services.redis_streams',
    'src.services.minio_client',
    'src.api.tasks',
    'src.api.files',
    'src.api.approvals',
    'src.api.dreaming',
    'src.api.evolution',
    'src.api.webui',
    'src.utils.config',
    'src.utils.cost',
    'src.utils.facts',
    'src.utils.health',
    'src.utils.rate_limit',
    'src.utils.security',
    'src.utils.tracing',
    'src.db',
    'src.models.schemas',
    'src.models.tables',
    'src.middleware.auth',
    'alembic',
    'alembic.runtime.migration',
    'alembic.script',
    'pkg_resources.py2_warn',
    'cython',
]

# Windows doesn't support .so files — skip proprietary modules on Windows
if sys.platform == 'win32':
    PROPRIETARY_SO_GLOBS = []
else:
    PROPRIETARY_SO_GLOBS = [
        (str(ROOT / 'src' / 'core' / 'proprietary' / '*.so'), 'src/core/proprietary'),
    ]

# ── Build Analysis ───────────────────────────────────────────────────

a = Analysis(
    ['src/main.py'],
    pathex=[str(ROOT)],
    binaries=[],
    datas=(
        config_datas
        + templates_datas
        + skills_datas
        + asyncpg_datas
        + redis_datas
        + minio_datas
        + docker_datas
        + uvicorn_datas
        + structlog_datas
        + sqlalchemy_datas
        + pydantic_datas
        + slowapi_datas
        + PROPRIETARY_SO_GLOBS
    ),
    hiddenimports=(
        asyncpg_hidden
        + redis_hidden
        + minio_hidden
        + docker_hidden
        + uvicorn_hidden
        + structlog_hidden
        + sqlalchemy_hidden
        + pydantic_hidden
        + slowapi_hidden
        + LLM_HIDDEN_IMPORTS
    ),
    hookspath=[str(ROOT / 'hooks')],
    runtime_hooks=[],
    excludes=[
        'tkinter',
        'matplotlib',
        'PIL',
        'cv2',
        'torch',
        'tensorflow',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='wisp',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,  # UPX on Windows can cause issues; disable by default
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,   # Keep console for now (debugging); set False for production
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,      # Optional: add icon.ico here
)
