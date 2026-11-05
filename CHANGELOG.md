# Change Log

Notable changes to the Python CLI server packages (`qodalis-cli-server`, `qodalis-cli-server-abstractions`,
`qodalis-cli-server-plugin-*`). Every package shares one version with the .NET and Node servers (`1.0.0b14` is
PEP 440 for `1.0.0-beta.14`).

## 1.0.0b14

### Dashboard name and icon

- `CliAdminBuilder.set_branding({"name", "icon_url", "cli_name"})`: the name and icon an application ships the
  dashboard under, and its terminal's name, served in `/qcli/admin/theme.json` and validated when set. Defaults:
  Qodalis Cli Admin with the Qodalis Solutions logo. See [Name and icon](plugins/admin/README.md#name-and-icon).

### Other packages

- Released at `1.0.0b14` with the .NET and Node servers; no other change.
