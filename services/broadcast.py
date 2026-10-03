"""Segmented, rate-limited broadcasts. Phase 7.

Gate items: G7.1 segment queries return the right users (all / active / expired) ·
G7.2 ``RetryAfter`` pauses and resumes without skipping or duplicating · G7.3 blocked users
marked and counted as failed while sending continues · G7.4 zero recipients reports
"no recipients", not "sent" · G7.5 rate stays at or under 20 messages/second.
"""

# TODO(phase-7): select_recipients(segment) -- 'all' | 'active' | 'expired'.
# TODO(phase-7): send_broadcast(...) -- asyncio.sleep(0.05) between sends, honour
#                RetryAfter, mark is_blocked_bot, tally sent_count / failed_count.
# TODO(phase-7): run as a background task: reply "Broadcast started" immediately,
#                post a summary when done.
