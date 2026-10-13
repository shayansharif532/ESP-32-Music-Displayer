# Security policy

## Reporting a vulnerability

Please report suspected vulnerabilities **privately** using GitHub's "Report a vulnerability" button (Security > Advisories) on this repository.
Do not open a public issue for a security problem. Include what you found, how to reproduce it, and the version / commit you tested.

This is a hobby-scale project maintained by one person: reports are read, but there is no guaranteed response time and no bug bounty.

## What is in scope

The Windows companion (`companion/`), the firmware (`firmware/`), the pairing and request-authentication protocol ([docs/protocol.md](docs/protocol.md)),
and anything that could leak secrets from the repository.

The threat model, what is protected, and the known limitations (for example: the LAN protocol is authenticated but not encrypted) are described in
[docs/security.md](docs/security.md). Please read it before reporting something that is listed there as a limitation.

## Supported versions

Only the latest commit of the default branch.
