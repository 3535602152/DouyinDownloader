# Security Policy

## Reporting a vulnerability

Please do not publish credentials, cookies, session tokens, private download paths, personal data, or working exploit details in a public Issue.

If GitHub shows a **Report a vulnerability** option on the repository Security tab, use it for private disclosure. If private reporting is unavailable, open a minimal Issue titled **Security contact request** without sensitive technical details so a private channel can be arranged.

Include the affected version or commit, the security impact, reproducible conditions, and any safe proof-of-concept details that do not expose third-party data.

## Supported versions

Security fixes are targeted at the latest release and the current `main` branch. Older snapshots may not receive backports.

## Credential handling

Douyin cookies and other authentication material are local secrets. The repository ignores local cookie/config files, databases, downloaded media, and workbench artifacts. Before attaching logs or diagnostics, review them for tokens, account identifiers, local paths, and downloaded content.
