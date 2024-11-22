# -*- coding: utf-8 -*-
{
    'name': "async_pos_reconciliation",

    'summary': """
       Makes the creation of account.move run on an jobrunner""",

    'description': """
        akes the creation of account.move run on an jobrunner
    """,

    'author': "Outsourcearg",
    'website': "https://www.outsourcearg.com",

    # Categories can be used to filter modules in modules listing
    # Check https://github.com/odoo/odoo/blob/master/odoo/addons/base/module/module_data.xml
    # for the full list
    'category': 'Point of sale',
    'version': '1.0',

    # any module necessary for this one to work correctly
    'depends': ['point_of_sale','queue_job'],
    'data' : ['views/poss_session_views.xml'],
}