"""Shared Google OAuth token loading for Jarvis modules."""

from pathlib import Path

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials


def load_credentials(token_file, scopes):
    """
    Load and refresh credentials from token_file, saving the token if refreshed.

    If SCOPES grew beyond what the token was granted, refresh fails with
    invalid_scope; retry with the token's own scopes so unattended runs keep
    working (re-run morning_brief.py --setup to grant the new scopes).
    Any other refresh failure (e.g. revoked token) is raised.
    """
    token_file = Path(token_file)
    creds = Credentials.from_authorized_user_file(str(token_file), scopes)
    if creds.valid or not creds.refresh_token:
        return creds

    try:
        creds.refresh(Request())
    except RefreshError as e:
        if "invalid_scope" not in str(e):
            raise
        creds = Credentials.from_authorized_user_file(str(token_file))
        creds.refresh(Request())

    tmp_file = token_file.parent / (token_file.name + ".tmp")
    tmp_file.write_text(creds.to_json())
    tmp_file.replace(token_file)
    return creds
