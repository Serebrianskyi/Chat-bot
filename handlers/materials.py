"""Knowledge base: admin authoring and user browsing. Phase 2.

Teaches the FSM + callback-query pattern reused by every later dialog.

Gate items: G2.1 category CRUD · G2.2 material CRUD (text, file, link) ·
G2.3 ``/cancel`` from every state leaves no partial writes · G2.4 deleting a non-empty
category is blocked or confirmed · G2.5 pagination over 8 items · G2.7 unexpected content
type gets a polite retry, not a crash · G2.8 regular users cannot reach admin actions.
"""

# TODO(phase-2): FSM states for add/edit material and category.
# TODO(phase-2): admin flows — add material (category -> text/file/link -> confirm),
#                rename/delete category, edit/delete material.
# TODO(phase-2): user flows — browse categories -> list -> open material
#                (files re-sent by file_id).
# TODO(phase-2): /cancel handler valid from any state.
# TODO(phase-2): pagination helper for lists longer than ~8 items.
