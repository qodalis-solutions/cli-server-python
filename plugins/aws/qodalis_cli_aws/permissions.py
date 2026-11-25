"""The permissions of the ``aws`` command (enforced on a protected server, along the command chain)."""

from __future__ import annotations

#: Every ``aws`` command: read the account's resources (list, describe, logs, metrics, scan, query, presigned GET URLs).
AWS_READ = "aws:read"
#: The commands that change something: s3 cp/rm/mb/rb, ec2 start/stop/reboot, lambda invoke, sqs send/purge,
#: sns publish, configure set.
AWS_WRITE = "aws:write"


class AwsPermissions:
    READ = AWS_READ
    WRITE = AWS_WRITE


#: Added to the server's permission catalog with the module.
AWS_PERMISSION_ENTRIES = [
    {"name": AWS_READ, "description": "Read AWS resources from the terminal (aws commands)", "group": "AWS"},
    {"name": AWS_WRITE, "description": "Change AWS resources and the AWS configuration from the terminal", "group": "AWS"},
]
