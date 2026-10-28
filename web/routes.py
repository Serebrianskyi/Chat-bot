"""FastAPI application: Telegram webhook, payment callbacks, health. Phase 4.

===========================  ======  =========================================
Path                         Method  Protected by
===========================  ======  =========================================
``/telegram/webhook``        POST    ``X-Telegram-Bot-Api-Secret-Token`` header
``/payments/wayforpay``      POST    HMAC signature in the body
``/health``                  GET     none
===========================  ======  =========================================

**Why this exists at all.** `/payments/wayforpay` is the only place WayForPay delivers a
`recToken` for an invoice payment, so automatic renewal is impossible without it — `CHECK_STATUS`
does not carry one, proven in production on 2026-10-28 (payments design §1a). It is also a
better confirmation path than polling: immediate, exactly once, signed, and free of the gap that
wrote off 77 orders on 2026-10-09.

Gate items: G4.3 the webhook rejects a wrong or missing secret token · G4.5 ``/health`` returns
200 **only** when the database is reachable.

``/jobs/*`` is deliberately **not** built. The scheduler runs in-process and every job is a date
comparison, so nothing is missed across restarts; a second way to trigger money movement, exposed
on the public internet behind one shared secret, is risk with no matching benefit. `scripts/
run_jobs.py` covers manual runs.
"""

import logging
from typing import Any

from aiogram import Bot, Dispatcher
from aiogram.types import Update
from fastapi import APIRouter, FastAPI, Header, HTTPException, Request, Response
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from config import Settings
from db.models import utcnow
from services import billing
from services.billing import BillingConfig
from services.wayforpay import SignatureMismatch, WayForPayClient, callback_reply

log = logging.getLogger(__name__)

#: Where Telegram posts updates. Secret enough to be useless on its own — the secret-token header
#: is what actually protects it — but there is no reason to publish the obvious path either.
TELEGRAM_WEBHOOK_PATH = "/telegram/webhook"

#: Where WayForPay posts payment results. Must match the ``serviceUrl`` sent on CREATE_INVOICE.
PAYMENTS_CALLBACK_PATH = "/payments/wayforpay"


def create_app(
    *,
    settings: Settings,
    dispatcher: Dispatcher,
    bot: Bot,
    engine: AsyncEngine,
    session_factory: async_sessionmaker,
    wayforpay: WayForPayClient | None,
    billing_config: BillingConfig,
) -> FastAPI:
    """Build the app. Everything it needs is passed in, nothing is looked up.

    A factory rather than a module-level ``app``: the bot, dispatcher and engine are owned by the
    process that starts them, and a module-level instance would make a second app in one process
    impossible — which every test needs.
    """
    app = FastAPI(title="ChatBot", docs_url=None, redoc_url=None, openapi_url=None)
    router = APIRouter()

    @router.get("/health")
    async def health() -> Response:
        """200 only when the database answers (G4.5).

        A health check that reports the process alive while the database is unreachable is worse
        than none: the platform would keep routing to a bot that cannot do anything.
        """
        try:
            async with engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
        except Exception:
            log.exception("Health check failed: the database is unreachable")
            return Response(
                content='{"status":"unhealthy"}', status_code=503, media_type="application/json"
            )
        return Response(content='{"status":"ok"}', media_type="application/json")

    @router.post(TELEGRAM_WEBHOOK_PATH)
    async def telegram_webhook(
        request: Request,
        x_telegram_bot_api_secret_token: str | None = Header(default=None),
    ) -> Response:
        """Feed one Telegram update to the dispatcher.

        The secret token is compared before the body is parsed (G4.3). Anyone can POST to a
        public URL; without this check they could forge any update, including an admin's.
        """
        expected = settings.webhook_secret
        if not expected or x_telegram_bot_api_secret_token != expected:
            log.warning("Rejected a webhook call with a wrong or missing secret token")
            raise HTTPException(status_code=403, detail="forbidden")

        update = Update.model_validate(await request.json(), context={"bot": bot})
        # Awaited, not backgrounded: aiogram's error middleware is inside this call, so a handler
        # that raises is already handled. Returning before it finished would also let Telegram
        # redeliver an update that is still being processed.
        await dispatcher.feed_update(bot, update)
        return Response(status_code=200)

    @router.post(PAYMENTS_CALLBACK_PATH)
    async def wayforpay_callback(request: Request) -> Response:
        """Apply a payment result WayForPay pushed to us.

        **The reply matters as much as the processing.** WayForPay repeats a callback until it is
        acknowledged with a signed ``accept``, so a silent 200 means the same payment arrives
        again and again.

        A body whose signature does not verify gets 403 and is **not** acknowledged: it is either
        forged or corrupted, and acknowledging it would tell the gateway a real notification had
        been handled.
        """
        # Both are needed: the client to read the body, the key to sign the acknowledgement.
        # `build_client` returns None without the key, so in practice they are absent together.
        if wayforpay is None or settings.wayforpay_secret_key is None:
            log.error("Payment callback received but WayForPay is not configured")
            raise HTTPException(status_code=503, detail="payments not configured")

        payload: dict[str, Any] = await request.json()
        try:
            await billing.process_callback(
                session_factory,
                bot,
                client=wayforpay,
                payload=payload,
                admin_ids=settings.admin_id_set,
                config=billing_config,
            )
        except SignatureMismatch:
            log.error("Payment callback failed signature verification; refusing it")
            raise HTTPException(status_code=403, detail="bad signature") from None
        except Exception:
            # Acknowledging would lose the notification for good, so this is left unacknowledged
            # on purpose: WayForPay retries, and by then the bug may be fixed or the poller will
            # have caught the payment anyway.
            log.exception("Payment callback could not be processed")
            raise HTTPException(status_code=500, detail="internal error") from None

        return _json(
            callback_reply(
                settings.wayforpay_secret_key.get_secret_value(),
                order_reference=str(payload.get("orderReference") or ""),
                now=int(utcnow().timestamp()),
            )
        )

    app.include_router(router)
    return app


def _json(body: dict[str, Any]) -> Response:
    """A JSON response built without FastAPI's model layer, so field order is ours."""
    import json

    return Response(content=json.dumps(body), media_type="application/json")
