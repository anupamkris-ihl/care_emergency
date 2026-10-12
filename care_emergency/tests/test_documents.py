from unittest import mock

from care.emr.models import FileUpload
from care.utils.tests.base import CareAPITestBase
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from rest_framework import status

from care_emergency.models import PatientDocument

JPEG = b"\xff\xd8\xff\xe0" + b"0" * 64


@mock.patch("care.emr.utils.file_manager.S3FilesManager.delete_object")
@mock.patch("care.emr.utils.file_manager.S3FilesManager.put_object")
class DocumentTests(CareAPITestBase):
    def setUp(self):
        super().setUp()
        self.user = self.create_super_user()
        self.patient = self.create_patient(phone_number="+919812345678")
        self.client.force_authenticate(user=self.user)

    def upload(self, content_type="image/jpeg", **data):
        body = {
            "file": SimpleUploadedFile("card.jpg", JPEG, content_type=content_type),
            "category": "payer_card",
            "side": "front",
            "capture_method": "photographed",
            **data,
        }
        return self.client.post(reverse("desk-documents"), body, format="multipart")

    def test_upload_is_staged_without_a_patient(self, put_object, delete_object):
        response = self.upload()
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        document = PatientDocument.objects.get(external_id=response.data["id"])
        self.assertIsNone(document.patient)
        self.assertEqual(document.file.file_type, "patient")
        self.assertEqual(document.file.associating_id, "")
        self.assertTrue(document.file.upload_completed)
        self.assertEqual(document.file.name, "Payer card (Front)")
        put_object.assert_called_once()

    def test_upload_to_a_patient_and_list(self, put_object, delete_object):
        response = self.upload(patient=str(self.patient.external_id), side="back")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        listed = self.client.get(
            reverse("desk-documents"), {"patient": str(self.patient.external_id)}
        )
        self.assertEqual(listed.status_code, status.HTTP_200_OK)
        self.assertEqual([x["side"] for x in listed.data], ["back"])
        self.assertTrue(listed.data[0]["read_signed_url"])
        file = FileUpload.objects.get()
        self.assertEqual(file.associating_id, str(self.patient.external_id))

    def test_rejects_other_types_and_choices(self, put_object, delete_object):
        response = self.upload(content_type="text/plain")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("JPEG, PNG, WebP or PDF", str(response.data))
        response = self.upload(side="top")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(PatientDocument.objects.exists())

    def test_only_the_uploader_removes_a_staged_document(
        self, put_object, delete_object
    ):
        document_id = self.upload().data["id"]
        url = reverse("desk-staged-document", args=[document_id])
        self.client.force_authenticate(user=self.create_super_user())
        self.assertEqual(self.client.delete(url).status_code, status.HTTP_404_NOT_FOUND)
        self.client.force_authenticate(user=self.user)
        self.assertEqual(
            self.client.delete(url).status_code, status.HTTP_204_NO_CONTENT
        )
        self.assertFalse(PatientDocument.objects.exists())
        self.assertFalse(FileUpload.objects.exists())
        delete_object.assert_called_once()

    def test_staging_needs_patient_create_access(self, put_object, delete_object):
        self.client.force_authenticate(user=self.create_user())
        self.assertEqual(self.upload().status_code, status.HTTP_403_FORBIDDEN)
