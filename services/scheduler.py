"""Running the billing jobs on a schedule. Phase 2A.

APScheduler in-process, which the plan allows before Phase 4 ("APScheduler acceptable early").
The jobs live and die with the bot: there is no server, so nothing runs while the laptop is off.

That is safe because **every job is a comparison against a stored date, not a timer**. A due date
that passed while the bot was down is still in the past when it starts again, so the next run
catches up. Nothing is lost by missing a window.

``coalesce=True`` and ``max_instances=1`` matter for the same reason: after a long downtime
APScheduler would otherwise try to replay every window it missed, and run overlapping copies of
a job that is already slow because it is talking to WayForPay.
"""

import logging

from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy.ext.asyncio import async_sessionmaker

from config import Settings
from services.billing import (
    BillingConfig,
    poll_open_payments,
    process_due_subscriptions,
    retry_missing_invites,
)
from services.wayforpay import WayForPayClient

log = logging.getLogger(__name__)

#: How often to ask WayForPay about invoices that have not settled.
POLL_INTERVAL_MINUTES = 2

#: Daily sweep. 9am local is deliberate: a payment request should not arrive at 3am, and an
#: admin alert is more likely to be acted on during the day.
DUE_JOB_HOUR = 9

#: Daily check that paid members actually got into the channel. An hour after the due-date job,
#: so a payment confirmed in the morning has had its invite attempted by the poller first and
#: this does not duplicate it.
INVITE_RETRY_HOUR = 10


def build_client(settings: Settings) -> WayForPayClient | None:
    """The WayForPay client, or None when it is not configured.

    Returning None rather than raising lets the bot run for onboarding alone — useful while the
    merchant account is still being set up.
    """
    if not settings.wayforpay_configured:
        return None
    return WayForPayClient(
        merchant_account=str(settings.wayforpay_merchant_account),
        merchant_domain=str(settings.wayforpay_merchant_domain),
        # Unwrapped here, at the single point of use, and never stored anywhere else.
        secret_key=settings.wayforpay_secret_key.get_secret_value(),  # type: ignore[union-attr]
    )


def build_billing_config(settings: Settings) -> BillingConfig:
    from datetime import timedelta

    return BillingConfig(
        period_days=settings.subscription_period_days,
        grace=timedelta(days=settings.subscription_grace_days),
        display_timezone=settings.display_timezone,
        channel_id=settings.channel_id,
    )


def start_scheduler(
    *,
    settings: Settings,
    session_factory: async_sessionmaker,
    bot: Bot,
) -> AsyncIOScheduler | None:
    """Start the billing jobs. Returns None when WayForPay is not configured.

    A missing merchant account is logged once, loudly, rather than failing per subscriber later.
    """
    client = build_client(settings)
    if client is None:
        log.warning(
            "WayForPay is not configured (WAYFORPAY_MERCHANT_ACCOUNT / _MERCHANT_DOMAIN / "
            "_SECRET_KEY). Onboarding works; no invoices will be created and nobody will be "
            "chased for payment."
        )
        return None

    if settings.subscription_price <= 0:
        log.warning(
            "SUBSCRIPTION_PRICE is %s. Invoices would be created for nothing, so the billing "
            "jobs are not being started.",
            settings.subscription_price,
        )
        return None

    config = build_billing_config(settings)
    admin_ids = settings.admin_id_set
    scheduler = AsyncIOScheduler(timezone=settings.display_timezone)

    scheduler.add_job(
        poll_open_payments,
        "interval",
        minutes=POLL_INTERVAL_MINUTES,
        id="poll_open_payments",
        kwargs={
            "session_factory": session_factory,
            "client": client,
            "bot": bot,
            "config": config,
            # Needed so an invite failure can be escalated rather than lost.
            "admin_ids": admin_ids,
        },
        coalesce=True,
        max_instances=1,
    )
    scheduler.add_job(
        process_due_subscriptions,
        "cron",
        hour=DUE_JOB_HOUR,
        minute=0,
        id="process_due_subscriptions",
        kwargs={
            "session_factory": session_factory,
            "client": client,
            "bot": bot,
            "admin_ids": admin_ids,
            "config": config,
        },
        coalesce=True,
        max_instances=1,
    )

    scheduler.add_job(
        retry_missing_invites,
        "cron",
        hour=INVITE_RETRY_HOUR,
        minute=0,
        id="retry_missing_invites",
        kwargs={
            "session_factory": session_factory,
            "bot": bot,
            "channel_id": config.channel_id,
            "admin_ids": admin_ids,
        },
        coalesce=True,
        max_instances=1,
    )

    scheduler.start()
    log.info(
        "Billing jobs started: payments polled every %s min, due dates checked daily at "
        "%s:00, missing invites retried daily at %s:00 %s",
        POLL_INTERVAL_MINUTES,
        DUE_JOB_HOUR,
        INVITE_RETRY_HOUR,
        settings.display_timezone,
    )
    return scheduler
