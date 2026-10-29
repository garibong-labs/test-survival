"""The survival command-line entry point."""
import argparse
import os
from importlib.resources import files
from pathlib import Path
import sys

from .config import ConfigError, STAGES, load
from .io import read_json
from .pipeline import PipelineError, background, run, run_lock, status, stop
from .display import DisplayConfigError, UTC_DISPLAY, load_display_settings


def parser():
    p = argparse.ArgumentParser(prog='survival')
    sub = p.add_subparsers(dest='command', required=True)
    init = sub.add_parser('init', help='Create a commented experiment configuration')
    init.add_argument('exp_dir', type=Path)
    init.add_argument('--language', default='java', choices=['java', 'javascript'])
    init.add_argument('--build', default='maven', choices=['maven', 'npm', 'pnpm'])
    execute = sub.add_parser('run', help='Run or resume experiment stages')
    execute.add_argument('exp_dir', type=Path)
    selection = execute.add_mutually_exclusive_group()
    selection.add_argument('--until', choices=STAGES)
    selection.add_argument('--stage', choices=STAGES)
    execute.add_argument('--force', action='store_true')
    execute.add_argument('--background', action='store_true')
    execute.add_argument('--no-panel', action='store_true', help='Disable the terminal progress panel')
    execute.add_argument('--group')
    execute.add_argument('--lock-fd', type=int, help=argparse.SUPPRESS)
    execute.add_argument('--ready-fd', type=int, help=argparse.SUPPRESS)
    for name in ('show', 'status', 'stop', 'rehash'):
        command = sub.add_parser(name)
        command.add_argument('exp_dir', type=Path)
        if name == 'show':
            command.add_argument('stage', choices=STAGES)
        if name == 'status':
            command.add_argument('--watch', action='store_true', help='Refresh progress every five seconds until the run stops')
            command.add_argument('--no-panel', action='store_true', help='Use plain status output')
    imp = sub.add_parser('import')
    imp.add_argument('exp_dir', type=Path)
    imp.add_argument('--from', dest='source', type=Path, required=True)
    imp.add_argument('--what', required=True)
    policy = sub.add_parser('policy', help='Plan and run checkpoint policies over a completed baseline')
    policy_sub = policy.add_subparsers(dest='policy_command', required=True)
    for name in ('plan', 'run', 'status'):
        command = policy_sub.add_parser(name)
        command.add_argument('exp_dir', type=Path)
        if name == 'run':
            command.add_argument('--background', action='store_true')
            command.add_argument('--lock-fd', type=int, help=argparse.SUPPRESS)
            command.add_argument('--ready-fd', type=int, help=argparse.SUPPRESS)
            command.add_argument('--until', choices=('pools', 'survive', 'cover', 'report'), default='report')
            command.add_argument('--force', action='store_true')
    dashboard = sub.add_parser('dashboard')
    build = dashboard.add_subparsers(dest='dashboard_command', required=True).add_parser('build')
    build.add_argument('--experiments', type=Path, required=True)
    build.add_argument('--out', type=Path, required=True)
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        # The background child writes stdout into state/background.log, a UTC record.
        display = (UTC_DISPLAY if args.command == 'run' and args.lock_fd is not None
                   else load_display_settings())
        if args.command == 'init':
            if (args.language, args.build) != ('java', 'maven'):
                raise ConfigError('project.language/project.build: not implemented in this version of the tool; implemented: java/maven')
            path = args.exp_dir / 'experiment.yaml'
            args.exp_dir.mkdir(parents=True, exist_ok=True)
            with path.open('x') as stream:
                stream.write(files('test_survival').joinpath('experiment-template.yaml').read_text())
            print(path)
        elif args.command == 'policy':
            from .policy.pipeline import command
            command(args)
        elif args.command == 'dashboard':
            from .dashboard.build import build
            build(args.experiments, args.out)
        elif args.command == 'import':
            from .importer import import_experiment
            import_experiment(args.exp_dir, args.source, args.what, display=display)
        elif args.command == 'stop':
            stop(args.exp_dir, display=display)
        else:
            config = load(args.exp_dir)
            if args.command == 'rehash':
                from .pipeline import rehash
                print(f'Rehashed {rehash(args.exp_dir, config)} completed stages')
            elif args.command == 'status':
                if args.watch:
                    from .panel import watch
                    watch(args.exp_dir, config, no_panel=args.no_panel, display=display)
                else:
                    print(status(args.exp_dir, config, display=display))
            elif args.command == 'show':
                from .summary import show
                print(show(args.exp_dir, args.stage))
            else:
                options = {key: getattr(args, key) for key in ('until', 'stage', 'force', 'group')}
                if args.background:
                    pid, log_path = background(args.exp_dir, **options)
                    print(f'PID {pid}; log {log_path}')
                elif args.lock_fd is not None:
                    if args.ready_fd is not None:
                        ready = os.read(args.ready_fd, 1)
                        os.close(args.ready_fd)
                        if ready != b'1':
                            raise PipelineError('Background startup was interrupted')
                    with run_lock(args.exp_dir, args.lock_fd):
                        run(args.exp_dir, config, locked=True, no_panel=args.no_panel, display=display, **options)
                else:
                    run(args.exp_dir, config, no_panel=args.no_panel, display=display, **options)
        return 0
    except (ConfigError, DisplayConfigError, PipelineError, FileExistsError, FileNotFoundError) as exc:
        print(f'survival: {exc}', file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print('survival: interrupted; saved stage state can be resumed', file=sys.stderr)
        return 130
    except Exception as exc:
        print(f'survival: {type(exc).__name__}; see stage evidence', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
