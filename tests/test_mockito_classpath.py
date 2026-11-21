import os
from pathlib import Path
import subprocess

import pytest

from test_junit4 import java_environment


@pytest.mark.java_integration
@pytest.mark.parametrize('profile', ['assertj', 'project-mockito'])
def test_mockito_internal_and_batch_classpaths(tmp_path, java_environment, profile):
    image, libs = java_environment
    repo = Path(__file__).resolve().parents[1]
    author = os.environ.get('KTESTER_FIXTURE_TOOL')
    mount = ['-v', f'{author}:/author:ro'] if author else []
    subprocess.run(['docker', 'run', '--rm', '--pull=never', '--network=none',
                    '-v', f'{repo}:/code:ro', '-v', f'{libs}:/libs:ro', '-v', f'{tmp_path}:/fixture',
                    *mount, '-e', 'PYTHONPATH=/code/src', '-e', 'PYTHONDONTWRITEBYTECODE=1',
                    image, 'python3', '/code/tests/fixtures/ktester/mockito/check.py', profile],
                   check=True, timeout=150)
