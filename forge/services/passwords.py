"""The passwords the package makes for the accounts it creates: a person's
first password, handed to the operator once, and a service account's, held
in memory for one sign-in and never written.
"""

import secrets

PASSWORD_BYTES = 24


def new_password() -> str:
    return secrets.token_urlsafe(PASSWORD_BYTES)
