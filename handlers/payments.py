"""Subscribe / cancel buttons shown to the user. Phase 5.

This module only renders buttons and reads state. Invoice creation, signature checks and
callback processing live in ``services/payments.py`` and ``web/routes.py`` so they can be
unit-tested without Telegram.
"""

# TODO(phase-5): "Subscribe" -> create a WayForPay invoice -> send the payment URL button.
# TODO(phase-5): "Cancel subscription" -> stop the recurring charge, keep access
#                until expires_at.
# TODO(phase-5): show current subscription status and next charge date (UTC -> display tz).
