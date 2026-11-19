# Security

applybot handles sensitive personal data (your CVs, contact details, mail
credentials) and can send e-mail. If you find a vulnerability, for example
credentials or personal data leaking into logs, files or network calls, or a
way to bypass the approval step, please report it privately through GitHub's
"Report a vulnerability" (Security tab) rather than a public issue.

Credentials are only read from environment variables (`APPLYBOT_MAIL_PASS`,
`ANTHROPIC_API_KEY`) and must never be committed.
