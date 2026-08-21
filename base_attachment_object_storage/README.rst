Base class for attachments on external object store
===================================================

This is a base addon that regroup common code used by addons targeting specific object store

Configuration
-------------

Object storage may be slow, and for this reason, we want to store
some files in the database whatever.

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

This storage configuration can be modified in the system parameter
``ir_attachment.storage.force.database``, as a JSON value, for instance::

    {"image/": 51200, "application/javascript": 0, "text/css": 0}

Where the key is the beginning of the mimetype to configure and the
value is the limit in size below which attachments are kept in DB.
0 means no limit.

Default configuration means:

* images mimetypes (image/png, image/jpeg, ...) below 50KB are
  stored in database
* application/javascript are stored in database whatever their size
* text/css are stored in database whatever their size

Disable attachment storage I/O
------------------------------

Define a environment variable `DISABLE_ATTACHMENT_STORAGE` set to `1`
This will prevent any kind of exceptions and read/write on storage attachments.

Incremental migration
---------------------

``_force_storage_to_object_storage`` migrates attachments in bounded batches and
commits after every completed batch. Its arguments are:

* ``batch_size``: attachments selected per batch (default: 500)
* ``max_batches``: batches processed by one execution (default: 20)
* ``max_duration_seconds``: optional duration limit checked between batches

Committed attachments are no longer selected on the next execution. Failed or
locked attachments remain pending and are retried automatically. A controlled
stop caused by the duration limit always happens between batches.

The migration deliberately does not delete files from the filestore. This
prevents data loss when several attachment records share the same physical file
and keeps the original data available until the object storage has been fully
validated.

Validation and filestore retirement
-----------------------------------

Call ``validate_object_storage()`` after the migration. It checks every unique
object URI through the configured backend and returns counters for pending,
valid, missing, inaccessible, and size-mismatched objects.

Do not remove the filestore unless all these conditions are met:

* ``pending`` is zero
* ``missing`` is zero
* ``inaccessible`` is zero
* ``size_mismatch`` is zero

Stop attachment writes before the final validation. Move or rename the complete
filestore as a reversible backup, restart Odoo, and verify representative
downloads, images, and assets. Keep that backup for an agreed retention period
before deleting it permanently.
