#!/usr/bin/env python3
"""Copy the dashboard's pages of this plugin into the package: API tokens (@qodalis/cli-dashboard-module-tokens,
into ``dashboard``) and Users + Account (@qodalis/cli-dashboard-module-users, into ``dashboard_users``)."""
import os
import shutil
import sys

here = os.path.dirname(os.path.abspath(__file__))
package = os.path.join(here, '..', 'qodalis_cli_users')

MODULES = [
    ('tokens', 'API tokens page', 'dashboard'),
    ('users', 'Users page', 'dashboard_users'),
]


def copy(src: str, dest: str, title: str, origin: str) -> None:
    if os.path.exists(dest):
        shutil.rmtree(dest)
    shutil.copytree(src, dest)
    print(f'{title} copied from {origin}: {os.path.abspath(src)}')


def find(name: str):
    # Source 1: sibling repo (development)
    dev_src = os.path.join(here, '..', '..', '..', '..', 'cli-server-dashboard', 'modules', name, 'dist')
    if os.path.isfile(os.path.join(dev_src, 'extension.js')):
        return dev_src, 'dev repo'
    # Source 2: npm package (search upward for node_modules)
    current = here
    while current != os.path.dirname(current):
        candidate = os.path.join(current, 'node_modules', '@qodalis', f'cli-dashboard-module-{name}', 'dist')
        if os.path.isfile(os.path.join(candidate, 'extension.js')):
            return candidate, 'npm package'
        current = os.path.dirname(current)
    return None, None


for name, title, folder in MODULES:
    src, origin = find(name)
    if src:
        copy(src, os.path.join(package, folder), title, origin)
    else:
        print(f'Warning: the {title} was not found; the dashboard will not show it', file=sys.stderr)
