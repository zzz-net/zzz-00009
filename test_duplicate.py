import sys
import os
sys.path.insert(0, 'src')
from asset_retag.cli import main

exit_code = 0
try:
    main(['dry-run', '-c', 'examples/config.yaml', '-m', 'examples/test_duplicate_only.csv', '--skip-confirm'])
except SystemExit as e:
    exit_code = e.code if e.code is not None else 0
print(f'Exit code: {exit_code}')
