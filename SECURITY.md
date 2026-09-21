# Security

## Reporting a vulnerability

Please report privately rather than in a public issue.

Use GitHub's private reporting: go to the **Security** tab of this repository and
choose **Report a vulnerability**. That opens a private thread with the
maintainers and is the fastest route to someone who can fix it.

If you cannot use GitHub, contact RunDiffusion through
[rundiffusion.com](https://www.rundiffusion.com) and ask for the security team.

Please include what an attacker could do, the steps to reproduce it, and the
extension version from the panel's **Account** tab.

## What to expect

We will acknowledge your report and tell you what we plan to do about it. We
will let you know when a fix ships, and we are happy to credit you unless you
would rather we did not.

If you have not heard back and think we have missed it, say so on the same
thread. That is not a nuisance, it is the fastest way to find out.

## Scope

This repository holds the Omniverse extension: a client that talks to the
RunDiffusion API. Reports about the extension itself belong here, including
anything about how it stores your session on disk or what it sends.

Vulnerabilities in the RunDiffusion service or website are not in this
repository, but report them the same way and we will route them.

## What the extension holds

The extension stores no credentials of its own. Signing in uses a device-code
flow in your browser, and the resulting token is sealed with Windows DPAPI under
your own user account, so another user on the same machine cannot read it.

If you believe a build of this extension contains a credential, treat that as a
vulnerability and report it. It should never happen, and we would want to know
within the hour.
