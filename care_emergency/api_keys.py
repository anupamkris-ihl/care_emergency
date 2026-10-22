import hashlib
import secrets
from datetime import timedelta

from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from drf_spectacular.extensions import OpenApiAuthenticationExtension
from rest_framework.authentication import BaseAuthentication, get_authorization_header
from rest_framework.exceptions import AuthenticationFailed

from care_emergency.models import ApiKey

KEY_PREFIX = "poh_"
# Long enough to tell keys apart in a list, far too short to guess the rest.
SHOWN_PREFIX_LENGTH = len(KEY_PREFIX) + 8
AUTH_SCHEME = b"api-key"
HEADER = "HTTP_X_API_KEY"
# last_used_at is only written when it is this stale, so a busy client doesn't
# turn every read into a write.
LAST_USED_RESOLUTION = timedelta(minutes=5)


def hash_key(key):
    # The keys are 256 random bits, so a fast hash is enough: there is nothing
    # to brute-force the way there is with a password.
    return hashlib.sha256(key.encode()).hexdigest()


def new_key():
    """A fresh key, and the prefix and hash to store for it."""
    key = KEY_PREFIX + secrets.token_urlsafe(32)
    return key, key[:SHOWN_PREFIX_LENGTH], hash_key(key)


def key_state(api_key, now=None):
    now = now or timezone.now()
    if api_key.revoked_at:
        return "revoked"
    if api_key.expires_at and api_key.expires_at <= now:
        return "expired"
    return "active"


def key_from_request(request):
    """The key from `Authorization: Api-Key <key>` or `X-API-Key: <key>`."""
    parts = get_authorization_header(request).split()
    if parts and parts[0].lower() == AUTH_SCHEME:
        if len(parts) != 2:  # noqa: PLR2004
            raise AuthenticationFailed(_("Invalid API key header."))
        return parts[1].decode(errors="replace")
    return request.META.get(HEADER) or None


class ApiKeyAuthentication(BaseAuthentication):
    """Authenticates a request by an API key (see ApiKey). A request without a
    key falls through to the other authentication classes."""

    def authenticate(self, request):
        key = key_from_request(request)
        if key is None:
            return None
        api_key = (
            ApiKey.objects.select_related("user")
            .filter(key_hash=hash_key(key), deleted=False)
            .first()
        )
        now = timezone.now()
        if api_key is None or key_state(api_key, now) != "active":
            raise AuthenticationFailed(_("Invalid or expired API key."))
        if not api_key.user.is_active:
            raise AuthenticationFailed(_("The API key's user is inactive."))
        if (
            api_key.last_used_at is None
            or now - api_key.last_used_at > LAST_USED_RESOLUTION
        ):
            ApiKey.objects.filter(pk=api_key.pk).update(last_used_at=now)
        return api_key.user, api_key

    def authenticate_header(self, request):
        return ""


class ApiKeyAuthenticationScheme(OpenApiAuthenticationExtension):
    target_class = "care_emergency.api_keys.ApiKeyAuthentication"
    name = "apiKeyAuth"

    def get_security_definition(self, auto_schema):
        return {
            "type": "apiKey",
            "in": "header",
            "name": "X-API-Key",
            "description": _(
                "A POH API key, also accepted as `Authorization: Api-Key <key>`."
            ),
        }
