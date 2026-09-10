# Security Policy

The latest tagged release and the current `main` branch receive security
fixes.

Do not report vulnerabilities in a public issue. Use GitHub's
[private vulnerability report](https://github.com/vitalysim/herdr-synapse/security/advisories/new)
and include the Synapse and Herdr versions, platform, impact, and a minimal
reproduction. Redact team boards, local paths, credentials, CLI login tokens,
and raw provider responses.

Synapse runs with the same local-user permissions as Herdr; it is not a
sandbox boundary. The usage report reads supported agent CLIs' local login
tokens only when the user opens that report, sends them only to the providers'
HTTPS usage endpoints, and never stores, logs, or prints them.
