#!/usr/bin/env python3
"""Copy the dashboard's Filesystem page (@qodalis/cli-dashboard-module-filesystem) into the package."""
import os
import shutil
import sys

here = os.path.dirname(os.path.abspath(__file__))
dest = os.path.join(here, '..', 'qodalis_cli_filesystem', 'dashboard')


def copy(src: str, origin: str) -> None:
    if os.path.exists(dest):
        shutil.rmtree(dest)
    shutil.copytree(src, dest)
    print(f'Filesystem page copied from {origin}: {os.path.abspath(src)}')
    sys.exit(0)


# Source 1: sibling repo (development)
dev_src = os.path.join(here, '..', '..', '..', '..', 'cli-server-dashboard', 'modules', 'filesystem', 'dist')
if os.path.isfile(os.path.join(dev_src, 'extension.js')):
    copy(dev_src, 'dev repo')

# Source 2: npm package (search upward for node_modules)
current = here
while current != os.path.dirname(current):
    candidate = os.path.join(current, 'node_modules', '@qodalis', 'cli-dashboard-module-filesystem', 'dist')
    if os.path.isfile(os.path.join(candidate, 'extension.js')):
        copy(candidate, 'npm package')
    current = os.path.dirname(current)

print('Warning: the filesystem page was not found; the dashboard will not show it', file=sys.stderr)
