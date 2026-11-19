#!/usr/bin/env python3
"""Copy the portal site's dist files into the package for embedding."""
import os
import shutil
import sys

dest = os.path.join(os.path.dirname(__file__), '..', 'qodalis_cli_portal', 'site')

# Source 1: sibling repo (development)
dev_src = os.path.join(os.path.dirname(__file__), '..', '..', '..', '..', 'cli-server-dashboard', 'portal', 'dist')
if os.path.isdir(dev_src):
    if os.path.exists(dest):
        shutil.rmtree(dest)
    shutil.copytree(dev_src, dest)
    print(f'Portal site copied from dev repo: {os.path.abspath(dev_src)}')
    sys.exit(0)

# Source 2: npm package (search upward for node_modules)
current = os.path.dirname(os.path.abspath(__file__))
while current != os.path.dirname(current):
    candidate = os.path.join(current, 'node_modules', '@qodalis', 'cli-server-portal', 'dist')
    if os.path.isdir(candidate):
        if os.path.exists(dest):
            shutil.rmtree(dest)
        shutil.copytree(candidate, dest)
        print(f'Portal site copied from npm package: {candidate}')
        sys.exit(0)
    current = os.path.dirname(current)

print('Warning: portal site dist not found — the portal will not be served', file=sys.stderr)
