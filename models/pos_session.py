from odoo import models, fields, api, _
from odoo.exceptions import UserError
import logging
from datetime import datetime, timedelta

_logger = logging.getLogger(__name__)


class PosSession(models.Model):
    _inherit = 'pos.session'

    job_id = fields.Char(string="Job ID", readonly=True, help="Identificador del trabajo asincrónico")

    def force_close(self):
        self.state = 'closing_control'
        return

    def _create_account_move(self, balancing_account=False, amount_to_balance=0, bank_payment_method_diffs=None):
        """Crea el asiento contable principal asincrónicamente."""
        self.ensure_one()
        # Fecha y hora actuales en UTC
        ahora = datetime.utcnow()
        
        # Calcular las 0:00 del siguiente día en tu zona horaria (UTC-3)
        ajuste_horario = timedelta(hours=-2)  # UTC-3
        manana = ahora + timedelta(days=1)
        ejecucion_a_medianoche = datetime(manana.year, manana.month, manana.day, 0, 0, 0)
        # Encolar la creación del asiento contable
        job = self.with_delay(priority=10,eta = ejecucion_a_medianoche - ajuste_horario)._async_create_account_move(
            balancing_account=balancing_account,
            amount_to_balance=amount_to_balance,
            bank_payment_method_diffs=bank_payment_method_diffs
        )
        _logger.info(f"Job en cola creado: {job.uuid}")

        # Devuelve el identificador del trabajo
        return {'job_id': job.uuid}

    def _async_create_account_move(self, balancing_account=False, amount_to_balance=0, bank_payment_method_diffs=None):
        """Método para ejecutar asincrónicamente la creación de asientos."""
        _logger.info("Procesando creación de asiento contable en segundo plano.")
        automation_ids = [10, 21, 17]  # Reemplazar con las IDs reales
        Automation = self.env['base.automation']
        
        # Archivar las automatizaciones
        automations = Automation.browse(automation_ids).sudo()
        automations.action_archive()
        account_move = self.env['account.move'].create({
            'journal_id': self.config_id.journal_id.id,
            'date': fields.Date.context_today(self),
            'ref': self.name,
        })
        self.write({'move_id': account_move.id})

        data = {'bank_payment_method_diffs': bank_payment_method_diffs or {}}
        data = self._accumulate_amounts(data)
        data = self._create_non_reconciliable_move_lines(data)
        data = self._create_bank_payment_moves(data)
        data = self._create_pay_later_receivable_lines(data)
        data = self._create_cash_statement_lines_and_cash_move_lines(data)
        data = self._create_invoice_receivable_lines(data)
        data = self._create_stock_output_lines(data)
        if balancing_account and amount_to_balance:
            data = self._create_balancing_line(data, balancing_account, amount_to_balance)

        self._finalize_session_after_async_process(data,automations)

    def _validate_session(self, balancing_account=False, amount_to_balance=0, bank_payment_method_diffs=None):
        """Cierra la sesión inmediatamente y lanza el procesamiento en segundo plano."""
        bank_payment_method_diffs = bank_payment_method_diffs or {}
        self.ensure_one()

        if self.state == 'closed':
            raise UserError(_('This session is already closed.'))

        self._check_if_no_draft_orders()
        self._check_invoices_are_posted()

        if self.update_stock_at_closing:
            self._create_picking_at_end_of_session()
            self._get_closed_orders().filtered(lambda o: not o.is_total_cost_computed)._compute_total_cost_at_session_closing(self.picking_ids.move_ids)

        # Lanza el trabajo en cola para procesar la creación de asientos
        result = self.with_company(self.company_id).with_context(
            check_move_validity=False, skip_invoice_sync=True
        )._create_account_move(balancing_account, amount_to_balance, bank_payment_method_diffs)

        # Guarda el identificador del trabajo
        self.write({'job_id': result['job_id']})

        # Cierra la sesión inmediatamente
        self.write({'state': 'closed'})
        _logger.info("Sesión cerrada de forma inmediata. Trabajo en cola: %s", result['job_id'])
        return True

    def _finalize_session_after_async_process(self, data,automations):
        """Finaliza los procesos restantes después del cierre de la sesión."""
        _logger.info(f"Finalizando procesos pendientes después del cierre de la sesión. {self.move_id}")
        cash_difference_before_statements = self.cash_register_difference
        self.sudo()._post_statement_difference(cash_difference_before_statements, False)

        if self.move_id.line_ids:
            self.move_id.sudo().with_company(self.company_id)._post()
            for dummy, amount_data in data['sales'].items():
                self.env['account.move.line'].browse(amount_data['move_line_id']).sudo().with_company(self.company_id).write({
                    'price_subtotal': abs(amount_data['amount_converted']),
                    'price_total': abs(amount_data['amount_converted']) + abs(amount_data['tax_amount']),
                })
            # Cambiar el estado de los pedidos no facturados a 'done'
            self.env['pos.order'].search([('session_id', '=', self.id), ('state', '=', 'paid')]).write({'state': 'done'})
        else:
            _logger.info("Eliminando movimientos no válidos.")
            self.move_id.sudo().unlink()

        self.sudo().with_company(self.company_id)._reconcile_account_move_lines(data)
        automations.action_unarchive()
        _logger.info("Procesos de reconciliación completados.")
        return
