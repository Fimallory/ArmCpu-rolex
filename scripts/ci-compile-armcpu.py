#!/usr/bin/env python3
"""Compile ArmCpu against selected XNU records, without prelink or boot inputs."""
import argparse
import contextlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys


FORK = Path(__file__).resolve().parents[1]
REFERENCES = {'xnu', 'libplatform', 'libdispatch', 'AvailabilityVersions'}
OBJECT_DIR = Path('build/xnu/ios-obj/DEVELOPMENT_ARM64_BCM2837')
RECORDS = (
    OBJECT_DIR / 'osfmk/DEVELOPMENT/startup.o.json',
    OBJECT_DIR / 'iokit/DEVELOPMENT/IOPlatformExpert.cpo.json',
)
LINK_FILELIST = OBJECT_DIR / 'link.filelist'


def arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--unixnu', type=Path, default=FORK.parent / 'UniXNU')
    parser.add_argument('--device', type=Path, required=True)
    parser.add_argument('--soc', type=Path, required=True)
    parser.add_argument('--armcpu', type=Path, default=FORK)
    parser.add_argument('--defconfig', default='rolex_defconfig')
    parser.add_argument('--out', type=Path, default=FORK / 'out/abi-work')
    parser.add_argument('--artifacts', type=Path, default=FORK / 'out/abi-compile')
    parser.add_argument('-j', '--jobs', type=int, default=4)
    return parser.parse_args()


def git_output(directory, *args):
    return subprocess.check_output(
        ['git', '-C', str(directory), *args], text=True, stderr=subprocess.PIPE,
    ).strip()


def selected_submodules(ctx, specs, overrides):
    """Initialize only selected component paths; never use the global updater."""
    names = {spec['id'] for spec in specs} - overrides
    for base in (ctx.root, ctx.device, ctx.soc.path):
        if not (base / '.gitmodules').is_file():
            continue
        result = subprocess.run(
            ['git', '-C', str(base), 'config', '-f', '.gitmodules',
             '--get-regexp', r'^submodule\..*\.path$'],
            capture_output=True, text=True,
        )
        if result.returncode not in (0, 1):
            raise RuntimeError(f'Cannot read selected submodules: {result.stderr.strip()}')
        paths = []
        for line in result.stdout.splitlines():
            _, relative = line.split(None, 1)
            path = Path(relative)
            if path.name not in names:
                continue
            if path.is_absolute() or '..' in path.parts:
                raise RuntimeError(f'Unsafe selected submodule path: {relative}')
            paths.append(relative)
        if paths:
            print(f'Initialize selected submodules in {base}: {", ".join(paths)}', flush=True)
            subprocess.run(
                ['git', '-C', str(base), 'submodule', 'update', '--init',
                 '--depth', '1', '--', *sorted(paths)], check=True,
            )


def validate_records(tree):
    for relative in RECORDS:
        path = tree / relative
        if not path.is_file():
            raise RuntimeError(f'Bootstrap compile record missing: {relative}')
        record = json.loads(path.read_text().strip().rstrip(','))
        command = record.get('arguments')
        if not isinstance(command, list) or not command or not all(isinstance(x, str) for x in command):
            raise RuntimeError(f'Invalid compile arguments: {relative}')
        if not record.get('file') or not record.get('directory') or not Path(record['directory']).is_dir():
            raise RuntimeError(f'Invalid compile source/directory: {relative}')
    filelist = tree / LINK_FILELIST
    if not filelist.is_file() or not filelist.read_text().strip():
        raise RuntimeError(f'Bootstrap link file list missing or empty: {LINK_FILELIST}')


def expected_link_failure(exit_code, text):
    """Accept make's failure only with the known missing-archive link evidence."""
    undefined = re.search(r'Undefined symbols for architecture arm64\s*:', text)
    linker = re.search(r'(?:linker command failed|symbol\(s\) not found for architecture arm64)', text)
    kernel_target = any(
        'make' in line and '***' in line and 'Error' in line and 'mach.development.bcm2837' in line
        for line in text.splitlines()
    )
    compile_error = re.search(r'^.*:\d+(?::\d+)?:\s+(?:fatal )?error:|fatal error:', text, re.MULTILINE)
    driver_errors = [line for line in text.splitlines()
                     if re.search(r'^clang(?:\+\+)?(?:-\d+)?: error:', line)
                     and 'linker command failed' not in line]
    return exit_code == 2 and bool(undefined and linker and kernel_target) and not compile_error and not driver_errors


def run_logged(command, cwd, env, path):
    print('+ ' + ' '.join(str(part) for part in command), flush=True)
    with path.open('w', encoding='utf-8') as stream:
        result = subprocess.run(
            [str(part) for part in command], cwd=cwd, env=env,
            stdout=stream, stderr=subprocess.STDOUT,
        )
    return result.returncode


def redact_logs(artifacts):
    tokens = [os.environ.get(name) for name in ('GITHUB_TOKEN', 'GH_TOKEN')]
    tokens = [value for value in tokens if value]
    for path in artifacts.iterdir():
        if path.suffix not in ('.log', '.json'):
            continue
        text = path.read_text(errors='replace')
        for token in tokens:
            text = text.replace(token, '[redacted]')
        path.write_text(text, encoding='utf-8')


def main():
    args = arguments()
    if platform.system() != 'Darwin':
        raise RuntimeError('This helper requires macOS with Xcode; it does not install a Linux toolchain.')
    if args.jobs < 1:
        raise RuntimeError('--jobs must be positive')
    developer = os.environ.get('DEVELOPER_DIR', '')
    if not developer or not Path(developer).is_dir():
        raise RuntimeError('DEVELOPER_DIR must name an installed Xcode Contents/Developer directory')
    for name in ('unixnu', 'device', 'soc', 'armcpu', 'out', 'artifacts'):
        setattr(args, name, getattr(args, name).resolve())
    for path in (args.unixnu / 'scripts/unixnu/cli.py', args.device / 'board.toml',
                 args.soc / 'soc.toml', args.armcpu / 'ArmCpu.cpp'):
        if not path.is_file():
            raise RuntimeError(f'Required source input missing: {path}')
    if args.artifacts.exists() and any(args.artifacts.iterdir()):
        raise RuntimeError('Artifact directory is not empty; select a fresh --artifacts directory')
    args.artifacts.mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(args.unixnu / 'scripts'))
    from unixnu import components, feeds, kernel, patches, tree
    from unixnu.cli import cmd_defconfig, parser
    from unixnu.config import Context, load_toml, parse_config

    report = {'status': 'preparing', 'scope': 'ArmCpu ARM64/iOS 17 archive only',
              'developer_dir': developer, 'jobs': args.jobs}
    report_path = args.artifacts / 'build-report.json'
    ctx = None
    try:
        with (args.artifacts / 'prepare.log').open('w', encoding='utf-8') as log:
            with contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
                soc_name = load_toml(args.device / 'board.toml')['board']['soc_source']
                options = parser().parse_args([
                    'defconfig', args.defconfig, '-D', str(args.device), '--out', str(args.out),
                    '-j', str(args.jobs), '--local-module', f'driver:ArmCpu={args.armcpu}',
                    '--local-module', f'soc:{soc_name}={args.soc}',
                ])
                ctx = Context(options)
                if ctx.tree.exists():
                    raise RuntimeError('Build tree already exists; select a fresh --out directory')
                cmd_defconfig(ctx)
                # Context caches .config at construction, before cmd_defconfig
                # writes the selected target file. Re-read it so the bootstrap
                # pass and the reported flags use the real board configuration.
                ctx.config = parse_config(ctx.config_path.read_text())
                specs = components.selected(ctx)
                if 'ArmCpu' not in {spec['id'] for spec in specs}:
                    raise RuntimeError('The selected board must include the ArmCpu component')
                selected_submodules(ctx, specs, {'ArmCpu'})
                modules = feeds.resolve(ctx)
                modules = {key: value for key, value in modules.items()
                           if key[0] != 'refer' or key[1] in REFERENCES}
                ctx.modules = modules
                feeds.fetch(ctx, modules)
                missing = REFERENCES - {name for kind, name in modules if kind == 'refer'}
                if missing:
                    raise RuntimeError('Required XNU feeds missing: ' + ', '.join(sorted(missing)))
                tree.assemble(ctx, modules)
                env = dict(os.environ)
                env.update(kernel.xnu_env(ctx))
                env.update(kernel.version_env(modules, ctx))
                env.update({'XNU_BOOTSTRAP': '1', 'XNU_BOOT_PROGRESS': '0',
                            'XNU_BUILD_TARGET': 'build', 'JOBS': str(args.jobs)})
                # This pass builds no runtime progress layer, and the shared
                # zzz-boot-progress patch currently fails to apply to the
                # pinned bsd_init.c context. Exclude that one patch from this
                # compile-only list; the full kernel build must rebase it.
                # Filtering the planner keeps the rest of the shared ordering.
                plain_plan = patches.plan
                patches.plan = lambda context, mods: [
                    path for path in plain_plan(context, mods)
                    if path.name != 'zzz-boot-progress.patch']
                env['UNIXNU_PATCH_LIST'] = str(patches.prepare(ctx, modules))
                patches.plan = plain_plan
                report['excluded_patches'] = ['zzz-boot-progress.patch']
                generated = ctx.tree / 'build/generated'
                generated.mkdir(parents=True, exist_ok=True)
                env['UNIXNU_GENERATED_DIR'] = str(generated)
                report['output'] = str(ctx.work)
                report['sources'] = {
                    name: git_output(path, 'rev-parse', 'HEAD')
                    for name, path in (('UniXNU', args.unixnu), ('device', args.device),
                                       ('soc', args.soc), ('ArmCpu', args.armcpu))
                }
                report['modules'] = [
                    {'kind': kind, 'name': name, 'revision': git_output(module.path, 'rev-parse', 'HEAD')}
                    for (kind, name), module in sorted(modules.items())
                    if kind in ('refer', 'driver', 'kit') and module.path is not None
                ]
                report['kernel_flags'] = kernel.xnu_env(ctx)
        report['status'] = 'bootstrap'
        report_path.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
        bootstrap_log = args.artifacts / 'kernel-bootstrap.log'
        exit_code = run_logged(
            ['bash', ctx.tree / 'scripts/build-xnu-4k-baseline.sh'], ctx.tree, env, bootstrap_log,
        )
        report['bootstrap_exit_code'] = exit_code
        validate_records(ctx.tree)
        if exit_code and not expected_link_failure(exit_code, bootstrap_log.read_text(errors='replace')):
            raise RuntimeError(f'Unexpected bootstrap failure ({exit_code}); inspect kernel-bootstrap.log')
        report['bootstrap_result'] = 'success' if exit_code == 0 else 'expected undefined-symbol kernel link failure'
        report['status'] = 'compiling ArmCpu'
        result = run_logged(
            [sys.executable, ctx.tree / 'scripts/build-arm-driver.py', 'ArmCpu'],
            ctx.tree, env, args.artifacts / 'armcpu-driver.log',
        )
        if result:
            raise RuntimeError(f'ArmCpu compilation failed ({result}); inspect compile.log and armcpu-driver.log')
        for name in ('libArmCpu.a', 'ArmCpu.o', 'compile.log', 'compile-command.json'):
            source = ctx.tree / 'build/armcpu' / name
            if not source.is_file():
                raise RuntimeError(f'ArmCpu output missing: {source}')
            shutil.copy2(source, args.artifacts / name)
        report['status'] = 'compiled'
        report['level'] = 'Lv1: compiled; no kernel prelink or runtime validation'
    except Exception as error:
        report['status'] = 'failed'
        report['error'] = str(error)
        raise
    finally:
        if ctx is not None:
            for name in ('compile.log', 'compile-command.json'):
                source = ctx.tree / 'build/armcpu' / name
                if source.is_file():
                    shutil.copy2(source, args.artifacts / name)
        report_path.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
        redact_logs(args.artifacts)
    print(f'ArmCpu ABI compilation complete: {args.artifacts}')


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(f'ci-compile-armcpu: {error}', file=sys.stderr)
        sys.exit(1)
