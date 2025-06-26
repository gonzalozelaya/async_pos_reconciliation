from odoo import models, fields, api, _
from odoo.exceptions import UserError
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo  # Librería para manejar zonas horarias específicas
from odoo.tools import float_is_zero, float_compare, convert
from collections import defaultdict
from odoo.tools.float_utils import float_split_str,float_round


_logger = logging.getLogger(__name__)


class PosSession(models.Model):
    _inherit = 'pos.session'

    job_id = fields.Char(string="Job ID", readonly=True, help="Identificador del trabajo asincrónico")

    def force_close(self):
        self.state = 'closing_control'
        return

    def validate_without_stock(self):
        self.update_stock_at_closing = False
        self.action_pos_session_close()

    def _create_account_move(self, balancing_account=False, amount_to_balance=0, bank_payment_method_diffs=None):
        """Crea el asiento contable principal asincrónicamente."""
        self.ensure_one()
        # Fecha y hora actuales en UTC
        zona_horaria = ZoneInfo("America/Argentina/Buenos_Aires")
        ahora = datetime.now(zona_horaria)
        _logger.info(ahora)
        ajuste_horario = timedelta(hours=3)  # UTC-3
        hoy = ahora.date()
        ejecucion_a_medianoche = datetime(hoy.year, hoy.month, hoy.day, 23, 0, 0)
        # Encolar la creación del asiento contable
        job = self.with_delay(priority=10,eta = ejecucion_a_medianoche + ajuste_horario)._async_create_account_move(
            balancing_account=balancing_account,
            amount_to_balance=amount_to_balance,
            bank_payment_method_diffs=bank_payment_method_diffs,
        )
        _logger.info(f"Job en cola creado: {job.uuid}")

        # Devuelve el identificador del trabajo
        return {'job_id': job.uuid}

    def _async_create_account_move(self , balancing_account=False, amount_to_balance=0,bank_payment_method_diffs=None):
        """Método para ejecutar asincrónicamente la creación de asientos."""
        _logger.info("Procesando creación de asiento contable en segundo plano.")
        Automation = self.env['base.automation']
        payment_date = self.start_at.date() if self.start_at else fields.Date.context_today(self)
        # Archivar las automatizaciones
        all_automations = Automation.sudo().search([])  # Busca todas las automatizaciones
        all_automations.action_archive()
        _logger.info("Todas las automatizaciones han sido archivadas.")
        account_move = self.env['account.move'].create({
            'journal_id': self.config_id.journal_id.id,
            'date': payment_date,
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

        self._finalize_session_after_async_process(data,all_automations)

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
        
        total_debit = sum(line.debit for line in self.move_id.line_ids)
        total_credit = sum(line.credit for line in self.move_id.line_ids)
        difference = round(total_debit - total_credit, 2)
        if difference != 0:
            _logger.info(f"Diferencia detectada: {difference}. Creando línea adicional para balancear el asiento.")
            balance_account = 346
            if not balance_account:
                raise UserError(_("No se configuró una cuenta para diferencias de balance en la compañía."))

            # Crear la línea adicional
            new_line_vals = {
                'move_id': self.move_id.id,
                'name': _("Balance automatico"),
                'account_id': 346,
                'debit': abs(difference) if difference < 0 else 0.0,
                'credit': abs(difference) if difference > 0 else 0.0,
                'partner_id': None,  # Agrega un partner si es necesario
            }
            self.env['account.move.line'].sudo().create(new_line_vals)
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
        transfer_journal = self.env.company.transfer_journal
        if not transfer_journal:
            raise UserError("La compañía no tiene seleccionada una cuenta de transferencias")
        self.reverse_and_reconcile_payments(transfer_journal)
        
        _logger.info("Procesos de reconciliación completados.")
        return

    def reverse_and_reconcile_payments(self, journal_id):
        """Crea asientos de cancelación para los pagos obtenidos y los concilia automáticamente."""
        payments = self.get_payments_by_journal(journal_id)
        _logger.info(f'Pagos a cuenta: {str(payments)}')
    
        # Separar pagos positivos y negativos
        positive_payments = payments.filtered(lambda p: p.amount_signed > 0)
        negative_payments = payments.filtered(lambda p: p.amount_signed < 0)
    
        # Agrupar por partner
        payment_groups = defaultdict(list)
        for payment in positive_payments:
            key = (payment.partner_id.id, float_round(payment.amount_signed, precision_digits=2))
            payment_groups[key].append(payment)
    
        # Lista donde guardaremos los pagos que NO están cancelados
        valid_payments = self.env['account.payment']
    
        # Procesar notas de crédito (pagos negativos)
        used_ncs = set()  # Guardamos las NC usadas para evitar reutilizarlas
        for nc in negative_payments:
            partner_id = nc.partner_id.id
            amount = float_round(abs(nc.amount_signed), precision_digits=2)
    
            key = (partner_id, amount)
            if key in payment_groups and payment_groups[key]:
                # Tomamos el primer pago disponible para cancelar
                cancelled_payment = payment_groups[key].pop(0)
                # No lo agregamos a 'valid_payments'
                # Marcamos esta NC como usada
                used_ncs.add(nc.id)
            # Si no hay pago pendiente para cancelar, simplemente lo ignoramos
    
        # Lo que queda en payment_groups son pagos no cancelados
        for group in payment_groups.values():
            valid_payments |= self.env['account.payment'].concat(*group)
    
        _logger.info(f'Pagos válidos a procesar: {valid_payments.ids}')
    
        if not valid_payments:
            _logger.info("No hay pagos válidos para procesar después de filtrar notas de crédito")
            return
        moves_to_reconcile = self.env['account.move']
        target_journal_id = self.env['account.journal'].sudo().browse(292)
        target_company_id = 1  # ID de la empresa destino
        for payment in valid_payments:
            # Crear asiento inverso
            reversal_move = self.env['account.move'].create({
                'journal_id': payment.journal_id.id,
                'date': fields.Date.context_today(self),
                'ref': f"Reversión de pago {payment.name}",
                'line_ids': [
                    (0, 0, {
                        'name': f"Reversion: {payment.ref}",
                        'account_id': payment.force_outstanding_account_id.id,  # Revertir la cuenta por pagar
                        'debit': 0.0,
                        'credit': payment.amount,
                        'partner_id': payment.partner_id.id,
                    }),
                    (0, 0, {
                        'name': f"Reversion: {payment.ref}",
                        'account_id': journal_id.default_account_id.id,  # Cuenta desde donde vino el pago
                        'debit': payment.amount,
                        'credit': 0.0,
                        'partner_id': payment.partner_id.id,
                    }),
                ]
            })
            target_journal_id = self.env['account.journal'].sudo().search([('id', '=', 292)])
            reversal_move.action_post()  # Publicar el asiento
            moves_to_reconcile |= reversal_move
    
            # Agregar la línea del pago original para conciliar
            moves_to_reconcile |= payment.move_id
            _logger.info(f"Moves to reconcile: {moves_to_reconcile}")
            # Conciliación automática
            moves_to_reconcile.line_ids.filtered(lambda l: l.account_id.id == payment.force_outstanding_account_id.id and not l.reconciled).reconcile()

            # Crear un nuevo asiento en la otra empresa
            cross_company_move = self.env['account.move'].sudo().create({
                'journal_id': target_journal_id.id,  # Diario en la otra empresa
                'date': fields.Date.context_today(self),
                'ref': f"{self.company_id.name}",
                'company_id': 1,  # Empresa destino
                'line_ids': [
                    (0, 0, {
                        'name': f"{self.company_id.name}-{payment.ref}",
                        'account_id': 860,  # Cuenta de la empresa destino
                        'debit': 0.0,
                        'credit': payment.amount,
                        'partner_id': payment.partner_id.id,
                        'company_id': 1,
                    }),
                    (0, 0, {
                        'name': f"{self.company_id.name}-{payment.ref}",
                        'account_id': target_journal_id.default_account_id.id,  # Cuenta en la empresa destino
                        'debit': payment.amount,
                        'credit': 0.0,
                        'partner_id': payment.partner_id.id,
                        'company_id': 1,
                    }),
                ]
            })
            cross_company_move.action_post()  # Publicar el asiento en la otra empresa
            _logger.info(f"Se creó el asiento en la empresa  {cross_company_move.id}")
            
    
    def _create_bank_payment_moves(self, data):
        combine_receivables_bank = data.get('combine_receivables_bank')
        split_receivables_bank = data.get('split_receivables_bank')
        bank_payment_method_diffs = data.get('bank_payment_method_diffs')
        MoveLine = data.get('MoveLine')
        payment_method_to_receivable_lines = {}
        payment_to_receivable_lines = {}
        payment_date = self.start_at.date() if self.start_at else fields.Date.context_today(self)
        for payment_method, amounts in combine_receivables_bank.items():
            combine_receivable_line = MoveLine.create(self._get_combine_receivable_vals(payment_method, amounts['amount'], amounts['amount_converted']))
            payment_receivable_line = self._create_combine_account_payment(payment_method, amounts, payment_date, diff_amount=bank_payment_method_diffs.get(payment_method.id) or 0)
            payment_method_to_receivable_lines[payment_method] = combine_receivable_line | payment_receivable_line
        
        for payment, amounts in split_receivables_bank.items():
            split_receivable_line = MoveLine.create(self._get_split_receivable_vals(payment, amounts['amount'], amounts['amount_converted']))
            payment_receivable_line = self._create_split_account_payment(payment, amounts, payment_date)
            payment_to_receivable_lines[payment] = split_receivable_line | payment_receivable_line
        
        for bank_payment_method in self.payment_method_ids.filtered(lambda pm: pm.type == 'bank' and pm.split_transactions):
            self._create_diff_account_move_for_split_payment_method(bank_payment_method, bank_payment_method_diffs.get(bank_payment_method.id) or 0)
        
        data['payment_method_to_receivable_lines'] = payment_method_to_receivable_lines
        data['payment_to_receivable_lines'] = payment_to_receivable_lines
        return data

    def _create_combine_account_payment(self, payment_method, amounts,payment_date, diff_amount):
        outstanding_account = payment_method.outstanding_account_id or self.company_id.account_journal_payment_debit_account_id
        destination_account = self._get_receivable_account(payment_method)

        if float_compare(amounts['amount'], 0, precision_rounding=self.currency_id.rounding) < 0:
            # revert the accounts because account.payment doesn't accept negative amount.
            outstanding_account, destination_account = destination_account, outstanding_account

        account_payment = self.env['account.payment'].create({
            'amount': abs(amounts['amount']),
            'journal_id': payment_method.journal_id.id,
            'force_outstanding_account_id': outstanding_account.id,
            'destination_account_id':  destination_account.id,
            'ref': _('Combine %s POS payments from %s', payment_method.name, self.name),
            'pos_payment_method_id': payment_method.id,
            'pos_session_id': self.id,
            'company_id': self.company_id.id,
            'date': payment_date,
        })

        diff_amount_compare_to_zero = self.currency_id.compare_amounts(diff_amount, 0)
        if diff_amount_compare_to_zero != 0:
            self._apply_diff_on_account_payment_move(account_payment, payment_method, diff_amount)

        account_payment.action_post()
        return account_payment.move_id.line_ids.filtered(lambda line: line.account_id == account_payment.destination_account_id)

    def _create_split_account_payment(self, payment, amounts,payment_date):
        payment_method = payment.payment_method_id
        if not payment_method.journal_id:
            return self.env['account.move.line']
        outstanding_account = payment_method.outstanding_account_id or self.company_id.account_journal_payment_debit_account_id
        accounting_partner = self.env["res.partner"]._find_accounting_partner(payment.partner_id)
        destination_account = accounting_partner.property_account_receivable_id

        if float_compare(amounts['amount'], 0, precision_rounding=self.currency_id.rounding) < 0:
            # revert the accounts because account.payment doesn't accept negative amount.
            outstanding_account, destination_account = destination_account, outstanding_account

        account_payment = self.env['account.payment'].create({
            'amount': abs(amounts['amount']),
            'partner_id': payment.partner_id.id,
            'journal_id': payment_method.journal_id.id,
            'force_outstanding_account_id': outstanding_account.id,
            'destination_account_id': destination_account.id,
            'ref': _('%s POS payment of %s in %s', payment_method.name, payment.partner_id.display_name, self.name),
            'pos_payment_method_id': payment_method.id,
            'pos_session_id': self.id,
            'company_id': self.company_id.id,
            'date': payment_date,
        })
        account_payment.action_post()
        return account_payment.move_id.line_ids.filtered(lambda line: line.account_id == account_payment.destination_account_id)

    
    def get_payments_by_journal(self, journal_id):
        """Obtiene los pagos de la sesión que están asociados a un diario específico."""
        self.ensure_one()
        payments = self.env['account.payment'].search([
            ('pos_session_id', '=', self.id),
            ('journal_id', 'in', [291]),
            ('state', '=', 'posted')  # Solo pagos confirmados
        ])
        return payments
