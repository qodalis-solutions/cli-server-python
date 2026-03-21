# qodalis-cli-server-plugin-aws (Python)

AWS cloud resource management plugin for [Qodalis CLI Server (Python)](https://github.com/qodalis-solutions/cli-server-python). Provides commands for managing S3, EC2, Lambda, CloudWatch, SNS, SQS, IAM, DynamoDB, and ECS resources directly from the CLI.

## Install

```bash
pip install qodalis-cli-server-plugin-aws
```

## Quick Start

```python
from qodalis_cli import create_cli_server
from qodalis_cli_aws import AwsModule

def configure(builder):
    builder.add_module(AwsModule())

app = create_cli_server(configure=configure)
```

## Authentication

The plugin supports the full AWS credential chain:

1. **CLI configure command**: `aws configure set --key <key> --secret <secret> --region <region>`
2. **Environment variables**: `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_REGION`
3. **AWS profiles**: `aws configure set --profile <name>`
4. **IAM roles**: Automatic when running on EC2/ECS/Lambda

Check connectivity: `aws status`

## Commands

### Configuration

| Command | Description |
|---------|-------------|
| `aws configure set` | Set AWS credentials and region |
| `aws configure get` | Show current configuration (secrets masked) |
| `aws configure profiles` | List available AWS profiles |
| `aws status` | Test connectivity via STS GetCallerIdentity |

### S3

| Command | Description |
|---------|-------------|
| `aws s3 ls [bucket]` | List buckets or objects in a bucket |
| `aws s3 cp <source> <dest>` | Copy objects between S3 locations |
| `aws s3 rm <path>` | Delete an object (supports `--dry-run`) |
| `aws s3 mb <name>` | Create a bucket |
| `aws s3 rb <name>` | Delete a bucket (supports `--dry-run`) |
| `aws s3 presign <path>` | Generate a pre-signed URL |

### EC2

| Command | Description |
|---------|-------------|
| `aws ec2 list` | List EC2 instances |
| `aws ec2 describe <id>` | Describe an instance |
| `aws ec2 start <id>` | Start an instance (supports `--dry-run`) |
| `aws ec2 stop <id>` | Stop an instance (supports `--dry-run`) |
| `aws ec2 reboot <id>` | Reboot an instance (supports `--dry-run`) |
| `aws ec2 sg list` | List security groups |

### Lambda

| Command | Description |
|---------|-------------|
| `aws lambda list` | List Lambda functions |
| `aws lambda invoke <name>` | Invoke a function (supports `--payload`) |
| `aws lambda logs <name>` | View recent function logs |

### CloudWatch

| Command | Description |
|---------|-------------|
| `aws cloudwatch alarms` | List CloudWatch alarms |
| `aws cloudwatch logs <group>` | Fetch log events from a log group |
| `aws cloudwatch metrics <namespace>` | List metrics for a namespace |

### SNS

| Command | Description |
|---------|-------------|
| `aws sns topics` | List SNS topics |
| `aws sns publish <arn> --message <msg>` | Publish a message to a topic |
| `aws sns subscriptions [arn]` | List subscriptions |

### SQS

| Command | Description |
|---------|-------------|
| `aws sqs list` | List SQS queues |
| `aws sqs send <url> --message <msg>` | Send a message to a queue |
| `aws sqs receive <url>` | Receive messages (supports `--max`) |
| `aws sqs purge <url>` | Purge a queue (supports `--dry-run`) |

### IAM

| Command | Description |
|---------|-------------|
| `aws iam users` | List IAM users |
| `aws iam roles` | List IAM roles |
| `aws iam policies` | List customer-managed policies |

### DynamoDB

| Command | Description |
|---------|-------------|
| `aws dynamodb tables` | List DynamoDB tables |
| `aws dynamodb describe <table>` | Describe a table |
| `aws dynamodb scan <table>` | Scan items (supports `--limit`) |
| `aws dynamodb query <table> --key <expr>` | Query items |

### ECS

| Command | Description |
|---------|-------------|
| `aws ecs clusters` | List ECS clusters |
| `aws ecs services <cluster>` | List services in a cluster |
| `aws ecs tasks <cluster>` | List tasks in a cluster |

## Common Parameters

| Parameter | Alias | Description |
|-----------|-------|-------------|
| `--region` | `-r` | Override the AWS region for this command |
| `--output` | `-o` | Output format: `table` (default), `json`, or `text` |
| `--dry-run` | | Preview destructive operations without executing |

## Output Formats

Commands return structured responses adapted to the data:

- **Tables** for resource lists (instances, buckets, functions, etc.)
- **Key-value** for single resource details (describe commands)
- **JSON** for complex data (DynamoDB items, Lambda payloads)
- **Text** for status messages and confirmations
- **Lists** for simple enumerations (ARNs, profile names)

## Dependencies

This plugin uses boto3, the AWS SDK for Python:

- `boto3>=1.34.0`

boto3 uses the standard AWS credential chain automatically, including environment variables, shared credentials file (`~/.aws/credentials`), and instance profile metadata.

## License

MIT
