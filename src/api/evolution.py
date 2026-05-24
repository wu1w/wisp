"""
Evolution Engine API — 查看/审批 Prompt 进化提案。

GET  /v1/evolution/proposals     — 待审批的提案列表
GET  /v1/evolution/proposals/:id  — 单个提案详情
POST /v1/evolution/proposals/:id/approve  — 审批通过
POST /v1/evolution/proposals/:id/reject   — 审批拒绝
POST /v1/evolution/proposals/:id/apply    — 应用已审批提案
GET  /v1/evolution/stats         — 当前统计数据
POST /v1/evolution/trigger       — 手动触发提案生成（条件满足时）
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from src.services.evolution import evolution_engine

router = APIRouter(tags=["evolution"])


def _proposal_to_dict(p: Any) -> dict[str, Any]:
    return {
        "proposal_id": p.proposal_id,
        "created_at": p.created_at.isoformat() if p.created_at else None,
        "target_version": p.target_version,
        "new_version": p.new_version,
        "summary": p.summary,
        "approved": p.approved,
        "approved_at": p.approved_at.isoformat() if p.approved_at else None,
        "applied_at": p.applied_at.isoformat() if p.applied_at else None,
        "analyses": [
            {
                "segment_name": a.segment_name,
                "sample_size": a.sample_size,
                "success_rate": a.success_rate,
                "avg_tool_calls": a.avg_tool_calls,
                "avg_latency": a.avg_latency,
                "issues": a.issues,
            }
            for a in (p.analyses or [])
        ],
        "changes": [
            {
                "segment_name": c.segment_name,
                "before_excerpt": c.before_excerpt,
                "after_excerpt": c.after_excerpt,
                "reason": c.reason,
            }
            for c in (p.changes or [])
        ],
    }


@router.get("/proposals")
async def list_proposals(approved: bool | None = None) -> dict[str, Any]:
    """
    列出提案。

    approved=None: 所有提案
    approved=true: 仅已通过
    approved=false: 仅已拒绝
    """
    if approved is None:
        proposals = await evolution_engine.get_pending_proposals()
        # Also include approved/rejected
        all_proposals = await evolution_engine.get_pending_proposals()  # fetch all from DB
        # Use the load_proposals directly
        from src.services.evolution import _load_proposals
        all_proposals = await _load_proposals(approved=None)
        return {"proposals": [_proposal_to_dict(p) for p in all_proposals]}
    else:
        from src.services.evolution import _load_proposals
        proposals = await _load_proposals(approved=approved)
        return {"proposals": [_proposal_to_dict(p) for p in proposals]}


@router.get("/proposals/{proposal_id}")
async def get_proposal(proposal_id: str) -> dict[str, Any]:
    from src.services.evolution import _load_proposals
    all_proposals = await _load_proposals(approved=None)
    proposal = next((p for p in all_proposals if p.proposal_id == proposal_id), None)
    if not proposal:
        raise HTTPException(status_code=404, detail="Proposal not found")
    return _proposal_to_dict(proposal)


@router.post("/proposals/{proposal_id}/approve")
async def approve_proposal(proposal_id: str) -> dict[str, Any]:
    success = await evolution_engine.approve_proposal(proposal_id)
    if not success:
        raise HTTPException(status_code=404, detail="Proposal not found")
    return {"ok": True, "proposal_id": proposal_id, "approved": True}


@router.post("/proposals/{proposal_id}/reject")
async def reject_proposal(proposal_id: str) -> dict[str, Any]:
    success = await evolution_engine.reject_proposal(proposal_id)
    if not success:
        raise HTTPException(status_code=404, detail="Proposal not found")
    return {"ok": True, "proposal_id": proposal_id, "approved": False}


@router.post("/proposals/{proposal_id}/apply")
async def apply_proposal(proposal_id: str) -> dict[str, Any]:
    success = await evolution_engine.apply_proposal(proposal_id)
    if not success:
        raise HTTPException(status_code=400, detail="Cannot apply proposal")
    return {"ok": True, "proposal_id": proposal_id}


@router.get("/stats")
async def get_stats() -> dict[str, Any]:
    return await evolution_engine.get_outcome_stats()


@router.post("/trigger")
async def trigger_proposal() -> dict[str, Any]:
    """
    手动触发提案生成。
    样本不足时返回提示。
    """
    proposal = await evolution_engine.propose_evolution()
    if not proposal:
        stats = await evolution_engine.get_outcome_stats()
        return {
            "triggered": False,
            "message": f"样本不足，需要 >=10 条记录，当前 {stats.get('total_records', 0)} 条",
        }
    return {"triggered": True, "proposal_id": proposal.proposal_id}
