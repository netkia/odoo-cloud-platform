from types import SimpleNamespace
from unittest.mock import Mock, patch

from botocore.exceptions import ClientError, EndpointConnectionError

from odoo.tests import common


class TestS3Validation(common.TransactionCase):
    def _bucket(self, head_object):
        client = Mock()
        client.head_object.side_effect = head_object
        return SimpleNamespace(
            name="bucket",
            meta=SimpleNamespace(client=client),
        )

    def _client_error(self, code, status_code):
        return ClientError(
            {
                "Error": {"Code": code, "Message": code},
                "ResponseMetadata": {"HTTPStatusCode": status_code},
            },
            "HeadObject",
        )

    def test_returns_object_size(self):
        attachments = self.env["ir.attachment"]
        bucket = self._bucket([{"ContentLength": 42}])

        with patch.object(
            type(attachments), "_get_s3_bucket", autospec=True, return_value=bucket
        ):
            info = attachments._object_storage_file_info("s3://bucket/key")

        self.assertEqual(info, {"size": 42})

    def test_missing_object_raises_file_not_found(self):
        attachments = self.env["ir.attachment"]
        bucket = self._bucket([self._client_error("NoSuchKey", 404)])

        with (
            patch.object(
                type(attachments), "_get_s3_bucket", autospec=True, return_value=bucket
            ),
            self.assertRaises(FileNotFoundError),
        ):
            attachments._object_storage_file_info("s3://bucket/missing")

    def test_access_denied_raises_permission_error(self):
        attachments = self.env["ir.attachment"]
        bucket = self._bucket([self._client_error("AccessDenied", 403)])

        with (
            patch.object(
                type(attachments), "_get_s3_bucket", autospec=True, return_value=bucket
            ),
            self.assertRaises(PermissionError),
        ):
            attachments._object_storage_file_info("s3://bucket/private")

    def test_transient_error_is_retried(self):
        attachments = self.env["ir.attachment"]
        bucket = self._bucket(
            [
                EndpointConnectionError(endpoint_url="https://s3.example.com"),
                {"ContentLength": 42},
            ]
        )

        with (
            patch.object(
                type(attachments), "_get_s3_bucket", autospec=True, return_value=bucket
            ),
            patch("odoo.addons.attachment_s3.models.ir_attachment.time.sleep") as sleep,
        ):
            info = attachments._object_storage_file_info("s3://bucket/key")

        self.assertEqual(info, {"size": 42})
        self.assertEqual(bucket.meta.client.head_object.call_count, 2)
        sleep.assert_called_once_with(1)
