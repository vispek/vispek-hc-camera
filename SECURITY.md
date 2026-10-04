# Security policy

## Supported versions

The latest 0.x minor release.

## Reporting

Please report vulnerabilities privately through GitHub's "Report a vulnerability" button
on this repository (private vulnerability reporting), or by e-mail to
<contact@vispek.cn>. Do not open a public issue.

- We acknowledge reports within 3 working days.
- We aim to publish a fix and an advisory within 90 days, coordinated with you.

**UV safety defects are security issues.** Anything that can light LEDs 1-4 without
explicit permission, or leave an LED lit, goes through the same private channel.

## No telemetry

The SDK makes no network connections. A test (`tests/test_no_network.py`) enforces this.
