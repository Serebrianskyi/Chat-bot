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
from zoneinfo import ZoneInfo

from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy.ext.asyncio import async_sessionmaker

from config import Settings
from services.billing import (
    BillingConfig,
    poll_open_payments,
    process_due_subscriptions,
    purge_blocked_users,
    recover_access,
    remove_lapsed_members,
    send_renewal_reminders,
)
from services.wayforpay import WayForPayClient

log = logging.getLogger(__name__)

#: How often to ask WayForPay about invoices that have not settled, in seconds.
#:
#: 60s rather than the original 120s, owner's decision 2026-10-28: it halves the worst-case wait
#: between a member paying and getting their channel link. It is a fallback now, not the primary
#: path — the `serviceUrl` callback confirms a payment in about a second — but it still covers
#: every invoice issued before webhook mode (those carry no callback URL) and any callback that
#: is lost.
#:
#: The cost is one extra CHECK_STATUS per open invoice per minute. Invoices expire after two
#: hours, so the query is small; going much below this would be paying real request volume to
#: shave a path that should rarely be the one used.
POLL_INTERVAL_SECONDS = 60

#: Daily sweep. 9am local is deliberate: a payment request should not arrive at 3am, and an
#: admin alert is more likely to be acted on during the day.
DUE_JOB_HOUR = 9

#: Daily sweep for members who paid and are not in the channel: reconcile the orders this bot
#: wrote off, then get a link to whoever is owed one.
#:
#: Daily and not at startup. It used to run on boot, which meant a day of deployments sent an
#: admin the same report over and over; `recover_access` also refuses to repeat inside
#: `RECOVERY_MIN_INTERVAL`, so the spacing holds even if something else calls it.
RECOVERY_HOUR = 11

#: **The admin's own clock, not the members'.** This sweep's only output is a report an admin
#: reads, and the owner is in Poland, so it is scheduled in Warsaw time while everything
#: member-facing stays on `display_timezone` (Kyiv). Written as a zone rather than an offset so
#: it stays 11:00 local across both countries' daylight-saving changes, which fall on the same
#: dates but leave Kyiv an hour ahead all year.
RECOVERY_TIMEZONE = "Europe/Warsaw"


def service_url(settings: Settings) -> str | None:
    """Where WayForPay should post payment results, or None while the bot polls.

    Only meaningful with a public HTTPS base URL. Sending a `serviceUrl` the gateway cannot reach
    would have it retrying into the void, so it is omitted entirely in polling mode.
    """
    if settings.mode != "webhook" or not settings.base_url:
        return None
    from web.routes import PAYMENTS_CALLBACK_PATH

    return settings.base_url.rstrip("/") + PAYMENTS_CALLBACK_PATH


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
        # Where payment results are posted. None while polling — see `service_url`.
        service_url=service_url(settings),
        # Only `regularApi` needs this, and it is optional: a schedule can be created without it
        # and merely cannot be cancelled through the API.
        merchant_password=(
            settings.wayforpay_merchant_password.get_secret_value()
            if settings.wayforpay_merchant_password
            else None
        ),
    )


def _warn_if_renewal_is_inert(settings: Settings) -> None:
    """Say so, loudly, when links will go out as one-offs despite auto-renew being on.

    This is the quiet failure: a `run_jobs due` from a shell where `MODE` is not set gets no
    `serviceUrl`, so every link it hands out renews nothing — and the members who received them
    look fine until their access lapses a month later.
    """
    if settings.auto_renew_enabled and service_url(settings) is None:
        log.warning(
            "AUTO_RENEW_ENABLED is on but there is no serviceUrl (MODE=%s, BASE_URL=%s): every "
            "payment link will be a ONE-OFF and will not renew. Set MODE=webhook and BASE_URL.",
            settings.mode,
            settings.base_url or "unset",
        )


def build_billing_config(settings: Settings) -> BillingConfig:
    from datetime import timedelta

    _warn_if_renewal_is_inert(settings)
    return BillingConfig(
        period_days=settings.subscription_period_days,
        grace=timedelta(days=settings.subscription_grace_days),
        display_timezone=settings.display_timezone,
        channel_id=settings.channel_id,
        # A schedule charges the card whether or not anything is listening, so it is only created
        # when the gateway has somewhere to report each charge — i.e. webhook mode with a public
        # BASE_URL. `service_url` returning None is exactly that condition.
        auto_renew=settings.auto_renew_enabled and service_url(settings) is not None,
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

    if settings.free_period_until is None:
        # Not a crash — after the offer closes, unset is the correct value. But while it is
        # supposed to be open, unset silently reinstates the behaviour the design rejected:
        # `first_expiry` falls back to "one period from today", so each member gets their own
        # end date instead of the one shared date. A founding member who joined on 7 October was
        # given 6 November instead of 1 November exactly this way (2026-10-21).
        log.warning(
            "FREE_PERIOD_UNTIL is not set. Free first periods will run 30 days from each "
            "member's own /start rather than ending on one shared date. If the offer is still "
            "open, set it — see docs/plan-2026-10-20.md."
        )

    config = build_billing_config(settings)
    admin_ids = settings.admin_id_set
    scheduler = AsyncIOScheduler(timezone=settings.display_timezone)

    scheduler.add_job(
        poll_open_payments,
        "interval",
        seconds=POLL_INTERVAL_SECONDS,
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
    # `hour="*"` under ENABLE_HOURLY_JOBS, so the two member-facing jobs can be watched without
    # waiting for tomorrow morning. Both are idempotent within a period, so running them twelve
    # times a day changes what a member receives not at all — only how soon.
    daily_hour: int | str = "*" if settings.enable_hourly_jobs else DUE_JOB_HOUR

    scheduler.add_job(
        process_due_subscriptions,
        "cron",
        hour=daily_hour,
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
        send_renewal_reminders,
        "cron",
        hour=daily_hour,
        minute=30,
        id="send_renewal_reminders",
        kwargs={
            "session_factory": session_factory,
            "client": client,
            "bot": bot,
            "config": config,
        },
        coalesce=True,
        max_instances=1,
    )
    scheduler.add_job(
        remove_lapsed_members,
        "cron",
        hour=DUE_JOB_HOUR,
        minute=45,
        id="remove_lapsed_members",
        kwargs={
            "session_factory": session_factory,
            "bot": bot,
            # Needed to stop a gateway-side schedule before showing somebody the door: a charge
            # that lands after the removal takes money for a channel they are no longer in.
            "client": client,
            "channel_id": config.channel_id,
            "admin_ids": admin_ids,
            "config": config,
            # Off unless the environment says otherwise. With it off the job reports and
            # changes nothing, which is how the list gets checked (G3.8).
            "enabled": settings.removal_enabled,
        },
        coalesce=True,
        max_instances=1,
    )
    scheduler.add_job(
        recover_access,
        "cron",
        hour=RECOVERY_HOUR,
        minute=0,
        timezone=ZoneInfo(RECOVERY_TIMEZONE),
        id="recover_access",
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

    # Monthly, on the 1st: delete members who have blocked the bot, read from `is_blocked_bot`,
    # which the my_chat_member handler keeps current. No API calls and no probing — nothing about
    # this needs to be prompt, a blocked member costing nothing but a row.
    # Fifteen minutes after the recovery sweep so the two reports do not arrive together.
    scheduler.add_job(
        purge_blocked_users,
        "cron",
        day=1,
        hour=RECOVERY_HOUR,
        minute=15,
        timezone=ZoneInfo(RECOVERY_TIMEZONE),
        id="purge_blocked_users",
        kwargs={
            "session_factory": session_factory,
            "bot": bot,
            "admin_ids": admin_ids,
        },
        coalesce=True,
        max_instances=1,
    )

    scheduler.start()
    if settings.enable_hourly_jobs:
        # Loud, because it is a test setting that costs twelve times the API calls and is easy to
        # leave on: the log is the only place it shows.
        log.warning(
            "ENABLE_HOURLY_JOBS is on: the due-date sweep and the renewal reminder run EVERY HOUR "
            "instead of once at %s:00/%s:30 %s. Turn it off when the test is done.",
            DUE_JOB_HOUR,
            DUE_JOB_HOUR,
            settings.display_timezone,
        )
    log.info(
        "Billing jobs started: payments polled every %ss, due dates checked %s at "
        "%s:00 %s, access recovered daily at %s:00 %s, removal enabled=%s",
        POLL_INTERVAL_SECONDS,
        "hourly" if settings.enable_hourly_jobs else "daily",
        DUE_JOB_HOUR,
        settings.display_timezone,
        RECOVERY_HOUR,
        RECOVERY_TIMEZONE,
        settings.removal_enabled,
    )
    return scheduler
