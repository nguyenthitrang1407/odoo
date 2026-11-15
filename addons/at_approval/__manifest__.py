{
    'name': 'Approvals',
    'version': '19.0.1.0.0',
    'category': 'Human Resources/Approvals',
    'summary': 'Approval requests with configurable types, multi-level steps and rule-based approvers',
    'description': """
Approval requests for Odoo Community.

* Approval types with configurable fields and custom fields (properties)
* Multi-level approval steps, parallel or sequential, with conditions
* Approvers resolved from users, groups, the org chart, job positions or request fields
* Confidential types for HR and salary requests
* Activities and per-type email templates
""",
    'author': 'ATTECH',
    'license': 'LGPL-3',
    'depends': ['mail', 'hr', 'product'],
    'data': [
        'security/security.xml',
        'security/ir.model.access.csv',
        'data/mail_data.xml',
        'wizard/return_wizard_views.xml',
        'views/approval_category_views.xml',
        'views/approval_request_views.xml',
        'views/menus.xml',
        'data/approval_category_data.xml',
    ],
    'application': True,
    'installable': True,
}
