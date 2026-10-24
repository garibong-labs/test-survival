"""The survival command-line entry point."""
import argparse
import os
from importlib.resources import files
from pathlib import Path
import sys

from .config import ConfigError, STAGES, load
from .io import read_json
from .pipeline import PipelineError, background, run, run_lock, status


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
    execute.add_argument('--group')
    execute.add_argument('--lock-fd', type=int, help=argparse.SUPPRESS)
    execute.add_argument('--ready-fd', type=int, help=argparse.SUPPRESS)
    for name in ('show', 'status'):
        command = sub.add_parser(name)
        command.add_argument('exp_dir', type=Path)
        if name == 'show':
            command.add_argument('stage', choices=STAGES)
    imp = sub.add_parser('import')
    imp.add_argument('exp_dir', type=Path)
    imp.add_argument('--from', dest='source', type=Path, required=True)
    imp.add_argument('--what', required=True)
    dashboard = sub.add_parser('dashboard')
    build = dashboard.add_subparsers(dest='dashboard_command', required=True).add_parser('build')
    build.add_argument('--experiments', type=Path, required=True)
    build.add_argument('--out', type=Path, required=True)
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command == 'init':
            if (args.language, args.build) != ('java', 'maven'):
                raise ConfigError('project.language/project.build: not implemented in this version of the tool; implemented: java/maven')
            path = args.exp_dir / 'experiment.yaml'
            args.exp_dir.mkdir(parents=True, exist_ok=True)
            with path.open('x') as stream:
                stream.write(files('test_survival').joinpath('experiment-template.yaml').read_text())
            print(path)
        elif args.command == 'dashboard':
            from .dashboard.build import build
            build(args.experiments, args.out)
        elif args.command == 'import':
            from .importer import import_experiment
            import_experiment(args.exp_dir, args.source, args.what)
        else:
            config = load(args.exp_dir)
            if args.command == 'status':
                print(status(args.exp_dir, config))
            elif args.command == 'show':
                state = read_json(args.exp_dir / 'state' / f'{args.stage}.json', {})
                print(f'{args.stage}: {state.get("status", "pending")}\n{state.get("notes", "")}')
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
                        run(args.exp_dir, config, locked=True, **options)
                else:
                    run(args.exp_dir, config, **options)
        return 0
    except (ConfigError, PipelineError, FileExistsError, FileNotFoundError) as exc:
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
