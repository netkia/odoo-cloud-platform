from odoo import _, fields, models



class ResCompany(models.Model):
    _inherit = "res.company"

    object_storage_test = fields.Boolean(
        default=False,
        help="Test Object Storage connection",
    )
