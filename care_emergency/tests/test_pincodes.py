import os
import tempfile
from io import StringIO
from unittest import mock

import requests
from care.utils.tests.base import CareAPITestBase
from django.core.management import CommandError, call_command
from django.test import TestCase
from django.urls import reverse
from rest_framework import status

from care_emergency.models import PostOffice
from care_emergency.pincodes import (
    DirectoryError,
    parse_row,
    replace_directory,
    rows_from_api,
)
from care_emergency.tasks import refresh_pincode_directory


def record(name, pincode="110001", **kwargs):
    return {
        "circlename": "Delhi Circle",
        "officename": name,
        "pincode": pincode,
        "officetype": "BO",
        "delivery": "Delivery",
        "district": "NEW DELHI",
        "statename": "DELHI",
        **kwargs,
    }


def page(records, total):
    response = mock.Mock()
    response.json.return_value = {
        "total": total,
        "count": len(records),
        "records": records,
    }
    response.raise_for_status.return_value = None
    return response


class ParseRowTests(TestCase):
    def test_tidies_names_and_strips_office_suffix(self):
        row = parse_row(
            record(
                "CONNAUGHT PLACE S.O",
                district="JAMMU AND KASHMIR",
                statename="JAMMU AND KASHMIR",
            )
        )
        self.assertEqual(row["name"], "Connaught Place")
        self.assertEqual(row["district"], "Jammu and Kashmir")
        self.assertTrue(row["delivery"])
        for name, expected in (
            ("Achampet B.O", "Achampet"),
            ("New Delhi G.P.O.", "New Delhi"),
            ("Kalkaji HO", "Kalkaji"),
        ):
            self.assertEqual(parse_row(record(name))["name"], expected)

    def test_csv_headers_and_non_delivery(self):
        row = parse_row(
            {
                "OfficeName": "Parliament House S.O",
                "Pincode": "110001",
                "OfficeType": "SO",
                "Delivery": "Non-Delivery",
                "District": "New Delhi",
                "StateName": "Delhi",
            }
        )
        self.assertEqual(row["name"], "Parliament House")
        self.assertFalse(row["delivery"])

    def test_rejects_bad_pincode(self):
        self.assertIsNone(parse_row(record("Somewhere", pincode="012345")))
        self.assertIsNone(parse_row(record("", pincode="110001")))


class DirectoryLoadTests(TestCase):
    def test_api_pages_until_total(self):
        session = mock.Mock()
        session.get.side_effect = [
            page([record("A B.O"), record("B B.O")], 3),
            page([record("C B.O")], 3),
        ]
        self.assertEqual(len(list(rows_from_api("key", session))), 3)
        self.assertEqual(session.get.call_args.kwargs["params"]["offset"], 2)

    def test_short_or_failed_download_keeps_old_directory(self):
        replace_directory([record("Old B.O")])
        session = mock.Mock()
        session.get.side_effect = [page([record("A B.O")], 5), page([], 5)]
        with self.assertRaises(DirectoryError):
            replace_directory(rows_from_api("key", session))
        session.get.side_effect = requests.ConnectionError("refused")
        with self.assertRaises(DirectoryError):
            replace_directory(rows_from_api("key", session))
        self.assertEqual(
            list(PostOffice.objects.values_list("name", flat=True)), ["Old"]
        )

    def test_replace_deduplicates(self):
        replace_directory([record("Old B.O", pincode="400001")])
        count = replace_directory([record("A B.O"), record("A BO"), record("B S.O")])
        self.assertEqual(count, 2)
        self.assertFalse(PostOffice.objects.filter(pincode="400001").exists())

    def test_command_needs_key_or_file(self):
        with mock.patch.dict(os.environ, {"DATA_GOV_IN_API_KEY": ""}):
            with self.assertRaises(CommandError):
                call_command("load_pincode_directory", stdout=StringIO())

    def test_command_loads_csv(self):
        with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False) as file:
            file.write(
                "circlename,officename,pincode,officetype,delivery,district,statename\n"
            )
            file.write(
                "Delhi Circle,Connaught Place S.O,110001,SO,Delivery,NEW DELHI,DELHI\n"
            )
            file.write(
                "Delhi Circle,Parliament House S.O,110001,SO,Non-Delivery,NEW DELHI,DELHI\n"
            )
        self.addCleanup(os.unlink, file.name)
        out = StringIO()
        call_command("load_pincode_directory", file=file.name, stdout=out)
        self.assertIn("Loaded 2 post offices", out.getvalue())

    def test_task_skips_without_key(self):
        with mock.patch.dict(os.environ, {"DATA_GOV_IN_API_KEY": ""}):
            refresh_pincode_directory()
        self.assertFalse(PostOffice.objects.exists())

    def test_task_uses_key(self):
        with (
            mock.patch.dict(os.environ, {"DATA_GOV_IN_API_KEY": "secret"}),
            mock.patch("care_emergency.pincodes.rows_from_api") as rows,
        ):
            rows.return_value = [record("A B.O")]
            refresh_pincode_directory()
        rows.assert_called_once_with("secret")
        self.assertTrue(PostOffice.objects.filter(name="A").exists())


class PincodeViewTests(CareAPITestBase):
    def setUp(self):
        super().setUp()
        self.client.force_authenticate(user=self.create_user())

    def get(self, pincode):
        return self.client.get(reverse("pincode", kwargs={"pincode": pincode}))

    def test_unloaded_directory(self):
        response = self.get("110001")
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertFalse(response.json()["directory_loaded"])

    def test_lookup(self):
        replace_directory(
            [
                record("Parliament House S.O"),
                record("Connaught Place S.O"),
                record("Pune City", pincode="411001"),
            ]
        )
        response = self.get("110001")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            response.json(),
            {
                "country": "India",
                "state": "Delhi",
                "district": "New Delhi",
                "localities": ["Connaught Place", "Parliament House"],
            },
        )
        missing = self.get("999999")
        self.assertEqual(missing.status_code, status.HTTP_404_NOT_FOUND)
        self.assertTrue(missing.json()["directory_loaded"])
        self.assertEqual(self.get("011001").status_code, status.HTTP_400_BAD_REQUEST)

    def test_needs_login(self):
        self.client.force_authenticate(user=None)
        self.assertIn(
            self.get("110001").status_code,
            (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN),
        )
