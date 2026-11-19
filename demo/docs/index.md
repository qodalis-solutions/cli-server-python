---
title: Welcome to {{name}}
order: 1
---
# Getting started

The demo's public API answers at `{{ baseUrl }}/v1/demo/time`. Send an API token created with the `demo:read`
permission:

```bash
curl -H "Authorization: Bearer qcli_pat_..." {{ baseUrl }}/v1/demo/time
```

Every account may make 1000 requests a day and 60 a minute; each answer carries `X-RateLimit-Limit`,
`X-RateLimit-Remaining` and `X-RateLimit-Reset`.
