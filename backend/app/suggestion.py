"""What a suggestion was, recorded next to the mail it produced.

The plan computes a *kind* (what to do next) and an *attempt* (how many emails
have already gone unanswered) every time it runs, and throws both away. A reply
is recorded against an ``EmailLog`` row, which until now carried no trace of why
that mail was written. So "which suggestion gets answered" had no answer, and
the quality report said so explicitly rather than inventing one.

This module is the vocabulary that closes that gap. The value is written when
the mail is sent and never recomputed, because recomputing it later would
describe today's thread rather than the one that was written to.

Two rules make the data usable rather than merely present:

* **A closed vocabulary.** An unrecognised kind is rejected at the API boundary.
  If arbitrary strings were accepted, ``send_follow_up``, ``followup`` and
  ``FU`` would become three buckets with one row each, and every rate computed
  from them would be noise.
* **Silence is a value.** A mail with no kind is *unattributed* -- not unknown
  to exist, not "other". Old rows and mail composed by hand are honestly
  uncounted in the by-kind table and counted in the total, so the coverage gap
  is visible instead of being averaged into the rates.
"""

from typing import Optional

# --- the closed vocabulary of next actions ----------------------------------

#: They spoke last. The only entry whose next action is never in doubt.
KIND_ANSWER_THEM = "answer_them"
#: We spoke last and they have not answered; keep trying by email. Also the
#: entry for a draft that has been sitting unsent.
KIND_SEND_FOLLOW_UP = "send_follow_up"
#: A proposal is written and waiting for a human to approve or reject it.
#: Nothing further is proposed for this customer until that decision is made.
KIND_AWAITING_APPROVAL = "awaiting_approval"
#: Enough emails with no answer. Stop sending the same thing.
KIND_CHANGE_CHANNEL = "change_channel"
#: The last attempt never arrived, so nothing is awaiting a reply.
KIND_FIX_DELIVERY = "fix_delivery"
#: Parked: no further chasing, but not forgotten.
KIND_REVIEW = "review"

#: The closed vocabulary of next actions. Closed on purpose: an open one grows a
#: new synonym every time somebody edits a prompt, and nothing can assert on it.
#: Every kind below is produced by a constructed state in the tests, so none of
#: them is decoration -- an earlier draft carried a "keep_momentum" kind that no
#: reachable state could produce.
ACTION_KINDS = (
    KIND_ANSWER_THEM,
    KIND_SEND_FOLLOW_UP,
    KIND_AWAITING_APPROVAL,
    KIND_CHANGE_CHANNEL,
    KIND_FIX_DELIVERY,
    KIND_REVIEW,
)

#: Kinds that mean "write to them again". The caller uses this to decide whether
#: a draft is wanted, so neither a quit decision nor a pending proposal comes
#: with an email attached -- a proposal to approve a proposal is not a thing.
EMAILING_KINDS = (KIND_SEND_FOLLOW_UP,)

#: The bucket for mail that arrived without provenance. Reserved: it is produced
#: by reading a NULL column, never accepted from a caller. A client that could
#: label its own mail "unattributed" could hide a labelling bug.
UNATTRIBUTED = "unattributed"

#: 0 means "this was the first contact"; 1 is the first chase. The dimension is
#: only meaningful next to a kind, which is why ``attempt`` without ``kind`` is
#: rejected (see ``app.schemas.email_log``).
FIRST_CONTACT = 0


def is_known(kind: Optional[str]) -> bool:
    return kind in ACTION_KINDS


def normalise(kind: Optional[str]) -> Optional[str]:
    """Strip and lower-case, or return ``None`` for "no provenance".

    Deliberately does not map near-misses onto known kinds. ``send_followup``
    is a bug in the caller, and silently accepting it would produce a bucket
    that looks like data.
    """
    if kind is None:
        return None
    cleaned = kind.strip().lower()
    return cleaned or None
