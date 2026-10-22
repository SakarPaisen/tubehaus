# Security

Please report security problems privately through
[GitHub's private vulnerability reporting](https://github.com/biolend83/tubehaus/security/advisories/new)
instead of a public issue.

Keep in mind that TubeHaus has no accounts of its own: anyone who can open it shares the same library.
Always put a login (Caddy `basic_auth`, Authelia, Authentik, a passkey gate, ...) in front of it before
exposing it to the internet.
