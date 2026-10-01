"""The account-request emails: the notice to the people who can answer one, and
the approval or refusal sent back to whoever asked.

Drawn and sent by `app/core/mail.py`; this module only says what they contain.
"""

from datetime import UTC

from app.core import mail
from app.core.config import settings
from app.db.models.account_request import AccountRequest
from app.db.models.invitation import Invitation
from app.invitations.mail import invitation_url


def _text(intro: str, facts: list[tuple[str, str]], action: str, note: str) -> str:
    lines = "\n".join(f"{label}: {value}" for label, value in facts)
    return f"{intro}\n\n{lines}\n\n" + (f"{action}\n\n" if action else "") + f"{note}\n"


def review_email(account_request: AccountRequest, to: str) -> mail.LinkEmail:
    """Tells one admin or DevOps user that someone asked for an account."""
    facts = [("Email", account_request.email)]
    if account_request.full_name:
        facts.append(("Name", account_request.full_name))
    if account_request.message:
        facts.append(("Message", account_request.message))
    url = mail.app_url("/users")
    intro = (
        f"{account_request.email} asked for an account on {settings.PROJECT_NAME}. "
        "Approve the request to email them a link to create one as a Viewer, or "
        "reject it and they are told so."
    )
    note = (
        "Anyone can ask from the sign-in page, and nothing here has been "
        "checked. Approve only if you know who this is."
    )
    return mail.LinkEmail(
        to=to,
        subject=f"{account_request.email} asked for a {settings.PROJECT_NAME} account",
        kicker="Account request",
        title="Someone asked for an account",
        intro=intro,
        facts=facts,
        button="Review request",
        url=url,
        note=note,
        text=_text(intro, facts, f"Review it under Users: {url}" if url else "", note),
    )


async def send_review_emails(emails: list[mail.LinkEmail], request_id: int) -> None:
    """Send each reviewer their notice, link or no link: the request is listed
    under Users either way. Never raises."""
    for email in emails:
        await mail.send_link_email(
            email, f"account request {request_id}", needs_link=False
        )


def approval_email(invitation: Invitation, url: str) -> mail.LinkEmail:
    """The invitation an approval creates, worded as the answer to a request."""
    approver = invitation.invited_by_name or "An administrator"
    expires = invitation.expires_at.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")
    facts = [("Role", invitation.role.value), ("Link expires", expires)]
    intro = (
        f"{approver} approved your request for an account on "
        f"{settings.PROJECT_NAME}. Choose your username and password to finish."
    )
    note = (
        "The link works once. If you did not ask for an account, ignore this "
        "email — none is created until you use the link."
    )
    return mail.LinkEmail(
        to=invitation.email,
        subject=f"Your {settings.PROJECT_NAME} account request was approved",
        kicker="Request approved",
        title="Your account request was approved",
        intro=intro,
        facts=facts,
        button="Create your account",
        url=url,
        note=note,
        text=_text(intro, facts, f"Create your account:\n{url}", note),
    )


async def send_approval(invitation: Invitation, token: str) -> bool:
    """Email the link that creates the account. Returns True once the mail
    server accepted it. Never raises: the invitation is already saved, and can
    be re-sent like any other."""
    return await mail.send_link_email(
        approval_email(invitation, invitation_url(token)), f"invitation {invitation.id}"
    )


def rejection_email(account_request: AccountRequest) -> mail.LinkEmail:
    facts = [("Requested for", account_request.email)]
    intro = (
        f"Your request for an account on {settings.PROJECT_NAME} was not approved."
    )
    note = (
        "You cannot ask again unless an administrator allows it. If you think "
        f"this is a mistake, talk to whoever runs this {settings.PROJECT_NAME}, who can "
        "invite you directly. If you did not ask for an account, ignore this email."
    )
    return mail.LinkEmail(
        to=account_request.email,
        subject=f"Your {settings.PROJECT_NAME} account request",
        kicker="Account request",
        title="Your account request was not approved",
        intro=intro,
        facts=facts,
        button="",
        url="",
        note=note,
        text=_text(intro, facts, "", note),
    )


async def send_rejection(account_request: AccountRequest) -> bool:
    """Email the refusal, which has no link. Returns True once the mail server
    accepted it. Never raises."""
    return await mail.send_link_email(
        rejection_email(account_request),
        f"rejection of account request {account_request.id}",
        needs_link=False,
    )
