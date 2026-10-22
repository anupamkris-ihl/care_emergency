from datetime import timedelta

from care.utils.tests.base import CareAPITestBase
from django.urls import reverse
from django.utils import timezone
from rest_framework import status

from care_emergency.constants import APP_SERVER_USERNAME
from care_emergency.models import ApiKey


class ApiKeyTests(CareAPITestBase):
    def setUp(self):
        super().setUp()
        self.admin = self.create_super_user(username="key-admin")
        self.app_user = self.create_user(username="mobile-app", is_active=True)
        self.url = reverse("api-keys")
        # Any authenticated endpoint will do to prove the key logs in.
        self.me = "/api/v1/users/getcurrentuser/"

    def assertRefused(self, response, detail="Invalid or expired API key."):
        # Care's JWT class sends no WWW-Authenticate, so DRF answers 403, not 401.
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(response.json()["detail"], detail)

    def create_key(self, **body):
        self.client.force_authenticate(user=self.admin)
        response = self.client.post(
            self.url, {"name": "Patient app", "username": "mobile-app", **body}, format="json"
        )
        self.client.force_authenticate(user=None)
        return response

    def test_create_returns_key_once_and_stores_only_hash(self):
        response = self.create_key()
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        body = response.json()
        self.assertTrue(body["key"].startswith("poh_"))
        self.assertTrue(body["key"].startswith(body["prefix"]))
        self.assertEqual(body["state"], "active")
        stored = ApiKey.objects.get(external_id=body["id"])
        self.assertNotEqual(stored.key_hash, body["key"])
        self.assertEqual(stored.created_by, self.admin)

        self.client.force_authenticate(user=self.admin)
        listed = self.client.get(self.url).json()["results"]
        self.assertEqual([row["id"] for row in listed], [body["id"]])
        self.assertNotIn("key", listed[0])

    def test_key_authenticates_as_its_user(self):
        key = self.create_key().json()["key"]
        for headers in ({"HTTP_AUTHORIZATION": f"Api-Key {key}"}, {"HTTP_X_API_KEY": key}):
            response = self.client.get(self.me, **headers)
            self.assertEqual(response.status_code, status.HTTP_200_OK, response.content)
            self.assertEqual(response.json()["username"], "mobile-app")
        self.assertIsNotNone(ApiKey.objects.get().last_used_at)

    def test_wrong_revoked_and_expired_keys_are_refused(self):
        body = self.create_key().json()
        self.assertRefused(self.client.get(self.me, HTTP_X_API_KEY=body["key"] + "x"))

        expiring = self.create_key(
            expires_at=(timezone.now() + timedelta(days=1)).isoformat()
        ).json()
        ApiKey.objects.filter(external_id=expiring["id"]).update(
            expires_at=timezone.now() - timedelta(minutes=1)
        )
        self.assertRefused(self.client.get(self.me, HTTP_X_API_KEY=expiring["key"]))

        self.client.force_authenticate(user=self.admin)
        revoked = self.client.post(reverse("api-key-revoke", args=[body["id"]]))
        self.assertEqual(revoked.json()["state"], "revoked")
        self.client.force_authenticate(user=None)
        self.assertRefused(self.client.get(self.me, HTTP_X_API_KEY=body["key"]))

    def test_inactive_user_key_is_refused(self):
        key = self.create_key().json()["key"]
        self.app_user.is_active = False
        self.app_user.save(update_fields=["is_active"])
        self.assertRefused(
            self.client.get(self.me, HTTP_X_API_KEY=key),
            "The API key's user is inactive.",
        )

    def test_only_superusers_manage_keys(self):
        self.client.force_authenticate(user=self.app_user)
        self.assertEqual(self.client.get(self.url).status_code, status.HTTP_403_FORBIDDEN)
        response = self.client.post(
            self.url, {"name": "x", "username": "mobile-app"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_a_key_cannot_manage_keys(self):
        staff = self.create_user(username="staff-admin", is_superuser=False)
        key = self.create_key(username="staff-admin").json()["key"]
        staff.is_superuser = True
        staff.save(update_fields=["is_superuser"])
        response = self.client.get(self.url, HTTP_X_API_KEY=key)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_create_validation(self):
        unknown = self.create_key(username="nobody")
        self.assertEqual(unknown.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(
            unknown.json()["errors"][0],
            {"loc": ["username"], "msg": "No active user has this username"},
        )
        superuser = self.create_key(username="key-admin")
        self.assertEqual(superuser.status_code, status.HTTP_400_BAD_REQUEST)
        past = self.create_key(expires_at=(timezone.now() - timedelta(days=1)).isoformat())
        self.assertEqual(past.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(ApiKey.objects.exists())

    def test_key_acts_as_the_app_server_by_default(self):
        self.client.force_authenticate(user=self.admin)
        missing = self.client.post(self.url, {"name": "Patient app"}, format="json")
        self.assertEqual(missing.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(missing.json()["errors"][0]["loc"], ["username"])

        app = self.create_user(username=APP_SERVER_USERNAME, is_active=True)
        response = self.client.post(self.url, {"name": "Patient app"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(response.json()["username"], APP_SERVER_USERNAME)
        self.assertEqual(ApiKey.objects.get().user, app)
