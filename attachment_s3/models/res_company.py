from odoo import _, fields, models



class ResCompany(models.Model):
    _inherit = "res.company"

    s3_test = fields.Boolean(
        default=False,
        help="Test S3 connection and bucket access",
    )
