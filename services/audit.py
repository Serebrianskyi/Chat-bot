"""The audit trail. Standing gate S6.

Every data-changing admin action writes exactly one row here, naming the actor, the action
and the target. Phase 1 builds the mechanism and its test; the first callers arrive in
Phase 3 with grant / extend / revoke (G3.9).

Do not commit inside ``record_action``: the caller owns the transaction, so the audit row
lands in the same commit as the change it describes. An action that rolls back must not
leave an audit row claiming it happened.
"""

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import AuditLog


class Action:
    """Canonical action names, so audit queries don't depend on matching free text."""

    # Phase 2A
    PAYMENT_MISSING = "payment.missing"  # due date passed unpaid; an admin was told
    PAYMENT_COMPLETE = "payment.complete"
    DISCOUNT_GRANTED = "discount.granted"
    DISCOUNT_REVOKED = "discount.revoked"
    # Phase 3
    SUBSCRIPTION_GRANTED = "subscription.granted"
    SUBSCRIPTION_EXTENDED = "subscription.extended"
    SUBSCRIPTION_REVOKED = "subscription.revoked"
    SUBSCRIPTION_EXPIRED = "subscription.expired"
    SUBSCRIPTION_CANCELLED = "subscription.cancelled"
    SUBSCRIPTION_RESUMED = "subscription.resumed"
    INVITE_SENT = "invite.sent"
    # TODO(phase-2): material.created / material.deleted / category.renamed ...
    # TODO(phase-7): broadcast.sent


async def record_action(
    session: AsyncSession,
    *,
    actor_id: int,
    action: str,
    target_user_id: int | None = None,
    details: dict[str, Any] | None = None,
) -> AuditLog:
    """Append one audit row. The caller commits.

    ``details`` is free-form JSON for whatever makes the action reconstructable later —
    the number of days granted, the old and new value, the order reference.
    """
    entry = AuditLog(
        actor_id=actor_id,
        action=action,
        target_user_id=target_user_id,
        details=details,
    )
    session.add(entry)
    await session.flush()  # assigns the id without ending the caller's transaction
    return entry


async def actions_for_target(session: AsyncSession, target_user_id: int) -> list[AuditLog]:
    """Everything ever done to one user, newest first.

    Backs "find a user, see their history" in the Phase 3 admin panel, and is how you answer
    a billing dispute.
    """
    result = await session.execute(
        select(AuditLog)
        .where(AuditLog.target_user_id == target_user_id)
        .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
    )
    return list(result.scalars().all())
