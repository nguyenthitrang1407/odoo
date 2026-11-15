import logging
from datetime import timedelta

from markupsafe import Markup, escape

from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError

_logger = logging.getLogger(__name__)

ACTIVITY_TYPE = 'at_approval.mail_activity_type_approval'
OFFICER_GROUP = 'at_approval.group_approval_officer'
OPEN_STATUSES = ('new', 'waiting', 'pending')
# Fields any user with read access may write: chatter, followers and activities.
FREE_PREFIXES = ('message_', 'activity_', 'website_message_', 'rating_')
# category flag -> request fields that must be set when the flag is "required"
REQUIRED_FIELDS = {
    'has_partner': ('partner_id',),
    'has_date': ('date',),
    'has_period': ('date_start', 'date_end'),
    'has_product': ('product_line_ids',),
    'has_quantity': ('quantity',),
    'has_amount': ('amount',),
    'has_reference': ('reference',),
    'has_location': ('location',),
    'has_employee': ('employee_id',),
}


class AtApprovalRequest(models.Model):
    _name = 'at.approval.request'
    _description = 'Approval Request'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'id desc'
    _check_company_auto = True

    name = fields.Char(
        'Subject', required=True, compute='_compute_name', store=True, readonly=False, precompute=True)
    code = fields.Char('Number', readonly=True, copy=False, index=True)
    active = fields.Boolean(default=True)
    category_id = fields.Many2one(
        'at.approval.category', string='Approval Type', required=True, index=True, check_company=True)
    request_owner_id = fields.Many2one(
        'res.users', string='Requested By', required=True, index=True, default=lambda self: self.env.user)
    employee_id = fields.Many2one(
        'hr.employee', string='Employee Concerned', check_company=True,
        compute='_compute_employee_id', store=True, readonly=False, precompute=True,
        help="The employee the request is about. Org-chart approvers are looked up from this employee.")
    employee_user_id = fields.Many2one('res.users', related='employee_id.user_id', store=True, compute_sudo=True)
    company_id = fields.Many2one('res.company', required=True, default=lambda self: self.env.company)
    state = fields.Selection([
        ('draft', 'Draft'),
        ('pending', 'Submitted'),
        ('approved', 'Approved'),
        ('refused', 'Refused'),
        ('cancel', 'Cancelled'),
    ], default='draft', required=True, readonly=True, copy=False, index=True, tracking=True)

    date = fields.Date()
    date_start = fields.Datetime('Period Start')
    date_end = fields.Datetime('Period End')
    partner_id = fields.Many2one('res.partner', string='Contact')
    amount = fields.Monetary()
    currency_id = fields.Many2one('res.currency', default=lambda self: self.env.company.currency_id)
    quantity = fields.Float()
    reference = fields.Char()
    location = fields.Char()
    description = fields.Html()
    return_reason = fields.Text('Revision Requested', readonly=True, copy=False)
    request_properties = fields.Properties(
        'Custom Fields', definition='category_id.request_properties_definition', copy=True)

    approver_ids = fields.One2many('at.approval.approver', 'request_id', string='Approvers', copy=False)
    approver_user_ids = fields.Many2many(
        'res.users', 'at_approval_request_approver_user_rel', 'request_id', 'user_id',
        compute='_compute_approver_user_ids', store=True, compute_sudo=True)
    approver_preview = fields.Html('Expected Approvers', compute='_compute_approver_preview', sanitize=False)
    product_line_ids = fields.One2many('at.approval.product.line', 'request_id', string='Products', copy=True)
    attachment_number = fields.Integer('Documents', compute='_compute_attachment_number')

    can_approve = fields.Boolean(compute='_compute_user_rights')
    can_withdraw = fields.Boolean(compute='_compute_user_rights')
    can_manage = fields.Boolean(compute='_compute_user_rights', help="Owner of the request, or Approvals officer.")

    is_confidential = fields.Boolean(related='category_id.is_confidential', store=True)
    employee_can_read = fields.Boolean(related='category_id.employee_can_read', store=True)
    has_document = fields.Selection(related='category_id.has_document', string='Document Setting')
    has_partner = fields.Selection(related='category_id.has_partner', string='Contact Setting')
    has_date = fields.Selection(related='category_id.has_date', string='Date Setting')
    has_period = fields.Selection(related='category_id.has_period', string='Period Setting')
    has_product = fields.Selection(related='category_id.has_product', string='Products Setting')
    has_quantity = fields.Selection(related='category_id.has_quantity', string='Quantity Setting')
    has_amount = fields.Selection(related='category_id.has_amount', string='Amount Setting')
    has_reference = fields.Selection(related='category_id.has_reference', string='Reference Setting')
    has_location = fields.Selection(related='category_id.has_location', string='Location Setting')
    has_employee = fields.Selection(related='category_id.has_employee', string='Employee Concerned Setting')

    # ------------------------------------------------------------------
    # Computes
    # ------------------------------------------------------------------

    @api.depends('category_id')
    def _compute_name(self):
        for request in self:
            if not request.name:
                request.name = request.category_id.name

    @api.depends('request_owner_id', 'company_id')
    def _compute_employee_id(self):
        for request in self:
            if not request.employee_id:
                # sudo: the owner's employee record is not readable by every user
                owner = request.request_owner_id.sudo().with_company(request.company_id or self.env.company)
                request.employee_id = owner.employee_id

    @api.depends('approver_ids.user_id')
    def _compute_approver_user_ids(self):
        for request in self:
            request.approver_user_ids = request.approver_ids.user_id

    @api.depends('approver_ids.status', 'approver_ids.user_id', 'state', 'request_owner_id')
    @api.depends_context('uid')
    def _compute_user_rights(self):
        officer = self._is_officer()
        for request in self:
            request.can_manage = officer or request.request_owner_id == self.env.user
            mine = request.approver_ids.filtered(lambda line: line.user_id == self.env.user)
            request.can_approve = request.state == 'pending' and 'pending' in mine.mapped('status')
            request.can_withdraw = (
                request.state in ('pending', 'approved', 'refused')
                and bool(set(mine.mapped('status')) & {'approved', 'refused'}))

    def _compute_attachment_number(self):
        counts = dict(self.env['ir.attachment']._read_group(
            [('res_model', '=', self._name), ('res_id', 'in', self.ids)], ['res_id'], ['__count']))
        for request in self:
            request.attachment_number = counts.get(request.id, 0)

    @api.depends('category_id', 'employee_id', 'request_owner_id', 'company_id', 'amount', 'quantity',
                 'partner_id', 'date', 'request_properties')
    def _compute_approver_preview(self):
        for request in self:
            if request.state != 'draft' or not request.category_id:
                request.approver_preview = False
                continue
            rows = Markup()
            for step, users, reason in request._plan_steps():
                detail = escape(', '.join(users.mapped('name'))) if users else Markup('<i>%s</i>') % reason
                rows += Markup('<li class="%s"><b>%s</b>: %s</li>') % ('' if users else 'text-muted', step.name, detail)
            request.approver_preview = Markup('<ul class="mb-0">%s</ul>') % rows if rows else False

    # ------------------------------------------------------------------
    # CRUD and access
    # ------------------------------------------------------------------

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if not self.env.su:
                if vals.get('state', 'draft') != 'draft' or vals.get('code'):
                    raise AccessError(self.env._('A request always starts as a draft.'))
                owner_id = vals.get('request_owner_id', self.env.uid)
                if owner_id != self.env.uid and not self.env.user.has_group(OFFICER_GROUP):
                    raise AccessError(self.env._('You can only create requests for yourself.'))
        return super().create(vals_list)

    def write(self, vals):
        protected = {name for name in vals if not name.startswith(FREE_PREFIXES)}
        if protected and not self.env.su:
            if protected & {'state', 'code', 'return_reason'}:
                raise AccessError(self.env._('The status of a request only changes through its buttons.'))
            officer = self.env.user.has_group(OFFICER_GROUP)
            for request in self:
                if officer and protected <= {'approver_ids', 'active'}:
                    continue
                if request.state != 'draft' or not (officer or request.request_owner_id == self.env.user):
                    raise UserError(self.env._(
                        'Request "%s" can only be edited by its owner while it is a draft.', request.name))
        return super().write(vals)

    @api.ondelete(at_uninstall=False)
    def _unlink_only_draft(self):
        if any(request.state != 'draft' for request in self):
            raise UserError(self.env._('Only draft requests can be deleted. Archive the others instead.'))

    def _is_officer(self):
        return self.env.su or self.env.user.has_group(OFFICER_GROUP)

    def _check_owner_or_officer(self):
        for request in self:
            if request.request_owner_id != self.env.user and not self._is_officer():
                raise AccessError(self.env._('Only the owner of request "%s" or an Approvals officer can do this.', request.name))

    def _check_can_edit_approvers(self):
        """Owners edit approvers while the request is a draft, officers until it is closed."""
        for request in self:
            allowed = ('draft', 'pending') if self._is_officer() else ('draft',)
            if request.state not in allowed or not (self._is_officer() or request.request_owner_id == self.env.user):
                raise AccessError(self.env._('You cannot change the approvers of request "%s".', request.name))

    # ------------------------------------------------------------------
    # Approval plan
    # ------------------------------------------------------------------

    def _plan_steps(self):
        """Return ``[(step, users, reason)]`` in step order. ``users`` is empty when
        the step is skipped or has nobody, ``reason`` then says why."""
        self.ensure_one()
        plan = []
        for step in self.category_id.step_ids:
            if not step._applies_to(self):
                plan.append((step, self.env['res.users'], self.env._('skipped, condition not met')))
                continue
            users = step._resolve_users(self)
            plan.append((step, users, '' if users else self.env._('no approver found')))
        return plan

    def _check_required_fields(self):
        self.ensure_one()
        category = self.category_id
        missing = [
            category._fields[flag]._description_string(self.env)
            for flag, names in REQUIRED_FIELDS.items()
            if category[flag] == 'required' and not all(self[name] for name in names)
        ]
        if category.has_document == 'required' and not self.sudo().attachment_number:
            missing.append(self.env._('Document (attach at least one file)'))
        required_labels = {label.strip() for label in (category.required_property_labels or '').split(',') if label.strip()}
        if required_labels:
            for prop in self.read(['request_properties'])[0]['request_properties']:
                if prop.get('string') in required_labels and not prop.get('value'):
                    missing.append(prop['string'])
        if missing:
            raise UserError(self.env._('Please fill in before submitting: %s', ', '.join(missing)))

    # ------------------------------------------------------------------
    # Buttons
    # ------------------------------------------------------------------

    def action_submit(self):
        for request in self:
            request._check_owner_or_officer()
            if request.state != 'draft':
                raise UserError(self.env._('Request "%s" has already been submitted.', request.name))
            request._check_required_fields()
            lines, notes = [], []
            for step, users, reason in request._plan_steps():
                if users:
                    lines += [{
                        'request_id': request.id,
                        'step_id': step.id,
                        'sequence': step.sequence,
                        'user_id': user.id,
                        'required': user in step.required_user_ids,
                    } for user in users]
                elif step._applies_to(request) and step.on_missing == 'block':
                    raise UserError(self.env._(
                        'Step "%s" has no approver for this request. '
                        'Ask an administrator to check the approval type or the org chart.', step.name))
                else:
                    notes.append(self.env._('Step "%(step)s": %(reason)s.', step=step.name, reason=reason))
            # sudo: permissions were checked above, lines and status are system-managed
            request = request.sudo()
            request.approver_ids.filtered('step_id').unlink()
            if not lines and not request.approver_ids:
                raise UserError(self.env._(
                    'Nobody would approve this request. Add an approver or ask an administrator '
                    'to configure approval steps for "%s".', request.category_id.name))
            self.env['at.approval.approver'].sudo().create(lines)
            vals = {'state': 'pending', 'return_reason': False}
            if request.category_id.sequence_id and not request.code:
                vals['code'] = request.category_id.sequence_id.next_by_id()
            request.write(vals)
            for note in notes:
                request.message_post(body=note)
        self._sync_state()
        return True

    def _my_line(self, statuses):
        self.ensure_one()
        line = self.approver_ids.filtered(lambda l: l.user_id == self.env.user and l.status in statuses)[:1]
        if not line:
            raise UserError(self.env._('It is not your turn to act on request "%s".', self.name))
        return line.sudo()

    def _decide(self, status, message):
        for request in self:
            if request.state != 'pending':
                raise UserError(self.env._('Request "%s" is not waiting for approval.', request.name))
            request._my_line(('pending',)).write({'status': status, 'decided_on': fields.Datetime.now()})
            request = request.sudo()
            request.activity_feedback([ACTIVITY_TYPE], user_id=self.env.uid)
            request.message_post(body=message)
        self._sync_state()
        return True

    def action_approve(self):
        return self._decide('approved', self.env._('Approved by %s.', self.env.user.name))

    def action_refuse(self):
        return self._decide('refused', self.env._('Refused by %s.', self.env.user.name))

    def action_withdraw(self):
        for request in self:
            if request.state not in ('pending', 'approved', 'refused'):
                raise UserError(self.env._('Request "%s" is closed.', request.name))
            request._my_line(('approved', 'refused')).write({'status': 'new', 'decided_on': False})
            request.sudo().message_post(body=self.env._('%s withdrew their decision.', self.env.user.name))
        self._sync_state()
        return True

    def action_force_approve(self):
        if not self._is_officer():
            raise AccessError(self.env._('Only an Approvals officer can force an approval.'))
        for request in self:
            if request.state != 'pending':
                raise UserError(self.env._('Request "%s" is not waiting for approval.', request.name))
            request = request.sudo()
            request.approver_ids.filtered(lambda l: l.status in OPEN_STATUSES).write({
                'status': 'approved',
                'decided_on': fields.Datetime.now(),
                'comment': self.env._('Forced by %s', self.env.user.name),
            })
            request.activity_unlink([ACTIVITY_TYPE])
            request.message_post(body=self.env._('Approval forced by %s.', self.env.user.name))
        self._sync_state()
        return True

    def action_cancel(self):
        self._check_owner_or_officer()
        for request in self.sudo():
            if request.state not in ('draft', 'pending'):
                raise UserError(self.env._('Request "%s" can no longer be cancelled.', request.name))
            request.approver_ids.filtered(lambda l: l.status in OPEN_STATUSES).status = 'cancel'
            request.activity_unlink([ACTIVITY_TYPE])
            request.state = 'cancel'
        return True

    def action_draft(self):
        self._check_owner_or_officer()
        for request in self.sudo():
            if request.state not in ('cancel', 'refused'):
                raise UserError(self.env._('Request "%s" cannot be set back to draft.', request.name))
            request._reset_to_draft()
        return True

    def action_return(self):
        self.ensure_one()
        self._my_line(('pending',))
        return {
            'type': 'ir.actions.act_window',
            'name': self.env._('Return for Revision'),
            'res_model': 'at.approval.return.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {'default_request_id': self.id},
        }

    def _return_to_owner(self, reason):
        """An approver whose turn it is sends the request back to its owner."""
        self.ensure_one()
        if self.state != 'pending':
            raise UserError(self.env._('Request "%s" is not waiting for approval.', self.name))
        self._my_line(('pending',))
        request = self.sudo()
        request._reset_to_draft()
        request.return_reason = reason
        request.message_post(body=self.env._(
            'Returned for revision by %(user)s: %(reason)s', user=self.env.user.name, reason=reason))
        emailed = request.category_id.notify_mode != 'activity'
        request.with_context(mail_activity_quick_update=emailed).activity_schedule(
            'mail.mail_activity_data_todo', user_id=request.request_owner_id.id,
            summary=self.env._('Revision needed: %s', request.name), note=reason)
        if emailed:
            request._send_template('returned')

    def _reset_to_draft(self):
        """Forget every decision. Lines coming from the type are rebuilt on the next submit."""
        self.approver_ids.filtered('step_id').unlink()
        self.approver_ids.write({'status': 'new', 'decided_on': False, 'comment': False})
        self.activity_unlink([ACTIVITY_TYPE])
        self.state = 'draft'

    # ------------------------------------------------------------------
    # Status engine
    # ------------------------------------------------------------------

    @staticmethod
    def _step_done(step, lines):
        approved = lines.filtered(lambda l: l.status == 'approved')
        if lines.filtered('required') - approved:
            return False
        if step.rule == 'any':
            return bool(approved)
        if step.rule == 'min':
            return len(approved) >= min(step.min_count, len(lines))
        return approved == lines  # 'all', and approvers added by hand

    def _sync_state(self):
        """The only place deriving the request state and the status of undecided
        lines from the decisions taken so far.

        * one refusal refuses the request;
        * levels (steps sharing an order) run one after the other, steps of a level in parallel;
        * the request is approved once every level is complete.
        """
        newly_pending = self.env['at.approval.approver']
        # sudo: callers checked permissions, the engine writes lines of other users
        for request in self.sudo():
            if request.state not in ('pending', 'approved', 'refused'):
                continue
            lines = request.approver_ids
            undecided = lines.filtered(lambda l: l.status not in ('approved', 'refused'))
            target = {}
            if lines.filtered(lambda l: l.status == 'refused'):
                state = 'refused'
                target = dict.fromkeys(undecided, 'cancel')
            else:
                active_level = None
                for level in sorted(set(lines.mapped('sequence'))):
                    level_done = True
                    for step, step_lines in lines.filtered(lambda l: l.sequence == level).grouped('step_id').items():
                        done = self._step_done(step, step_lines)
                        level_done &= done
                        for line in step_lines & undecided:
                            target[line] = 'waiting' if active_level is not None else 'cancel' if done else 'pending'
                    if active_level is None and not level_done:
                        active_level = level
                state = 'pending' if active_level is not None else 'approved'
            for line, status in target.items():
                if line.status == status:
                    continue
                if line.status == 'pending':
                    request.activity_unlink([ACTIVITY_TYPE], user_id=line.user_id.id)
                line.status = status
                if status == 'pending':
                    newly_pending |= line
            if state != request.state:
                request.state = state
                if state in ('approved', 'refused') and request.category_id.notify_mode != 'activity':
                    request._send_template(state)
        newly_pending._notify_turn()

    def _send_template(self, kind, record=None):
        """Queue the email of ``kind``: request, approved, refused or returned.
        Confidential types always use the default template, which carries no details."""
        self.ensure_one()
        category = self.category_id
        template = self.env.ref(f'at_approval.mail_template_{kind}')
        if not category.is_confidential and category[f'mail_template_{kind}_id']:
            template = category[f'mail_template_{kind}_id']
        try:
            with self.env.cr.savepoint():
                template.sudo().send_mail((record or self).id)
        except Exception:  # noqa: BLE001 - a broken template must never block an approval
            _logger.exception('at_approval: could not queue email %s for request %s', kind, self.id)


class AtApprovalApprover(models.Model):
    _name = 'at.approval.approver'
    _description = 'Approver'
    _order = 'sequence, id'
    _rec_name = 'user_id'

    request_id = fields.Many2one('at.approval.request', required=True, ondelete='cascade', index=True)
    category_id = fields.Many2one(related='request_id.category_id', store=True, index=True)
    step_id = fields.Many2one('at.approval.step', string='Step', ondelete='set null', readonly=True)
    sequence = fields.Integer(default=1000, readonly=True)
    user_id = fields.Many2one(
        'res.users', string='Approver', required=True, index=True, domain=[('share', '=', False)])
    required = fields.Boolean('Must Approve', readonly=True)
    status = fields.Selection([
        ('new', 'New'),
        ('waiting', 'Waiting'),
        ('pending', 'To Approve'),
        ('approved', 'Approved'),
        ('refused', 'Refused'),
        ('cancel', 'Not Needed'),
    ], default='new', required=True, readonly=True, index=True)
    decided_on = fields.Datetime(readonly=True)
    comment = fields.Char()

    @api.model_create_multi
    def create(self, vals_list):
        if not self.env.su:
            # Approvers added by hand join a final level where everyone must approve.
            for vals in vals_list:
                vals.update(step_id=False, sequence=1000, status='new', required=True, decided_on=False)
        lines = super().create(vals_list)
        if not self.env.su:
            lines.request_id._check_can_edit_approvers()
            if any(line.user_id == line.request_id.request_owner_id for line in lines):
                raise UserError(self.env._('The owner of a request cannot approve it.'))
            lines.request_id._sync_state()
        return lines

    def write(self, vals):
        if not self.env.su:
            changed = set(vals)
            if changed <= {'comment'}:
                if any(line.user_id != self.env.user for line in self):
                    raise AccessError(self.env._('You can only comment on your own approval.'))
            elif changed <= {'user_id', 'comment'} and not any(self.mapped('step_id')):
                self.request_id._check_can_edit_approvers()
            else:
                raise AccessError(self.env._('Approvals only change through the buttons of the request.'))
        return super().write(vals)

    def unlink(self):
        requests = self.request_id
        if not self.env.su:
            requests._check_can_edit_approvers()
            if any(self.mapped('step_id')) and not requests._is_officer():
                raise UserError(self.env._('Approvers coming from the approval type cannot be removed.'))
        for line in self.filtered(lambda l: l.status == 'pending'):
            line.request_id.sudo().activity_unlink([ACTIVITY_TYPE], user_id=line.user_id.id)
        res = super().unlink()
        requests._sync_state()
        return res

    def _notify_turn(self):
        """Tell each approver it is their turn, by activity and/or email."""
        for line in self.sudo():
            request = line.request_id
            category = request.category_id
            if category.notify_mode in ('activity', 'both'):
                # With "both", our template is the email: skip Odoo's own "assigned to you" mail.
                request.with_context(mail_activity_quick_update=category.notify_mode == 'both').activity_schedule(
                    ACTIVITY_TYPE, user_id=line.user_id.id,
                    date_deadline=fields.Date.context_today(line) + timedelta(days=category.response_days),
                    summary=self.env._('Approval needed: %s', request.name))
            if category.notify_mode in ('email', 'both'):
                request._send_template('request', line)


class AtApprovalProductLine(models.Model):
    _name = 'at.approval.product.line'
    _description = 'Approval Request Product Line'

    request_id = fields.Many2one('at.approval.request', required=True, ondelete='cascade', index=True)
    product_id = fields.Many2one('product.product', string='Product', required=True)
    description = fields.Char(compute='_compute_description', store=True, readonly=False, precompute=True)
    quantity = fields.Float(default=1.0)
    uom_id = fields.Many2one(
        'uom.uom', string='Unit', compute='_compute_uom_id', store=True, readonly=False, precompute=True)

    @api.depends('product_id')
    def _compute_description(self):
        for line in self:
            line.description = line.product_id.display_name

    @api.depends('product_id')
    def _compute_uom_id(self):
        for line in self:
            line.uom_id = line.product_id.uom_id
