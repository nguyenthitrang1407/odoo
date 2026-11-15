import ast

from odoo import api, fields, models
from odoo.exceptions import ValidationError
from odoo.fields import Domain

FIELD_SELECTION = [('required', 'Required'), ('optional', 'Optional'), ('no', 'None')]


class AtApprovalCategory(models.Model):
    _name = 'at.approval.category'
    _description = 'Approval Type'
    _order = 'sequence, id'

    name = fields.Char(required=True, translate=True)
    description = fields.Char(translate=True)
    image = fields.Image(max_width=256, max_height=256)
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)
    company_id = fields.Many2one('res.company', default=lambda self: self.env.company)

    # Which standard fields a request of this type shows and requires
    has_document = fields.Selection(
        [('required', 'Required'), ('optional', 'Optional')], string='Document', default='optional', required=True)
    has_partner = fields.Selection(FIELD_SELECTION, string='Contact', default='no', required=True)
    has_date = fields.Selection(FIELD_SELECTION, string='Date', default='no', required=True)
    has_period = fields.Selection(FIELD_SELECTION, string='Period', default='no', required=True)
    has_product = fields.Selection(FIELD_SELECTION, string='Products', default='no', required=True)
    has_quantity = fields.Selection(FIELD_SELECTION, string='Quantity', default='no', required=True)
    has_amount = fields.Selection(FIELD_SELECTION, string='Amount', default='no', required=True)
    has_reference = fields.Selection(FIELD_SELECTION, string='Reference', default='no', required=True)
    has_location = fields.Selection(FIELD_SELECTION, string='Location', default='no', required=True)
    has_employee = fields.Selection(FIELD_SELECTION, string='Employee Concerned', default='no', required=True)

    request_properties_definition = fields.PropertiesDefinition('Custom Fields')
    required_property_labels = fields.Char(
        'Required Custom Fields',
        help="Labels of the custom fields that must be filled before submitting, separated by commas.")

    use_sequence = fields.Boolean('Automated Numbering')
    sequence_code = fields.Char('Number Prefix')
    sequence_id = fields.Many2one('ir.sequence', string='Number Sequence', readonly=True, copy=False)

    step_ids = fields.One2many('at.approval.step', 'category_id', string='Approval Steps', copy=True)

    is_confidential = fields.Boolean(
        'Confidential',
        help="Requests are visible only to their owner, their approvers and the groups below. "
             "Emails carry no details.")
    confidential_group_ids = fields.Many2many('res.groups', string='Groups Allowed to Read')
    employee_can_read = fields.Boolean('Employee Concerned Can Read')

    notify_mode = fields.Selection(
        [('activity', 'Activity'), ('email', 'Email'), ('both', 'Activity and Email')],
        string='Notify Approvers By', default='both', required=True)
    response_days = fields.Integer('Days to Respond', default=2)
    mail_template_request_id = fields.Many2one(
        'mail.template', string='Email: Approval Needed', domain=[('model', '=', 'at.approval.approver')])
    mail_template_approved_id = fields.Many2one(
        'mail.template', string='Email: Approved', domain=[('model', '=', 'at.approval.request')])
    mail_template_refused_id = fields.Many2one(
        'mail.template', string='Email: Refused', domain=[('model', '=', 'at.approval.request')])
    mail_template_returned_id = fields.Many2one(
        'mail.template', string='Email: Returned for Revision', domain=[('model', '=', 'at.approval.request')])

    to_review_count = fields.Integer(compute='_compute_to_review_count')

    def _compute_to_review_count(self):
        counts = dict(self.env['at.approval.approver']._read_group(
            [('category_id', 'in', self.ids), ('user_id', '=', self.env.uid), ('status', '=', 'pending')],
            ['category_id'], ['__count']))
        for category in self:
            category.to_review_count = counts.get(category, 0)

    @api.model_create_multi
    def create(self, vals_list):
        categories = super().create(vals_list)
        categories._sync_sequence()
        return categories

    def write(self, vals):
        res = super().write(vals)
        if {'use_sequence', 'sequence_code'} & set(vals):
            self._sync_sequence()
        return res

    def _sync_sequence(self):
        for category in self.filtered(lambda c: c.use_sequence and c.sequence_code):
            prefix = f'{category.sequence_code}/%(year)s/'
            if category.sequence_id:
                category.sequence_id.sudo().prefix = prefix
            else:
                category.sequence_id = self.env['ir.sequence'].sudo().create({
                    'name': category.name,
                    'prefix': prefix,
                    'padding': 4,
                    'company_id': category.company_id.id,
                })

    def action_new_request(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'at.approval.request',
            'view_mode': 'form',
            'context': {'default_category_id': self.id},
        }

    def action_to_review(self):
        self.ensure_one()
        action = self.env['ir.actions.act_window']._for_xml_id('at_approval.action_request_to_review')
        action['domain'] = [
            ('category_id', '=', self.id),
            ('approver_ids', 'any', [('user_id', '=', self.env.uid), ('status', '=', 'pending')]),
        ]
        return action


class AtApprovalStep(models.Model):
    _name = 'at.approval.step'
    _description = 'Approval Step'
    _order = 'sequence, id'

    category_id = fields.Many2one('at.approval.category', required=True, ondelete='cascade', index=True)
    sequence = fields.Integer(
        'Order', default=10, help="Lower runs first. Steps with the same order run in parallel.")
    name = fields.Char(required=True, translate=True)
    approver_type = fields.Selection([
        ('user', 'Specific users'),
        ('group', 'Members of a group'),
        ('manager', 'Direct manager'),
        ('manager_level', 'Manager N levels up'),
        ('department_manager', 'Department head'),
        ('job', 'Holder of a job position'),
        ('field', 'Field on the request'),
    ], required=True, default='manager')
    user_ids = fields.Many2many('res.users', 'at_approval_step_user_rel', 'step_id', 'user_id', string='Users')
    required_user_ids = fields.Many2many(
        'res.users', 'at_approval_step_required_user_rel', 'step_id', 'user_id', string='Must Approve',
        domain="[('id', 'in', user_ids)]",
        help="These users must approve whatever the completion rule says.")
    group_id = fields.Many2one('res.groups', string='Group')
    level = fields.Integer('Levels Up', default=2)
    job_id = fields.Many2one('hr.job', string='Job Position')
    field_name = fields.Char(
        'Request Field',
        help="Technical name of a user or employee field of the request, "
             "or the label of a custom field pointing to a user or an employee.")
    rule = fields.Selection([
        ('all', 'All approvers'),
        ('any', 'Any one approver'),
        ('min', 'A minimum number of approvers'),
    ], string='Completed When', default='all', required=True)
    min_count = fields.Integer('Minimum Approvals', default=1)
    domain = fields.Char('Applies If', default='[]')
    on_missing = fields.Selection([
        ('block', 'Block the submission'),
        ('skip', 'Skip this step'),
    ], string='If No Approver Is Found', default='block', required=True)

    @api.constrains('approver_type', 'user_ids', 'group_id', 'job_id', 'field_name', 'rule', 'min_count', 'level')
    def _check_configuration(self):
        needed = {'user': 'user_ids', 'group': 'group_id', 'job': 'job_id', 'field': 'field_name'}
        for step in self:
            field = needed.get(step.approver_type)
            if field and not step[field]:
                raise ValidationError(self.env._(
                    'Step "%(step)s": please fill in "%(field)s".',
                    step=step.name, field=step._fields[field]._description_string(self.env)))
            if step.rule == 'min' and step.min_count < 1:
                raise ValidationError(self.env._('Step "%s": the minimum number of approvals must be at least 1.', step.name))
            if step.approver_type == 'manager_level' and step.level < 1:
                raise ValidationError(self.env._('Step "%s": the manager level must be at least 1.', step.name))

    @api.constrains('domain')
    def _check_domain(self):
        for step in self:
            try:
                Domain(ast.literal_eval(step.domain or '[]')).validate(self.env['at.approval.request'])
            except Exception as error:  # noqa: BLE001 - any parsing error means a bad condition
                raise ValidationError(self.env._('Step "%(step)s": invalid condition (%(error)s).', step=step.name, error=error))

    def _applies_to(self, request):
        """Whether the step's condition matches the (possibly unsaved) request."""
        self.ensure_one()
        domain = ast.literal_eval(self.domain or '[]')
        return not domain or bool(request.filtered_domain(domain))

    def _resolve_users(self, request):
        """Users who approve ``request`` at this step. The owner never approves their own request."""
        self.ensure_one()
        company = request.company_id or self.env.company
        # sudo: routing follows the org chart, which plain users are not allowed to read
        employee = (request.employee_id or request.request_owner_id.with_company(company).employee_id).sudo()
        if self.approver_type == 'user':
            users = self.user_ids
        elif self.approver_type == 'group':
            users = self.group_id.sudo().all_user_ids.filtered(lambda user: company in user.company_ids)
        elif self.approver_type == 'manager':
            users = employee.parent_id.user_id
        elif self.approver_type == 'manager_level':
            manager = employee
            for _level in range(self.level):
                manager = manager.parent_id
            users = manager.user_id
        elif self.approver_type == 'department_manager':
            users = employee.department_id.manager_id.user_id
        elif self.approver_type == 'job':
            users = self.env['hr.employee'].sudo().search([
                ('job_id', '=', self.job_id.id),
                ('company_id', '=', company.id),
                ('user_id', '!=', False),
            ]).user_id
        else:
            users = self._users_from_field(request)
        return self.env['res.users'].browse(users.ids) - request.request_owner_id

    def _users_from_field(self, request):
        name = (self.field_name or '').strip()
        request = request.sudo()
        value = self.env['res.users']
        if name in request._fields:
            value = request[name]
        elif request.id:
            for prop in request.read(['request_properties'])[0]['request_properties']:
                if name in (prop.get('string'), prop.get('name')) and prop.get('comodel') and prop.get('value'):
                    raw = prop['value']
                    ids = [raw[0]] if prop['type'] == 'many2one' else [item[0] for item in raw]
                    value = self.env[prop['comodel']].sudo().browse(ids)
        if value._name == 'hr.employee':
            return value.user_id
        return value if value._name == 'res.users' else self.env['res.users']
