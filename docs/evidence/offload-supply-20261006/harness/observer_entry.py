"""Narrow entry adapter; reuse original HTTP runner and ownership machinery."""
from pathlib import Path
import sys

sys.dont_write_bytecode = True
R = Path(__file__).resolve().parent
O = R / 'observer-source'
sys.path.insert(0, str(O / 'tools/evalscope'))


def main():
    stage, cell = sys.argv[1:3]
    if stage not in ('wrapper', 'acceptance'):
        raise ValueError('unknown entry stage')
    cells = ('q01-quality-on', 's01-as-off', 's02-as-on',
             's03-al-on', 's04-al-off')
    if cell not in cells:
        raise ValueError('unknown frozen cell')
    enabled = cell not in ('s01-as-off', 's04-al-off')
    quality = cell == 'q01-quality-on'
    sys.argv = [sys.argv[0], *sys.argv[3:]]
    import mechanism_protocol as old
    import supply_observer_protocol as supply
    old.read_mechanism_sequence = supply.read_supply_sequence
    old.mechanism_environment = supply.supply_environment
    old.check_mechanism_environment = supply.check_supply_environment
    old.mechanism_request_identity = supply.supply_request_identity
    import run_acceptance
    if stage == 'acceptance':
        run_acceptance.main()
        return
    import run_budget_experiment as wrapper
    original_command = wrapper.runner_command

    def environment(chunk_order, inherited, partition=0,
                    policy_axis='chunk-order', phase_diagnostics=False,
                    request_partition=0, decode_partition_log_quiet=0):
        if (chunk_order, partition, policy_axis, request_partition,
                decode_partition_log_quiet) != (0, 0, 'request-partition-log', 0, 0):
            raise ValueError('supply observer requires explicit A000')
        if phase_diagnostics != quality:
            raise ValueError('quality phase flag differs from frozen cell')
        return supply.observer_environment(enabled, inherited)

    def command(args, out, unit, plan):
        if (args.mode == 'quality') != quality:
            raise ValueError('cell mode differs')
        if not quality and (plan['group_id'] != cell or
                plan['supply_observer_enabled'] != enabled):
            raise ValueError('cell sequence differs')
        value = original_command(args, out, unit, plan)
        if value[1] != str(O / 'tools/evalscope/run_acceptance.py'):
            raise ValueError('unexpected original runner')
        return [value[0], '-B', str(Path(__file__).resolve()),
                'acceptance', cell, *value[2:]]

    wrapper.experiment_environment = environment
    wrapper.runner_command = command
    wrapper.main()


if __name__ == '__main__':
    main()
