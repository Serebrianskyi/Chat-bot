"""FastAPI application: webhook, payment callbacks, cron jobs, health. Phase 4.

Endpoints:

===========================  ======  =========================================
Path                         Method  Protected by
===========================  ======  =========================================
``/telegram/webhook``        POST    ``X-Telegram-Bot-Api-Secret-Token`` header
``/payments/wayforpay``      POST    HMAC signature in the body (Phase 5)
``/jobs/expire``             POST    jobs secret header
``/jobs/remind``             POST    jobs secret header
``/health``                  GET     none
===========================  ======  =========================================

Gate items: G4.3 webhook rejects a wrong or missing secret token · G4.4 ``/jobs/*`` rejects
requests without the jobs secret · G4.5 ``/health`` returns 200 **only** when the database
is reachable.
"""

# TODO(phase-4): create_app() -> FastAPI, with the aiogram Dispatcher wired in.
# TODO(phase-4): POST /telegram/webhook -- compare the secret-token header before feeding
#                the update to the dispatcher; 403 otherwise.
# TODO(phase-4): POST /jobs/expire, POST /jobs/remind -- shared secret-header dependency.
# TODO(phase-4): GET /health -- execute `SELECT 1`; 503 if the database is unreachable.
# TODO(phase-4): on startup, set_webhook(url=BASE_URL + path, secret_token=WEBHOOK_SECRET).
# TODO(phase-5): POST /payments/wayforpay -- delegate to services.payments.process_callback
#                and return the signed accept response.
