# Copyright 2017-2019 Camptocamp SA
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl.html)

import inspect
import logging
import os
import time
from contextlib import closing, contextmanager

import psycopg2

import odoo
from odoo import _, api, exceptions, models
from odoo.osv.expression import AND, OR, normalize_domain
from odoo.tools.safe_eval import const_eval

from .strtobool import strtobool

_logger = logging.getLogger(__name__)


def is_true(strval):
    return bool(strtobool(strval or "0"))


class IrAttachment(models.Model):
    _inherit = "ir.attachment"

    @staticmethod
    def is_storage_disabled(storage=None, log=True):
        msg = _("Storages are disabled (see environment configuration).")
        if storage:
            msg = _("Storage '%s' is disabled (see environment configuration).") % (
                storage,
            )
        is_disabled = is_true(os.environ.get("DISABLE_ATTACHMENT_STORAGE"))
        if is_disabled and log:
            _logger.warning(msg)
        return is_disabled

    def _register_hook(self):
        super()._register_hook()
        location = self.env.context.get("storage_location") or self._storage()
        # ignore if we are not using an object storage
        if location not in self._get_stores():
            return
        curframe = inspect.currentframe()
        calframe = inspect.getouterframes(curframe, 2)
        # the caller of _register_hook is 'load_modules' in
        # odoo/modules/loading.py
        load_modules_frame = calframe[1][0]
        # 'update_module' is an argument that 'load_modules' receives with a
        # True-ish value meaning that an install or upgrade of addon has been
        # done during the initialization. We need to move the attachments that
        # could have been created or updated in other addons before this addon
        # was loaded
        update_module = load_modules_frame.f_locals.get("update_module")

        # We need to call the migration on the loading of the model because
        # when we are upgrading addons, some of them might add attachments.
        # To be sure they are migrated to the storage we need to call the
        # migration here.
        # Typical example is images of ir.ui.menu which are updated in
        # ir.attachment at every upgrade of the addons
        if update_module:
            self.env["ir.attachment"].sudo()._force_storage_to_object_storage()

    @property
    def _object_storage_default_force_db_config(self):
        return {"image/": 51200, "application/javascript": 0, "text/css": 0}

    def _get_storage_force_db_config(self):
        param = (
            self.env["ir.config_parameter"]
            .sudo()
            .get_param(
                "ir_attachment.storage.force.database",
            )
        )
        storage_config = None
        if param:
            try:
                storage_config = const_eval(param)
            except (SyntaxError, TypeError, ValueError):
                _logger.exception(
                    "Could not parse system parameter"
                    " 'ir_attachment.storage.force.database', reverting to the"
                    " default configuration."
                )

        if not storage_config:
            storage_config = self._object_storage_default_force_db_config
        return storage_config

    def _store_in_db_instead_of_object_storage_domain(self):
        """Return a domain for attachments that must be forced to DB

        Read the docstring of ``_store_in_db_instead_of_object_storage`` for
        more details.

        Used in ``force_storage_to_db_for_special_fields`` to find records
        to move from the object storage to the database.

        The domain must be inline with the conditions in
        ``_store_in_db_instead_of_object_storage``.
        """
        domain = []
        storage_config = self._get_storage_force_db_config()
        for mimetype_key, limit in storage_config.items():
            part = [("mimetype", "=like", f"{mimetype_key}%")]
            if limit:
                part = AND([part, [("file_size", "<=", limit)]])
            domain = OR([domain, part])
        return domain

    def _store_in_db_instead_of_object_storage(self, data, mimetype):
        """Return whether an attachment must be stored in db

        When we are using an Object Storage. This is sometimes required
        because the object storage is slower than the database/filesystem.

        Small images (128, 256) are used in Odoo in list / kanban views. We
        want them to be fast to read.
        They are generally < 50KB (default configuration) so they don't take
        that much space in database, but they'll be read much faster than from
        the object storage.

        The assets (application/javascript, text/css) are stored in database
        as well whatever their size is:

        * a database doesn't have thousands of them
        * of course better for performance
        * better portability of a database: when replicating a production
          instance for dev, the assets are included

        The configuration can be modified in the ir.config_parameter
        ``ir_attachment.storage.force.database``, as a dictionary, for
        instance::

            {"image/": 51200, "application/javascript": 0, "text/css": 0}

        Where the key is the beginning of the mimetype to configure and the
        value is the limit in size below which attachments are kept in DB.
        0 means no limit.

        Default configuration means:

        * images mimetypes (image/png, image/jpeg, ...) below 51200 bytes are
          stored in database
        * application/javascript are stored in database whatever their size
        * text/css are stored in database whatever their size

        The conditions must be inline with the domain in
        ``_store_in_db_instead_of_object_storage_domain``.

        """
        if self.is_storage_disabled():
            return True
        storage_config = self._get_storage_force_db_config()
        for mimetype_key, limit in storage_config.items():
            if mimetype.startswith(mimetype_key):
                if not limit:
                    return True
                bin_data = data
                return len(bin_data) <= limit
        return False

    def _get_datas_related_values(self, data, mimetype):
        storage = self.env.context.get("storage_location") or self._storage()
        if data and storage in self._get_stores():
            if self._store_in_db_instead_of_object_storage(data, mimetype):
                # compute the fields that depend on datas
                bin_data = data
                values = {
                    "file_size": len(bin_data),
                    "checksum": self._compute_checksum(bin_data),
                    "index_content": self._index(bin_data, mimetype),
                    "store_fname": False,
                    "db_datas": data,
                }
                return values
        return super()._get_datas_related_values(data, mimetype)

    @api.model
    def _file_read(self, fname):
        if self._is_file_from_a_store(fname):
            return self._store_file_read(fname)
        else:
            return super()._file_read(fname)

    def _store_file_read(self, fname):
        storage = fname.partition("://")[0]
        raise NotImplementedError("No implementation for %s" % (storage,))

    def _store_file_write(self, key, bin_data):
        storage = self._storage()
        raise NotImplementedError("No implementation for %s" % (storage,))

    def _store_file_delete(self, fname):
        storage = fname.partition("://")[0]
        raise NotImplementedError("No implementation for %s" % (storage,))

    @api.model
    def _file_write(self, bin_data, checksum):
        location = self.env.context.get("storage_location") or self._storage()
        if location in self._get_stores() and not self.env.company.object_storage_test:
            key = self.env.context.get("force_storage_key")
            if not key:
                key = self._compute_checksum(bin_data)
            filename = self._store_file_write(key, bin_data)
        else:
            filename = super()._file_write(bin_data, checksum)
        return filename

    @api.model
    def _file_delete(self, fname):
        if (
            self._is_file_from_a_store(fname)
            and not self.env.company.object_storage_test
        ):
            cr = self.env.cr
            # using SQL to include files hidden through unlink or due to record
            # rules
            cr.execute(
                "SELECT COUNT(*) FROM ir_attachment WHERE store_fname = %s", (fname,)
            )
            count = cr.fetchone()[0]
            if not count:
                self._store_file_delete(fname)
        else:
            return super()._file_delete(fname)

    @api.model
    def _is_file_from_a_store(self, fname):
        for store_name in self._get_stores():
            if self.is_storage_disabled(store_name):
                continue
            uri = f"{store_name}://"
            if fname.startswith(uri):
                return True
        return False

    @contextmanager
    def do_in_new_env(self, new_cr=False):
        """Context manager that yields a new environment

        Using a new Odoo Environment thus a new PG transaction.
        """
        with api.Environment.manage():
            if new_cr:
                registry = odoo.modules.registry.Registry.new(self.env.cr.dbname)
                with closing(registry.cursor()) as cr:
                    try:
                        yield self.env(cr=cr)
                    except Exception:
                        cr.rollback()
                        raise
                    else:
                        # disable pylint error because this is a valid commit,
                        # we are in a new env
                        cr.commit()  # pylint: disable=invalid-commit
            else:
                # make a copy
                yield self.env()

    def _move_attachment_to_store(self):
        self.ensure_one()
        _logger.info("inspecting attachment %s (%d)", self.name, self.id)
        fname = self.store_fname
        storage = fname.partition("://")[0]
        if self.is_storage_disabled(storage):
            fname = False
        if fname:
            # migrating from filesystem filestore
            # or from the old 'store_fname' without the bucket name
            _logger.info("moving %s on the object storage", fname)
            self.write(
                {
                    "datas": self.datas,
                    # this is required otherwise the
                    # mimetype gets overriden with
                    # 'application/octet-stream'
                    # on assets
                    "mimetype": self.mimetype,
                }
            )
            _logger.info("moved %s on the object storage", fname)
            return self._full_path(fname)
        elif self.db_datas:
            _logger.info("moving on the object storage from database")
            self.write({"datas": self.datas})

    @api.model
    def force_storage(self):
        if not self.env["res.users"].browse(self.env.uid)._is_admin():
            raise exceptions.AccessError(
                _("Only administrators can execute this action.")
            )
        location = self.env.context.get("storage_location") or self._storage()
        if location not in self._get_stores():
            return super().force_storage()
        self._force_storage_to_object_storage()

    @api.model
    def force_storage_to_db_for_special_fields(self, new_cr=False):
        """Migrate special attachments from Object Storage back to database

        The access to a file stored on the objects storage is slower
        than a local disk or database access. For attachments like
        image_small that are accessed in batch for kanban views, this
        is too slow. We store this type of attachment in the database.

        This method can be used when migrating a filestore where all the files,
        including the special files (assets, image_small, ...) have been pushed
        to the Object Storage and we want to write them back in the database.

        It is not called anywhere, but can be called by RPC or scripts.
        """
        storage = self._storage()
        if self.is_storage_disabled(storage):
            return
        if storage not in self._get_stores():
            return

        domain = AND(
            (
                normalize_domain(
                    [
                        ("store_fname", "=like", f"{storage}://%"),
                        # for res_field, see comment in
                        # _force_storage_to_object_storage
                        "|",
                        ("res_field", "=", False),
                        ("res_field", "!=", False),
                    ]
                ),
                normalize_domain(self._store_in_db_instead_of_object_storage_domain()),
            )
        )

        with self.do_in_new_env(new_cr=new_cr) as new_env:
            model_env = new_env["ir.attachment"].with_context(prefetch_fields=False)
            attachment_ids = model_env.search(domain).ids
            if not attachment_ids:
                return
            total = len(attachment_ids)
            start_time = time.time()
            _logger.info(
                "Moving %d attachments from %s to DB for fast access", total, storage
            )
            current = 0
            for attachment_id in attachment_ids:
                current += 1
                # if we browse attachments outside of the loop, the first
                # access to 'datas' will compute all the 'datas' fields at
                # once, which means reading hundreds or thousands of files at
                # once, exhausting memory
                attachment = model_env.browse(attachment_id)
                # this write will read the datas from the Object Storage and
                # write them back in the DB (the logic for location to write is
                # in the 'datas' inverse computed field)
                attachment.write({"datas": attachment.datas})
                # as the file will potentially be dropped on the bucket,
                # we should commit the changes here
                new_env.cr.commit()
                if current % 100 == 0 or total - current == 0:
                    _logger.info(
                        "attachment %s/%s after %.2fs",
                        current,
                        total,
                        time.time() - start_time,
                    )

    @api.model
    def _object_storage_migration_domain(self, storage):
        # The explicit res_field condition prevents ir.attachment._search from
        # implicitly restricting the migration to non-field attachments.
        not_in_storage = normalize_domain(
            [
                "!",
                ("store_fname", "=like", f"{storage}://%"),
                "|",
                ("res_field", "=", False),
                ("res_field", "!=", False),
            ]
        )
        forced_database = normalize_domain(
            self._store_in_db_instead_of_object_storage_domain()
        )
        requires_migration = OR(
            [
                [("store_fname", "!=", False)],
                AND(
                    [
                        [("db_datas", "!=", False)],
                        ["!"] + forced_database,
                    ]
                ),
            ]
        )
        return AND([not_in_storage, requires_migration])

    @api.model
    def _force_storage_to_object_storage(
        self,
        new_cr=False,
        batch_size=500,
        max_batches=20,
        max_duration_seconds=None,
    ):
        """Migrate a bounded number of attachments to the object storage.

        Committed attachments are excluded from subsequent executions by the
        search domain, so no persistent migration cursor is required.
        """
        if batch_size <= 0 or max_batches <= 0:
            raise ValueError("batch_size and max_batches must be positive")
        if max_duration_seconds is not None and max_duration_seconds <= 0:
            raise ValueError("max_duration_seconds must be positive")

        _logger.info(
            "migrating files to the object storage "
            "(batch_size=%d, max_batches=%d, max_duration_seconds=%s)",
            batch_size,
            max_batches,
            max_duration_seconds,
        )
        storage = self.env.context.get("storage_location") or self._storage()
        if self.is_storage_disabled(storage):
            return
        domain = self._object_storage_migration_domain(storage)
        # We do a copy of the environment so we can workaround the cache issue
        # below. We do not create a new cursor by default because it causes
        # serialization issues due to concurrent updates on attachments during
        # the installation
        with self.do_in_new_env(new_cr=new_cr) as new_env:
            model_env = new_env["ir.attachment"].with_context(prefetch_fields=False)
            started_at = time.monotonic()
            last_attachment_id = 0
            processed = 0
            skipped = 0
            failed = 0
            batch_number = 0
            stop_reason = "max_batches"

            while batch_number < max_batches:
                elapsed = time.monotonic() - started_at
                if max_duration_seconds is not None and elapsed >= max_duration_seconds:
                    stop_reason = "max_duration_seconds"
                    break

                ids = model_env.search(
                    domain + [("id", ">", last_attachment_id)],
                    limit=batch_size,
                    order="id",
                ).ids
                if not ids:
                    stop_reason = "no_pending_attachments"
                    break

                batch_number += 1
                batch_started_at = time.monotonic()
                batch_processed = 0
                batch_skipped = 0
                batch_failed = 0
                for attachment_id in ids:
                    try:
                        with new_env.cr.savepoint():
                            # Do not wait for attachments used by another
                            # transaction; they will be retried on a later run.
                            new_env.cr.execute(
                                "SELECT id "
                                "FROM ir_attachment "
                                "WHERE id = %s "
                                "FOR UPDATE NOWAIT",
                                (attachment_id,),
                                log_exceptions=False,
                            )

                            # Avoid prefetching the contents of every attachment
                            # when reading the computed datas field.
                            new_env.clear()
                            attachment = model_env.browse(attachment_id)
                            attachment._move_attachment_to_store()
                            batch_processed += 1
                    except psycopg2.errors.LockNotAvailable:
                        batch_skipped += 1
                        _logger.info(
                            "Skipping locked attachment %s; it will be retried",
                            attachment_id,
                        )
                    except Exception as error:
                        batch_failed += 1
                        _logger.error(
                            "Could not migrate attachment %s to %s due to error: %s",
                            attachment_id,
                            storage,
                            error,
                        )

                # A completed batch is the durable checkpoint. The filestore
                # is deliberately retained until a separate validation and
                # cleanup operation has completed.
                new_env.cr.commit()
                processed += batch_processed
                skipped += batch_skipped
                failed += batch_failed
                last_attachment_id = ids[-1]
                _logger.info(
                    "Object storage migration batch %d committed: selected=%d, "
                    "processed=%d, skipped=%d, failed=%d, last_id=%d, "
                    "duration=%.2fs",
                    batch_number,
                    len(ids),
                    batch_processed,
                    batch_skipped,
                    batch_failed,
                    last_attachment_id,
                    time.monotonic() - batch_started_at,
                )

            _logger.info(
                "Object storage migration stopped: reason=%s, batches=%d, "
                "processed=%d, skipped=%d, failed=%d, duration=%.2fs",
                stop_reason,
                batch_number,
                processed,
                skipped,
                failed,
                time.monotonic() - started_at,
            )
            return {
                "reason": stop_reason,
                "batches": batch_number,
                "processed": processed,
                "skipped": skipped,
                "failed": failed,
                "duration": time.monotonic() - started_at,
            }

    @api.model
    def _object_storage_file_info(self, fname):
        """Return backend metadata for an object storage URI."""
        storage = fname.partition("://")[0]
        raise NotImplementedError("No implementation for %s" % (storage,))

    @api.model
    def validate_object_storage(self, batch_size=500):
        """Validate every unique object URI referenced by attachments."""
        if batch_size <= 0:
            raise ValueError("batch_size must be positive")

        storage = self.env.context.get("storage_location") or self._storage()
        if self.is_storage_disabled(storage):
            return
        if storage not in self._get_stores():
            raise exceptions.UserError(
                _("Storage '%s' is not an object storage backend.") % storage
            )

        report = {
            "storage": storage,
            "pending": self.search_count(
                self._object_storage_migration_domain(storage)
            ),
            "checked": 0,
            "valid": 0,
            "missing": 0,
            "inaccessible": 0,
            "size_mismatch": 0,
        }
        last_fname = ""
        while True:
            self.env.cr.execute(
                "SELECT store_fname, MAX(file_size) "
                "FROM ir_attachment "
                "WHERE store_fname LIKE %s AND store_fname > %s "
                "GROUP BY store_fname "
                "ORDER BY store_fname "
                "LIMIT %s",
                (f"{storage}://%", last_fname, batch_size),
            )
            objects = self.env.cr.fetchall()
            if not objects:
                break

            for fname, expected_size in objects:
                report["checked"] += 1
                try:
                    info = self._object_storage_file_info(fname)
                except FileNotFoundError:
                    report["missing"] += 1
                    _logger.error("Object storage file is missing: %s", fname)
                except (PermissionError, exceptions.AccessError):
                    report["inaccessible"] += 1
                    _logger.exception("Object storage file is inaccessible: %s", fname)
                except Exception:
                    report["inaccessible"] += 1
                    _logger.exception(
                        "Could not validate object storage file: %s", fname
                    )
                else:
                    actual_size = info.get("size")
                    if (
                        expected_size is not None
                        and actual_size is not None
                        and expected_size != actual_size
                    ):
                        report["size_mismatch"] += 1
                        _logger.error(
                            "Object storage file size mismatch for %s: "
                            "expected=%s, actual=%s",
                            fname,
                            expected_size,
                            actual_size,
                        )
                    else:
                        report["valid"] += 1
                last_fname = fname

            _logger.info(
                "Object storage validation progress: checked=%d, valid=%d, "
                "missing=%d, inaccessible=%d, size_mismatch=%d",
                report["checked"],
                report["valid"],
                report["missing"],
                report["inaccessible"],
                report["size_mismatch"],
            )

        _logger.info("Object storage validation completed: %s", report)
        return report

    def _get_stores(self):
        """To get the list of stores activated in the system"""
        return []
