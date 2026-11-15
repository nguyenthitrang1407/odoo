from odoo import fields, models


class AtApprovalReturnWizard(models.TransientModel):
    _name = 'at.approval.return.wizard'
    _description = 'Return an Approval Request for Revision'

    request_id = fields.Many2one('at.approval.request', required=True)
    reason = fields.Text(required=True)

    def action_confirm(self):
        self.ensure_one()
        self.request_id._return_to_owner(self.reason)
        # The approver may no longer see the request once it is a draft again.
        return self.env['ir.actions.act_window']._for_xml_id('at_approval.action_request_to_review')
