# Security policy

skillpose is a private, non-commercial learning project. There is no support and no bug bounty.

## Reporting a vulnerability

Please report a vulnerability privately with GitHub's "Report a vulnerability" button (tab "Security", then
"Advisories"). Please do not open a public issue for it.

Please include the version or commit, the steps to reproduce and the effect. I read reports in my spare time, so an
answer can take some weeks.

## Supported versions

Only the latest commit on `main` gets fixes.

## Scope

skillpose is designed to run on 127.0.0.1 for one user. Running it on another address without HTTPS and
`APP_PASSWORD` is not supported.
