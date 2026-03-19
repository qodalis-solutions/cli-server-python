#!/usr/bin/env python3
"""Copy dashboard dist files into the package for embedding."""
import os
import shutil
import sys

dest = os.path.join(os.path.dirname(__file__), '..', 'qodalis_cli_admin', 'dashboard')

# Source 1: sibling repo (development)
dev_src = os.path.join(os.path.dirname(__file__), '..', '..', '..', '..', 'cli-server-dashboard', 'dist')
if os.path.isdir(dev_src):
    if os.path.exists(dest):
        shutil.rmtree(dest)
    shutil.copytree(dev_src, dest)
    print(f'Dashboard copied from dev repo: {os.path.abspath(dev_src)}')
    sys.exit(0)

# Source 2: npm package (search upward for node_modules)
current = os.path.dirname(os.path.abspath(__file__))
while current != os.path.dirname(current):
    candidate = os.path.join(current, 'node_modules', '@qodalis', 'cli-server-dashboard', 'dist')
    if os.path.isdir(candidate):
        if os.path.exists(dest):
            shutil.rmtree(dest)
        shutil.copytree(candidate, dest)
        print(f'Dashboard copied from npm package: {candidate}')
        sys.exit(0)
    current = os.path.dirname(current)

print('Warning: Dashboard dist not found — dashboard will not be available', file=sys.stderr)
