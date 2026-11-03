import unittest
from tools.serve_startup_memory import correction
from tools.serve_admission import verify as base


class WorkerAccounting(unittest.TestCase):
    def rows(self):
        return dict({k:0 for k in base.EMPTY},q4t_requests_total=5,q4t_requests_success_total=5,
            q4t_requests_error_total=0,q4t_requests_aborted_total=0,
            q4t_structured_workers_started_total=3,q4t_structured_workers_ready_total=3,
            q4t_structured_workers_reaped_total=3,q4t_seq_slots_total=1,q4t_seq_slots_free=1,q4t_ready=1)

    def test_three_constrained_turns_are_counted(self):
        correction.accounting_contract(self.rows())
        rows=self.rows();rows['q4t_structured_workers_started_total']=2
        with self.assertRaises(RuntimeError):correction.accounting_contract(rows)

    def test_unreaped_or_failed_turn_cannot_pass(self):
        for name,value in (('q4t_structured_workers_reaped_total',2),('q4t_requests_error_total',1),
                           ('q4t_requests_success_total',4),('q4t_active_chats',1)):
            rows=self.rows();rows[name]=value
            with self.assertRaises(RuntimeError):correction.accounting_contract(rows)


if __name__=='__main__':unittest.main()
