from unittest.mock import patch

from odoo.tests import common


class TestObjectStorageMigration(common.TransactionCase):
    def test_migration_limits_must_be_positive(self):
        attachments = self.env["ir.attachment"]

        with self.assertRaises(ValueError):
            attachments._force_storage_to_object_storage(batch_size=0)
        with self.assertRaises(ValueError):
            attachments._force_storage_to_object_storage(max_batches=0)
        with self.assertRaises(ValueError):
            attachments._force_storage_to_object_storage(max_duration_seconds=0)

    def test_database_forced_attachment_is_not_pending(self):
        attachments = self.env["ir.attachment"]
        attachment = attachments.create(
            {
                "name": "asset.js",
                "mimetype": "application/javascript",
                "datas": b"Y29uc29sZS5sb2coJ3Rlc3QnKTs=",
            }
        )
        attachment.write({"store_fname": False})

        pending = attachments.search(
            attachments._object_storage_migration_domain("test")
        )

        self.assertNotIn(attachment, pending)

    def test_url_attachment_is_not_pending(self):
        attachments = self.env["ir.attachment"]
        attachment = attachments.create(
            {
                "name": "external",
                "type": "url",
                "url": "https://example.com/document",
            }
        )

        pending = attachments.search(
            attachments._object_storage_migration_domain("test")
        )

        self.assertNotIn(attachment, pending)

    def test_validation_deduplicates_and_classifies_objects(self):
        attachments = self.env["ir.attachment"]
        attachments.create(
            [
                {
                    "name": "valid-1",
                    "store_fname": "test://bucket/valid",
                    "file_size": 10,
                },
                {
                    "name": "valid-2",
                    "store_fname": "test://bucket/valid",
                    "file_size": 10,
                },
                {
                    "name": "missing",
                    "store_fname": "test://bucket/missing",
                    "file_size": 20,
                },
                {
                    "name": "wrong-size",
                    "store_fname": "test://bucket/wrong-size",
                    "file_size": 30,
                },
            ]
        )
        model_type = type(attachments)

        def object_info(_model, fname):
            if fname.endswith("/missing"):
                raise FileNotFoundError(fname)
            if fname.endswith("/wrong-size"):
                return {"size": 31}
            return {"size": 10}

        with (
            patch.object(
                model_type, "_get_stores", autospec=True, return_value=["test"]
            ),
            patch.object(
                model_type,
                "_object_storage_file_info",
                autospec=True,
                side_effect=object_info,
            ) as file_info,
        ):
            report = attachments.with_context(
                storage_location="test"
            ).validate_object_storage(batch_size=2)

        self.assertEqual(report["checked"], 3)
        self.assertEqual(report["valid"], 1)
        self.assertEqual(report["missing"], 1)
        self.assertEqual(report["size_mismatch"], 1)
        self.assertEqual(report["inaccessible"], 0)
        self.assertEqual(file_info.call_count, 3)
