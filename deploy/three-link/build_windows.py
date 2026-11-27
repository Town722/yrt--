"""Build the Windows ground station and serial bench from repository sources."""

import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile


SOURCE = Path(__file__).resolve().parent
REPO = SOURCE.parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=REPO / 'dist')
    parser.add_argument('--runtime-temp', type=Path, help='Optional one-file extraction directory')
    args = parser.parse_args()
    if sys.platform != 'win32':
        parser.error('Build these executables on Windows.')

    config = SOURCE / 'windows.json'
    if json.loads(config.read_text(encoding='utf-8-sig')).get('gateway_token'):
        parser.error('Build with the repository configuration template and an empty gateway token.')

    import av

    logo = REPO / 'ground-station' / 'brand-logo.png'
    av_libraries = Path(av.__file__).resolve().parent.parent / 'av.libs'
    station = [
        '--windowed', '--name', 'YRT-ThreeLink-Station', '--collect-all', 'av',
        '--add-data', f'{config};.', '--add-data', f'{logo};.',
    ]
    if av_libraries.is_dir():
        station.extend(('--add-binary', f'{av_libraries / "*.dll"};av.libs'))

    with tempfile.TemporaryDirectory(prefix='yrt-windows-build-') as work:
        common = [
            sys.executable, '-m', 'PyInstaller', '--noconfirm', '--onefile',
            '--specpath', work, '--workpath', str(Path(work) / 'build'),
            '--distpath', str(args.output.resolve()),
        ]
        if args.runtime_temp:
            common.extend(('--runtime-tmpdir', str(args.runtime_temp.resolve(strict=True))))

        subprocess.run(common + station + [str(SOURCE / 'desktop.py')], check=True)
        subprocess.run(common + [
            '--console', '--name', 'YRT-LoRa-Bench', str(SOURCE / 'lora_bench.py'),
        ], check=True)


if __name__ == '__main__':
    main()
