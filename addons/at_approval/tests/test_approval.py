from odoo.exceptions import AccessError, UserError
from odoo.tests import new_test_user, tagged
from odoo.tests.common import TransactionCase

ACTIVITY_TYPE = 'at_approval.mail_activity_type_approval'


@tagged('post_install', '-at_install', 'at_approval')
class TestApproval(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env = cls.env(context=dict(cls.env.context, tracking_disable=False))
        users = {}
        for login in ('owner', 'manager', 'director', 'hr', 'acc', 'buyer1', 'buyer2', 'buyer3', 'outsider'):
            users[login] = new_test_user(cls.env, login=f'at_{login}', groups='base.group_user', name=login.title())
        cls.users = users
        cls.officer = new_test_user(cls.env, login='at_officer', groups='at_approval.group_approval_officer', name='Officer')
        Employee = cls.env['hr.employee']
        cls.department = cls.env['hr.department'].create({'name': 'Sales'})
        cls.emp_director = Employee.create({'name': 'Director', 'user_id': users['director'].id})
        cls.emp_manager = Employee.create({
            'name': 'Manager', 'user_id': users['manager'].id, 'parent_id': cls.emp_director.id})
        cls.department.manager_id = cls.emp_manager
        cls.emp_owner = Employee.create({
            'name': 'Owner', 'user_id': users['owner'].id, 'parent_id': cls.emp_manager.id,
            'department_id': cls.department.id})
        cls.emp_hr = Employee.create({'name': 'Hr', 'user_id': users['hr'].id, 'parent_id': cls.emp_director.id})
        cls.buyers = users['buyer1'] | users['buyer2'] | users['buyer3']

    # -- helpers ---------------------------------------------------------

    def _category(self, steps, **vals):
        return self.env['at.approval.category'].create({
            'name': 'Test type', 'notify_mode': 'both', **vals,
            'step_ids': [(0, 0, step) for step in steps],
        })

    def _request(self, category, owner='owner', **vals):
        return self.env['at.approval.request'].with_user(self.users[owner]).create({
            'name': 'Test request', 'category_id': category.id, **vals})

    def _as(self, request, login):
        return request.with_user(self.users[login])

    def _statuses(self, request):
        return {line.user_id.login.removeprefix('at_'): line.status for line in request.sudo().approver_ids}

    # -- completion rules --------------------------------------------------

    def test_any_rule_completes_on_first_approval(self):
        category = self._category([
            {'name': 'Buyers', 'approver_type': 'user', 'user_ids': [(6, 0, self.buyers.ids)], 'rule': 'any'},
        ])
        request = self._request(category)
        request.action_submit()
        self.assertEqual(request.state, 'pending')
        self.assertEqual(set(self._statuses(request).values()), {'pending'})
        self._as(request, 'buyer2').action_approve()
        self.assertEqual(request.state, 'approved')
        self.assertEqual(self._statuses(request), {'buyer1': 'cancel', 'buyer2': 'approved', 'buyer3': 'cancel'})

    def test_minimum_rule(self):
        category = self._category([{
            'name': 'Board', 'approver_type': 'user', 'rule': 'min', 'min_count': 2,
            'user_ids': [(6, 0, (self.buyers | self.users['hr']).ids)],
        }])
        request = self._request(category)
        request.action_submit()
        self._as(request, 'buyer1').action_approve()
        self.assertEqual(request.state, 'pending')
        self._as(request, 'hr').action_approve()
        self.assertEqual(request.state, 'approved')
        self.assertEqual(self._statuses(request)['buyer2'], 'cancel')

    def test_required_user_must_approve(self):
        category = self._category([{
            'name': 'Buyers', 'approver_type': 'user', 'rule': 'any',
            'user_ids': [(6, 0, self.buyers.ids)], 'required_user_ids': [(6, 0, self.users['buyer3'].ids)],
        }])
        request = self._request(category)
        request.action_submit()
        self._as(request, 'buyer1').action_approve()
        self.assertEqual(request.state, 'pending', 'the required approver has not approved yet')
        self._as(request, 'buyer3').action_approve()
        self.assertEqual(request.state, 'approved')

    def test_one_refusal_refuses_then_withdraw_reopens(self):
        category = self._category([
            {'name': 'Buyers', 'approver_type': 'user', 'user_ids': [(6, 0, self.buyers.ids)], 'rule': 'all'},
        ])
        request = self._request(category)
        request.action_submit()
        self._as(request, 'buyer1').action_refuse()
        self.assertEqual(request.state, 'refused')
        self.assertEqual(self._statuses(request), {'buyer1': 'refused', 'buyer2': 'cancel', 'buyer3': 'cancel'})
        self._as(request, 'buyer1').action_withdraw()
        self.assertEqual(request.state, 'pending')
        self.assertEqual(set(self._statuses(request).values()), {'pending'})

    def test_withdraw_after_approval_reopens(self):
        category = self._category([{'name': 'Manager', 'approver_type': 'manager'}])
        request = self._request(category)
        request.action_submit()
        self._as(request, 'manager').action_approve()
        self.assertEqual(request.state, 'approved')
        self._as(request, 'manager').action_withdraw()
        self.assertEqual(request.state, 'pending')
        self.assertEqual(self._statuses(request), {'manager': 'pending'})

    # -- levels ------------------------------------------------------------

    def test_sequential_levels(self):
        category = self._category([
            {'name': 'Manager', 'sequence': 10, 'approver_type': 'manager'},
            {'name': 'Director', 'sequence': 20, 'approver_type': 'manager_level', 'level': 2},
        ])
        request = self._request(category)
        request.action_submit()
        self.assertEqual(self._statuses(request), {'manager': 'pending', 'director': 'waiting'})
        with self.assertRaises(UserError):
            self._as(request, 'director').action_approve()
        self._as(request, 'manager').action_approve()
        self.assertEqual(self._statuses(request), {'manager': 'approved', 'director': 'pending'})
        self._as(request, 'director').action_approve()
        self.assertEqual(request.state, 'approved')

    def test_parallel_steps_share_a_level(self):
        category = self._category([
            {'name': 'HR', 'sequence': 10, 'approver_type': 'user', 'user_ids': [(6, 0, self.users['hr'].ids)]},
            {'name': 'Accounting', 'sequence': 10, 'approver_type': 'user', 'user_ids': [(6, 0, self.users['acc'].ids)]},
            {'name': 'Director', 'sequence': 20, 'approver_type': 'user', 'user_ids': [(6, 0, self.users['director'].ids)]},
        ])
        request = self._request(category)
        request.action_submit()
        self.assertEqual(self._statuses(request), {'hr': 'pending', 'acc': 'pending', 'director': 'waiting'})
        self._as(request, 'hr').action_approve()
        self.assertEqual(self._statuses(request)['director'], 'waiting')
        self._as(request, 'acc').action_approve()
        self.assertEqual(self._statuses(request)['director'], 'pending')

    def test_conditional_step_on_amount(self):
        category = self._category([
            {'name': 'Manager', 'sequence': 10, 'approver_type': 'manager'},
            {'name': 'Director', 'sequence': 20, 'approver_type': 'user',
             'user_ids': [(6, 0, self.users['director'].ids)], 'domain': "[('amount', '>', 50000000)]"},
        ], has_amount='required')
        small = self._request(category, amount=30000000)
        small.action_submit()
        self.assertEqual(self._statuses(small), {'manager': 'pending'})
        big = self._request(category, amount=80000000)
        big.action_submit()
        self.assertEqual(self._statuses(big), {'manager': 'pending', 'director': 'waiting'})

    def test_conditional_step_on_custom_field(self):
        category = self._category([
            {'name': 'Manager', 'sequence': 10, 'approver_type': 'manager'},
            {'name': 'Director', 'sequence': 20, 'approver_type': 'user',
             'user_ids': [(6, 0, self.users['director'].ids)],
             'domain': "[('request_properties.proposed_salary', '>', 1000)]"},
        ], request_properties_definition=[{'name': 'proposed_salary', 'string': 'Proposed Salary', 'type': 'float'}])
        request = self._request(category, request_properties={'proposed_salary': 5000})
        request.action_submit()
        self.assertEqual(self._statuses(request), {'manager': 'pending', 'director': 'waiting'})
        request = self._request(category, request_properties={'proposed_salary': 500})
        request.action_submit()
        self.assertEqual(self._statuses(request), {'manager': 'pending'})

    # -- approver resolution -------------------------------------------------

    def test_missing_approver_blocks_or_skips(self):
        step = {'name': 'Level 3', 'sequence': 20, 'approver_type': 'manager_level', 'level': 3}
        manager_step = {'name': 'Manager', 'sequence': 10, 'approver_type': 'manager'}
        blocking = self._category([manager_step, dict(step, on_missing='block')])
        with self.assertRaises(UserError):
            self._request(blocking).action_submit()
        skipping = self._category([manager_step, dict(step, on_missing='skip')])
        request = self._request(skipping)
        request.action_submit()
        self.assertEqual(self._statuses(request), {'manager': 'pending'})

    def test_no_approver_at_all_blocks_submission(self):
        with self.assertRaises(UserError):
            self._request(self._category([])).action_submit()

    def test_owner_never_approves_own_request(self):
        category = self._category([{
            'name': 'Peers', 'approver_type': 'user',
            'user_ids': [(6, 0, (self.users['owner'] | self.users['hr']).ids)],
        }])
        request = self._request(category)
        request.action_submit()
        self.assertEqual(self._statuses(request), {'hr': 'pending'})

    def test_routing_follows_employee_concerned(self):
        """HR files a raise for Owner: Owner's manager approves, not HR's."""
        category = self._category([{'name': 'Manager', 'approver_type': 'manager'}], has_employee='required')
        request = self._request(category, owner='hr', employee_id=self.emp_owner.id)
        request.action_submit()
        self.assertEqual(self._statuses(request), {'manager': 'pending'})

    def test_department_head_group_and_job(self):
        job = self.env['hr.job'].create({'name': 'Chief Accountant'})
        self.env['hr.employee'].create({'name': 'Acc', 'user_id': self.users['acc'].id, 'job_id': job.id})
        group = self.env['res.groups'].create({'name': 'Buyers', 'user_ids': [(6, 0, self.buyers.ids)]})
        category = self._category([
            {'name': 'Head', 'sequence': 10, 'approver_type': 'department_manager'},
            {'name': 'Buyers', 'sequence': 20, 'approver_type': 'group', 'group_id': group.id, 'rule': 'any'},
            {'name': 'Accountant', 'sequence': 30, 'approver_type': 'job', 'job_id': job.id},
        ])
        request = self._request(category)
        request.action_submit()
        self.assertEqual(self._statuses(request), {
            'manager': 'pending', 'buyer1': 'waiting', 'buyer2': 'waiting', 'buyer3': 'waiting', 'acc': 'waiting'})

    # -- lifecycle -------------------------------------------------------------

    def test_subject_defaults_to_type_name(self):
        category = self._category([{'name': 'Manager', 'approver_type': 'manager'}])
        request = self.env['at.approval.request'].with_user(self.users['owner']).create({'category_id': category.id})
        self.assertEqual(request.name, 'Test type')

    def test_required_fields_and_numbering(self):
        category = self._category(
            [{'name': 'Manager', 'approver_type': 'manager'}],
            has_amount='required', has_location='required', use_sequence=True, sequence_code='XX')
        request = self._request(category)
        with self.assertRaises(UserError):
            request.action_submit()
        request.write({'amount': 10, 'location': 'Hanoi'})
        self.assertFalse(request.code)
        request.action_submit()
        self.assertTrue(request.code.startswith('XX/'))

    def test_return_for_revision_restarts_from_first_level(self):
        category = self._category([
            {'name': 'Manager', 'sequence': 10, 'approver_type': 'manager'},
            {'name': 'Director', 'sequence': 20, 'approver_type': 'manager_level', 'level': 2},
        ])
        request = self._request(category)
        request.action_submit()
        self._as(request, 'manager').action_approve()
        self._as(request, 'director')._return_to_owner('Missing budget line')
        self.assertEqual(request.state, 'draft')
        self.assertEqual(request.return_reason, 'Missing budget line')
        self.assertFalse(request.sudo().approver_ids)
        request.action_submit()
        self.assertEqual(self._statuses(request), {'manager': 'pending', 'director': 'waiting'})

    def test_cancel_and_back_to_draft(self):
        category = self._category([{'name': 'Manager', 'approver_type': 'manager'}])
        request = self._request(category)
        request.action_submit()
        with self.assertRaises(AccessError):
            self._as(request, 'manager').action_cancel()
        request.action_cancel()
        self.assertEqual(request.state, 'cancel')
        self.assertFalse(request.sudo().activity_ids)
        request.action_draft()
        self.assertEqual(request.state, 'draft')
        with self.assertRaises(UserError):
            self._submitted(category).unlink()

    def _submitted(self, category):
        request = self._request(category)
        request.action_submit()
        return request

    def test_force_approval_is_for_officers(self):
        category = self._category([
            {'name': 'Buyers', 'approver_type': 'user', 'user_ids': [(6, 0, self.buyers.ids)], 'rule': 'all'},
        ])
        request = self._submitted(category)
        with self.assertRaises(AccessError):
            request.action_force_approve()
        request.with_user(self.officer).action_force_approve()
        self.assertEqual(request.state, 'approved')

    def test_manual_approver_joins_last_level(self):
        category = self._category([{'name': 'Manager', 'approver_type': 'manager'}])
        request = self._request(category, approver_ids=[(0, 0, {'user_id': self.users['hr'].id})])
        request.action_submit()
        self.assertEqual(self._statuses(request), {'manager': 'pending', 'hr': 'waiting'})
        self._as(request, 'manager').action_approve()
        self.assertEqual(request.state, 'pending')
        self._as(request, 'hr').action_approve()
        self.assertEqual(request.state, 'approved')

    # -- notifications -----------------------------------------------------------

    def test_activity_and_email_follow_the_turn(self):
        category = self._category([
            {'name': 'Manager', 'sequence': 10, 'approver_type': 'manager'},
            {'name': 'Director', 'sequence': 20, 'approver_type': 'manager_level', 'level': 2},
        ])
        mails_before = self.env['mail.mail'].sudo().search_count([])
        request = self._submitted(category)
        activities = request.sudo().activity_ids
        self.assertEqual(activities.user_id, self.users['manager'], 'only the current level is notified')
        self.assertEqual(self.env['mail.mail'].sudo().search_count([]), mails_before + 1)
        self._as(request, 'manager').action_approve()
        self.assertEqual(request.sudo().activity_ids.user_id, self.users['director'])
        self._as(request, 'director').action_approve()
        self.assertFalse(request.sudo().activity_ids)
        # manager turn, director turn, approved notice to the owner
        self.assertEqual(self.env['mail.mail'].sudo().search_count([]), mails_before + 3)

    def test_custom_template_and_confidential_fallback(self):
        template = self.env.ref('at_approval.mail_template_request').copy({'subject': 'CUSTOM {{ object.request_id.name }}'})
        steps = [{'name': 'Manager', 'approver_type': 'manager'}]
        Mail = self.env['mail.mail'].sudo()
        self._submitted(self._category(steps, mail_template_request_id=template.id))
        self.assertTrue(Mail.search([], order='id desc', limit=1).subject.startswith('CUSTOM'))
        self._submitted(self._category(steps, mail_template_request_id=template.id, is_confidential=True))
        self.assertFalse(Mail.search([], order='id desc', limit=1).subject.startswith('CUSTOM'))

    # -- access ------------------------------------------------------------------

    def test_users_only_see_their_requests(self):
        request = self._submitted(self._category([{'name': 'Manager', 'approver_type': 'manager'}]))
        Request = self.env['at.approval.request']
        self.assertIn(request, Request.with_user(self.users['manager']).search([]))
        self.assertNotIn(request, Request.with_user(self.users['outsider']).search([]))
        with self.assertRaises(AccessError):
            self._as(request, 'outsider').read(['name'])
        Approver = self.env['at.approval.approver']
        self.assertFalse(Approver.with_user(self.users['outsider']).search([('request_id', '=', request.id)]))
        self.assertTrue(Approver.with_user(self.users['manager']).search([('request_id', '=', request.id)]))
        self.assertIn(request, Request.with_user(self.officer).search([]))

    def test_confidential_requests_hidden_from_officers(self):
        group = self.env['res.groups'].create({'name': 'Payroll', 'user_ids': [(6, 0, self.users['hr'].ids)]})
        category = self._category(
            [{'name': 'Manager', 'approver_type': 'manager'}],
            is_confidential=True, confidential_group_ids=[(6, 0, group.ids)], has_employee='required')
        request = self._request(category, owner='director', employee_id=self.emp_owner.id)
        request.action_submit()
        with self.assertRaises(AccessError):
            request.with_user(self.officer).read(['name'])
        with self.assertRaises(AccessError):
            self._as(request, 'owner').read(['name'])  # the employee concerned
        self.assertTrue(self._as(request, 'hr').read(['name']))  # member of the allowed group
        self.assertEqual(category.with_user(self.officer).to_review_count, 0)
        category.employee_can_read = True
        self.assertTrue(self._as(request, 'owner').read(['name']))

    def test_approver_cannot_edit_the_request(self):
        request = self._submitted(self._category([{'name': 'Manager', 'approver_type': 'manager'}]))
        with self.assertRaises(UserError):
            self._as(request, 'manager').write({'name': 'Changed'})
        with self.assertRaises(UserError):
            request.write({'name': 'Changed after submit'})
        with self.assertRaises(AccessError):
            request.write({'state': 'approved'})
        line = request.sudo().approver_ids.with_user(self.users['manager'])
        with self.assertRaises(AccessError):
            line.write({'status': 'approved'})
        line.write({'comment': 'Looks fine'})
        with self.assertRaises(AccessError):
            self.env['at.approval.request'].with_user(self.users['outsider']).create({
                'name': 'For someone else', 'category_id': request.category_id.id,
                'request_owner_id': self.users['owner'].id})
