"""
WebUI 路由 — 任务仪表盘。

提供简单的人机界面查看任务状态、提交新任务。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import structlog
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from src.db import acquire

logger = structlog.get_logger(__name__)
router = APIRouter()
from pathlib import Path

_TEMPLATE_DIR = str(Path(__file__).parent.parent.parent / "templates" / "webui")
templates = Jinja2Templates(directory=_TEMPLATE_DIR)


# ── 状态映射 ────────────────────────────────────────────────────

_STATE_LABEL: dict[str, str] = {
    "IDLE": "🟡 等待中",
    "THINKING": "🔵 思考中",
    "TOOL_CALLING": "🟠 工具调用",
    "AWAITING_APPROVAL": "🟣 等待审批",
    "DONE": "✅ 完成",
    "FAILED": "❌ 失败",
    "HIBERNATING": "💤 休眠",
    "PANIC": "🚨 恐慌",
}


def _label(state: str | None) -> str:
    if state is None:
        return "⚪ 未知"
    return _STATE_LABEL.get(state, f"🔘 {state}")


def _age(dt_str: str | None) -> str:
    """返回相对时间字符串。"""
    if dt_str is None:
        return "—"
    try:
        dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
        now = datetime.now(UTC)
        delta = (now - dt).total_seconds()
        if delta < 60:
            return f"{int(delta)}秒前"
        if delta < 3600:
            return f"{int(delta / 60)}分钟前"
        if delta < 86400:
            return f"{int(delta / 3600)}小时前"
        return f"{int(delta / 86400)}天前"
    except Exception:
        return dt_str


# ── 页面路由 ────────────────────────────────────────────────────


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request) -> HTMLResponse:
    """
    任务仪表盘主页。
    显示最近任务列表和系统状态摘要。
    """
    async with acquire() as conn:
        # 任务统计
        stats = await conn.fetch(
            "SELECT status, current_state, COUNT(*) as count FROM tasks GROUP BY status, current_state"
        )
        total = sum(r["count"] for r in stats)

        # 最近任务（最近 50 条）
        rows = await conn.fetch(
            """
            SELECT id, description, user_id, status, current_state,
                   tool_call_count, max_tool_calls, created_at, updated_at
            FROM tasks
            ORDER BY created_at DESC
            LIMIT 50
            """
        )
        tasks = [
            {
                "id": str(r["id"]),
                "description": (r["description"] or "")[:60],
                "user": r["user_id"],
                "status": r["status"],
                "state": _label(r["current_state"]),
                "calls": f"{r['tool_call_count']}/{r['max_tool_calls']}",
                "age": _age(str(r["created_at"])),
            }
            for r in rows
        ]

    # 状态统计
    stat_map: dict[str, int] = {}
    for r in stats:
        key = r["status"]
        stat_map[key] = stat_map.get(key, 0) + r["count"]

    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "total": total,
            "stat_map": stat_map,
            "tasks": tasks,
        },
    )


@router.get("/tasks/{task_id}", response_class=HTMLResponse)
async def task_detail(task_id: str, request: Request) -> HTMLResponse:
    """任务详情页：包含步骤历史和输出。"""
    try:
        uuid.UUID(task_id)
    except ValueError:
        return HTMLResponse("<h1>无效的任务 ID</h1>", status_code=400)

    async with acquire() as conn:
        task = await conn.fetchrow(
            "SELECT * FROM tasks WHERE id = $1", uuid.UUID(task_id)
        )
        if not task:
            return HTMLResponse("<h1>任务不存在</h1>", status_code=404)

        steps = await conn.fetch(
            """
            SELECT id, seq, state, tool_name, input_args, output, error,
                   attempt, max_attempts, created_at, updated_at, heartbeat_at
            FROM task_steps
            WHERE task_id = $1
            ORDER BY seq ASC
            """,
            uuid.UUID(task_id),
        )

    task_dict = dict(task)
    step_list = []
    for s in steps:
        sd = dict(s)
        # 截断输出
        out = sd.get("output")
        err = sd.get("error")
        if isinstance(out, dict):
            out_str = str(out)[:500]
        elif isinstance(out, str):
            out_str = out[:500]
        else:
            out_str = str(out) if out else ""

        step_list.append({
            "id": str(sd["id"]),
            "seq": sd["seq"],
            "state": sd["state"].upper() if sd["state"] else "?",
            "tool": sd["tool_name"] or "—",
            "attempt": f"{sd['attempt']}/{sd['max_attempts']}",
            "output": out_str,
            "error": (str(err)[:200] if err else "") if sd["state"] == "dead" else "",
            "heartbeat": _age(str(sd["heartbeat_at"])) if sd["heartbeat_at"] else "—",
            "age": _age(str(sd["created_at"])),
        })

    return templates.TemplateResponse(
        "task_detail.html",
        {
            "request": request,
            "task": {
                "id": str(task_dict["id"]),
                "description": task_dict["description"] or "—",
                "user": task_dict["user_id"],
                "status": task_dict["status"],
                "state": _label(task_dict["current_state"]),
                "calls": f"{task_dict['tool_call_count']}/{task_dict['max_tool_calls']}",
                "age": _age(str(task_dict["created_at"])),
            },
            "steps": step_list,
        },
    )


@router.get("/new", response_class=HTMLResponse)
async def new_task_page(request: Request) -> HTMLResponse:
    """新建任务页面。"""
    return templates.TemplateResponse(
        "new_task.html",
        {"request": request},
    )


@router.post("/submit")
async def submit_task(request: Request) -> RedirectResponse:
    """
    接收表单提交，创建任务。
    不需要认证（内部使用）。
    """
    form = await request.form()
    desc_raw = form.get("description", "")
    user_raw = form.get("user_id", "anonymous")
    description = (desc_raw if isinstance(desc_raw, str) else "").strip()
    user_id = (user_raw if isinstance(user_raw, str) else "anonymous").strip() or "anonymous"

    if not description:
        return RedirectResponse("/new?error=description_required", status_code=302)

    task_id = str(uuid.uuid4())
    async with acquire() as conn:
        await conn.execute(
            """
            INSERT INTO tasks (id, description, user_id, status, current_state,
                               max_tool_calls, tool_call_count, prompt_version)
            VALUES ($1, $2, $3, 'pending', 'IDLE', 50, 0, 'v1.0.0')
            """,
            uuid.UUID(task_id),
            description,
            user_id,
        )
        step_id = str(uuid.uuid4())
        await conn.execute(
            """
            INSERT INTO task_steps
                (id, task_id, seq, state, attempt, max_attempts, ttl_seconds)
            VALUES ($1, $2, 0, 'pending', 1, 3, 300)
            """,
            uuid.UUID(step_id),
            uuid.UUID(task_id),
        )

    # 通知 Worker 有新任务
    try:
        from src.services.redis_streams import stream_queue
        await stream_queue.publish_step(
            step_id=step_id,
            task_id=task_id,
            seq=0,
        )
    except Exception:
        pass  # Redis 不可用时 Worker 会通过 DB 轮询发现

    return RedirectResponse(f"/tasks/{task_id}", status_code=302)
